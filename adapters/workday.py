"""
Workday CXS jobs API adapter.

Workday career sites (careers.rapid7.com, volarisgroup.wd3.myworkdayjobs.com, etc.)
are all backed by the same underlying API pattern:

    POST https://{tenant}.{region}.myworkdayjobs.com/wday/cxs/{tenant}/{site_id}/jobs
    Content-Type: application/json
    Body: {"appliedFacets":{},"limit":20,"offset":0,"searchText":""}

Response:
    {
      "total": 123,
      "jobPostings": [
        {
          "title": "Senior Security Researcher",
          "externalPath": "/job/us---remote/vector-command-specialist-penetration-testing_r11173",
          "locationsText": "US - Remote",
          "postedOn": "Posted 12 Days Ago",
          "bulletFields": ["R11173"]
        }, ...
      ]
    }

The list endpoint does NOT include job descriptions. To get the full description
text (so the cert extractor can find GPEN/GCIH/GFACT/GSEC mentions), we make a
second GET per job to:

    GET https://{tenant}.{region}.myworkdayjobs.com/wday/cxs/{tenant}/{site_id}/job{externalPath}

Response shape:
    {
      "jobPostingInfo": {
        "description": "<p>About the role...</p><p>Required: GPEN or OSCP...</p>",
        "title": "...",
        "location": "...",
        ...
      }
    }

This per-job fetch is the unlock that makes Tier A promotion work for Workday-
backed companies (Arctic Wolf, CrowdStrike, Optiv, Rapid7, Volaris, ReliaQuest,
NCC Group). Without it, every Workday job goes to Tier B/C even when the role
explicitly wants the certs we're tracking.

Config per company:
    tenant:    required, e.g. "mymoose" (Rapid7) or "volarisgroup"
    region:    required, e.g. "wd1", "wd3", "wd5"
    site_id:   required, e.g. "careers" or "volaris"
    locale:    optional, default "en-US"
    page_size: optional, default 20  (Workday caps this at 20 across all
                                      tenants — larger values return HTTP 400)
    max_pages: optional, default 50  (20 × 50 = 1000 jobs max per company,
                                      enough for CrowdStrike's 597 with room)
    fetch_descriptions: optional, default True. Set False to skip per-job
                        description fetches (faster but no cert badging).
    detail_delay_ms: optional, default 200. Per-job delay to avoid hammering.
    detail_concurrency: optional, default 4. Worker pool size for per-job
                        description fetches — each worker still paces itself
                        with detail_delay_ms, so this trades wall-clock time
                        for concurrency without dropping the per-request delay.
    search_text: optional, default "" (everything). A string or a list of
                 strings sent as Workday's searchText; a list runs one search
                 per term and merges the results. Use it on big-tech tenants
                 (Cisco, HPE, Salesforce...) whose full boards run to
                 thousands of postings and would otherwise hit max_pages.
                 Workday's search is loose full-text (description matches
                 count), so "security" still returns ~1400 at Cisco — it
                 narrows the list, the title filter does the rest.
                 Each search is capped at 2000 results by Workday itself
                 (no paging past it), so boards bigger than that (Booz
                 Allen) need several narrow terms rather than one broad one.

Descriptions are only fetched for postings that pass the adapter's
title_filter (supplied by recon.py from title_keywords). Postings that fail
it are still yielded, without a description, so recon.py's own filtering is
unchanged — this just skips detail requests for jobs it will discard.

For Rapid7: tenant=mymoose, region=wd1, site_id=careers
For Volaris: tenant=volarisgroup, region=wd3, site_id=volaris
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from .base import Adapter, Job


log = logging.getLogger(__name__)

# Hard per-search limit: Workday reports total=2000 at most, and offsets past
# 2000 return page 1 again rather than more postings.
_WORKDAY_RESULT_CAP = 2000


class WorkdayAdapter(Adapter):
    name = "workday"

    def fetch(self) -> Iterable[Job]:
        tenant = self.config.get("tenant")
        region = self.config.get("region")
        site_id = self.config.get("site_id")
        locale = self.config.get("locale", "en-US")
        # Workday caps limit at 20 across tenants — values >20 return HTTP 400.
        # Verified the hard way: bumping default to 50 broke every Workday
        # company on first request. Lesson: don't change request shape without
        # testing against a real endpoint first.
        page_size = int(self.config.get("page_size", 20))
        max_pages = int(self.config.get("max_pages", 50))
        fetch_descriptions = self.config.get("fetch_descriptions", True)
        delay_s = int(self.config.get("detail_delay_ms", 200)) / 1000.0
        detail_concurrency = int(self.config.get("detail_concurrency", 4))

        if not (tenant and region and site_id):
            raise ValueError(
                f"{self.company_id}: workday adapter requires config.tenant, "
                f"config.region, config.site_id"
            )

        base = f"https://{tenant}.{region}.myworkdayjobs.com"
        list_url = f"{base}/wday/cxs/{tenant}/{site_id}/jobs"
        # externalPath from the list endpoint already starts with "/job/...",
        # so the detail prefix here is just /wday/cxs/{tenant}/{site_id} —
        # NOT .../job. Concatenating .../job + /job/... = HTTP 406 from Workday.
        detail_url_prefix = f"{base}/wday/cxs/{tenant}/{site_id}"
        public_prefix = f"{base}/{locale}/{site_id}"

        # Workday is sometimes picky about Referer — set one that matches
        # the public site they expect to be the source of the request.
        common_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": public_prefix,
        }

        search_terms = self.config.get("search_text", "")
        if isinstance(search_terms, str):
            search_terms = [search_terms]

        # Phase 1: paginate through the list endpoint once per search term,
        # merging results. Postings matching several terms are deduped by
        # externalPath (stable per posting, unlike the display title).
        postings = []
        seen_paths = set()
        for term in search_terms:
            page_list = self._list_postings(
                list_url, term, page_size, max_pages,
                {**common_headers, "Content-Type": "application/json"},
            )
            if page_list is None:
                return
            for p in page_list:
                key = p.get("externalPath") or p.get("title")
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                postings.append(p)

        log.debug(
            "workday[%s]: list returned %d postings",
            self.company_id, len(postings),
        )

        # Phase 2: fetch descriptions (independent per-posting network calls —
        # parallelize with a small bounded pool instead of one-at-a-time).
        def _describe(p):
            external_path = p.get("externalPath", "")
            if not (fetch_descriptions and external_path):
                return ""
            if self.title_filter and not self.title_filter(p.get("title", "")):
                return ""
            description = self._fetch_description(
                f"{detail_url_prefix}{external_path}",
                common_headers,
            )
            if delay_s:
                time.sleep(delay_s)
            return description

        if fetch_descriptions and postings:
            with ThreadPoolExecutor(max_workers=detail_concurrency) as pool:
                descriptions = list(pool.map(_describe, postings))
        else:
            descriptions = ["" for _ in postings]

        for p, description in zip(postings, descriptions):
            external_path = p.get("externalPath", "")
            bullets = p.get("bulletFields", []) or []
            req_id = bullets[0] if bullets else (
                external_path.rsplit("_", 1)[-1] if external_path else ""
            )
            apply_url = f"{public_prefix}{external_path}" if external_path else ""

            yield Job(
                id=str(req_id) if req_id else (external_path or p.get("title", "unknown")),
                title=p.get("title", ""),
                location=p.get("locationsText", ""),
                url=apply_url,
                apply_url=apply_url,
                description=description,
                raw=p,
            )

    def _list_postings(self, list_url: str, search_text: str, page_size: int,
                       max_pages: int, headers: dict):
        """Paginate the list endpoint for one searchText. Returns the
        postings, or None if any page request failed."""
        postings = []
        offset = 0
        pages_fetched = 0
        # Workday's `total` field is reliable on the FIRST page only — on
        # subsequent pages many tenants report total=0 (CrowdStrike does this).
        # So we capture it once on the first response and use it as the
        # authoritative upper bound. Empty `jobPostings` array is also a stop
        # signal in case `total` is unreliable in some other way.
        known_total = None

        while pages_fetched < max_pages:
            body = {
                "appliedFacets": {},
                "limit": page_size,
                "offset": offset,
                "searchText": search_text,
            }
            data = self._request_json(
                "POST", list_url,
                json=body,
                headers=headers,
                log_fn=log.error,
                error_msg=f"workday list fetch failed for {self.company_id} "
                          f"(search={search_text!r}, offset={offset})",
            )
            if data is None:
                return None

            page = data.get("jobPostings", [])
            page_total = data.get("total", 0)
            if known_total is None and page_total:
                known_total = page_total
                log.debug(
                    "workday[%s]: tenant reports %d total postings for search %r",
                    self.company_id, known_total, search_text,
                )

            if not page:
                break
            postings.extend(page)
            pages_fetched += 1
            offset += page_size
            # Stop when we've fetched everything the tenant told us about
            # on page 1. Don't trust page_total from subsequent pages.
            if known_total is not None and offset >= known_total:
                break

        if known_total is not None and known_total >= _WORKDAY_RESULT_CAP:
            # Workday never reports or serves more than 2000 results per
            # search — offsets past it just return page 1 again — so the
            # board is probably bigger than what we got. max_pages can't help.
            log.warning(
                "workday[%s]: search %r hit Workday's %d-result cap — postings "
                "beyond it are unreachable; use narrower search_text terms",
                self.company_id, search_text, _WORKDAY_RESULT_CAP,
            )
        if known_total is not None and offset < min(known_total, _WORKDAY_RESULT_CAP):
            # Hit max_pages before the end — the tail of the board is missing.
            log.warning(
                "workday[%s]: stopped at max_pages=%d (%d of %d postings for "
                "search %r) — raise max_pages or narrow search_text",
                self.company_id, max_pages, len(postings), known_total, search_text,
            )
        return postings

    def _fetch_description(self, detail_url: str, headers: dict) -> str:
        """Fetch a single job's detail JSON and pull the description out.

        Workday calls the description field `jobDescription` (NOT `description`)
        nested under `jobPostingInfo`. The HTML body lives there. Some older
        tenants or alternate endpoints use `description` so we also accept that
        as a fallback.

        Verified shape (2026-04, CrowdStrike):
            {"jobPostingInfo": {"jobDescription": "<p>...</p>", ...}, ...}
        """
        resp = self._request(
            "GET", detail_url,
            headers=headers,
            log_fn=log.warning,
            error_msg=f"workday detail fetch failed for {detail_url}",
        )
        if resp is None:
            return ""
        try:
            data = resp.json()
        except ValueError as e:
            log.warning("workday detail response not JSON for %s: %s", detail_url, e)
            return ""

        # The actual Workday field name is jobDescription, not description.
        # Check it first on jobPostingInfo, then fall back to the same keys
        # on the top-level response for non-standard tenants.
        info = data.get("jobPostingInfo") or {}
        for src in (info if isinstance(info, dict) else {}, data):
            for key in ("jobDescription", "description"):
                val = src.get(key)
                if isinstance(val, str) and val:
                    return val
        return ""
