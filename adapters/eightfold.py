"""
Eightfold AI career-site adapter.

Used by: Microsoft (apply.careers.microsoft.com), Netflix
(explore.jobs.netflix.net). Public, no auth. Two search APIs exist and each
tenant has only one enabled — the other answers "Not authorized for PCSX" /
"PCSX is not enabled":

    search_api: pcsx  (Microsoft)
        GET https://{host}/api/pcsx/search?domain={domain}&query={term}&start={n}
        -> {"data": {"count": 1882, "positions": [
               {"id": 1970393556944449, "name": "...", "locations": ["..."],
                "positionUrl": "/careers/job/1970393556944449"}, ...]}}

    search_api: v2    (Netflix)
        GET https://{host}/api/apply/v2/jobs?domain={domain}&query={term}&start={n}
        -> {"count": 49, "positions": [
               {"id": 790317577115, "name": "...", "location": "...",
                "canonicalPositionUrl": "https://.../careers/job/790317577115"}, ...]}

Both return 10 positions per page whatever `num` says, and neither includes
the description. That comes from the v2 detail endpoint, which works on
every tenant:

    GET https://{host}/api/apply/v2/jobs/{id}?domain={domain} -> {"job_description": "<p>..."}

Descriptions are only fetched for postings passing title_filter (see
workday.py — same reasoning).

Job URLs are emitted as https://{host}/careers/job/{id}?domain={domain}.
The public page is a JS shell that returns 200 even for closed jobs (on
Microsoft), and the v2 detail endpoint also keeps answering 200 for closed
postings, so verify.py recognizes this URL shape and checks the PCSX
position_details endpoint instead, which 404s once a job is closed. The
domain param is what lets it build that call; the URL still opens normally
in a browser.

Config:
    host:       required, e.g. "apply.careers.microsoft.com"
    domain:     required, e.g. "microsoft.com"
    search_api: "pcsx" or "v2" (default "v2")
    search_text: string or list of strings (default "security"), merged and
                 deduped by position id
    max_pages:  optional, default 300 (x10 results per search term)
    page_delay_ms: optional, default 250. Pause between search pages —
                Microsoft answers 429 to fast bursts.
    fetch_descriptions / detail_delay_ms / detail_concurrency: as in workday.py
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from .base import Adapter, Job


log = logging.getLogger(__name__)

_PAGE_SIZE = 10  # fixed by Eightfold; `num` is ignored


class EightfoldAdapter(Adapter):
    name = "eightfold"

    def fetch(self) -> Iterable[Job]:
        host = self.config.get("host")
        domain = self.config.get("domain")
        if not (host and domain):
            raise ValueError(
                f"{self.company_id}: eightfold adapter requires config.host and config.domain"
            )
        search_api = self.config.get("search_api", "v2")
        if search_api not in ("pcsx", "v2"):
            raise ValueError(f"{self.company_id}: search_api must be 'pcsx' or 'v2'")
        search_terms = self.config.get("search_text", "security")
        if isinstance(search_terms, str):
            search_terms = [search_terms]
        max_pages = int(self.config.get("max_pages", 300))
        # Microsoft's search is ~190 pages and rate-limits bursts (429).
        self._page_delay_s = int(self.config.get("page_delay_ms", 250)) / 1000.0
        fetch_descriptions = self.config.get("fetch_descriptions", True)
        delay_s = int(self.config.get("detail_delay_ms", 200)) / 1000.0
        detail_concurrency = int(self.config.get("detail_concurrency", 4))

        base = f"https://{host}"
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}

        # Phase 1: search, merged across terms.
        postings = {}
        for term in search_terms:
            found = self._search(base, domain, search_api, term, max_pages, headers)
            if found is None:
                return
            for p in found:
                postings.setdefault(str(p["id"]), p)

        log.debug("eightfold[%s]: search returned %d postings", self.company_id, len(postings))

        # Phase 2: descriptions for title-matching postings only.
        def _describe(item):
            pid, p = item
            if not fetch_descriptions:
                return ""
            if self.title_filter and not self.title_filter(p.get("name", "")):
                return ""
            description = self._fetch_description(base, domain, pid, headers)
            if delay_s:
                time.sleep(delay_s)
            return description

        items = list(postings.items())
        if fetch_descriptions and items:
            with ThreadPoolExecutor(max_workers=detail_concurrency) as pool:
                descriptions = list(pool.map(_describe, items))
        else:
            descriptions = ["" for _ in items]

        for (pid, p), description in zip(items, descriptions):
            url = f"{base}/careers/job/{pid}?domain={domain}"
            locations = p.get("locations") or ([p["location"]] if p.get("location") else [])
            yield Job(
                id=pid,
                title=p.get("name", ""),
                location="; ".join(locations),
                url=url,
                apply_url=url,
                description=description,
                raw=p,
            )

    def _search(self, base: str, domain: str, search_api: str, term: str,
                max_pages: int, headers: dict) -> Optional[list]:
        """Page through one search term. Returns positions, or None on failure."""
        if search_api == "pcsx":
            url = f"{base}/api/pcsx/search"
        else:
            url = f"{base}/api/apply/v2/jobs"

        out = []
        start = 0
        for _ in range(max_pages):
            data = self._request_json(
                "GET", url,
                params={"domain": domain, "query": term, "start": start},
                headers=headers,
                log_fn=log.error,
                error_msg=f"eightfold search failed for {self.company_id} "
                          f"(search={term!r}, start={start})",
            )
            if data is None:
                return None
            if search_api == "pcsx":
                data = data.get("data") or {}
            page = data.get("positions") or []
            out.extend(page)
            start += _PAGE_SIZE
            if not page or start >= (data.get("count") or 0):
                return out
            time.sleep(self._page_delay_s)

        log.warning(
            "eightfold[%s]: stopped at max_pages=%d for search %r — "
            "raise max_pages or narrow search_text",
            self.company_id, max_pages, term,
        )
        return out

    def _fetch_description(self, base: str, domain: str, pid: str, headers: dict) -> str:
        data = self._request_json(
            "GET", f"{base}/api/apply/v2/jobs/{pid}",
            params={"domain": domain},
            headers=headers,
            log_fn=log.warning,
            error_msg=f"eightfold detail fetch failed for {self.company_id} ({pid})",
        )
        if not isinstance(data, dict):
            return ""
        return data.get("job_description") or ""
