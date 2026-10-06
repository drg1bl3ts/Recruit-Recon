"""
Paylocity adapter — HTML scraping approach.

Paylocity's hosted careers boards are React SPAs that hydrate from a
server-side-rendered `window.pageData = {...}` JavaScript object embedded
directly in the page HTML. There is *no* useful XHR call after page load —
the public Job Feed v2 API documented at developers.paylocity.com requires
a different "API key" guid that's not exposed publicly per company.

So we fetch the careers page HTML, extract the JSON blob from the inline
script tag, and optionally fetch each job's detail page to get the full
description (descriptions are empty in the list-page payload).

Config:
    company_id: e.g. "021c9a71-0fb7-40fc-ab23-5370c11658d5"  (UUID from URL)
    company_slug: e.g. "Binary-Defense"  (URL-safe name from URL)
    fetch_descriptions: bool, default True. Set False to skip per-job
                        description fetches (faster but no cert badging).
    detail_concurrency: int, default 4. Worker pool size for per-job
                        description fetches — each worker still paces itself
                        with detail_delay_ms.

Page URL format:
    https://recruiting.paylocity.com/recruiting/jobs/All/{company_id}/{company_slug}

Detail URL format (constructed from JobId in the list payload):
    https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}

Apply URL format:
    https://recruiting.paylocity.com/Recruiting/Jobs/Apply/{JobId}

window.pageData shape (truncated):
    {
      "Jobs": [
        {
          "JobId": 3971632,
          "JobTitle": "Cybersecurity Incident Response Analyst - REMOTE",
          "LocationName": "Houston, TX",
          "PublishedDate": "2026-03-05T08:15:24-06:00",
          "Description": "",                    # always empty on list page
          "IsInternal": false,
          "HiringDepartment": null,
          "JobLocation": {
            "City": "Houston", "State": "TX", "Country": "USA",
            "Zip": null, "Address": null
          },
          "IsRemote": true,
          "IndeedRemoteType": 2
        },
        ...
      ],
      "ModuleTitle": "Binary Defense",
      "ModuleId": "11647",
      "Locations": ["All Locations", "Remote", ...],
      "Departments": [...]
    }
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from .base import Adapter, Job, format_location


log = logging.getLogger(__name__)

# Fallback: pull the first non-empty Description JSON string from anywhere
# in the HTML. Used only if the structured pageData parse misses.
_DESC_HTML_RE = re.compile(r'"Description"\s*:\s*"((?:\\.|[^"\\])*)"')

# Markers for the window.pageData assignment in the HTML. We locate the start
# of the JSON value with a lightweight string search, then let Python's own
# JSON decoder walk the balanced braces — far more robust than a regex that
# tries to match `{...}` across multi-line nested objects.
_PAGE_DATA_MARKERS = (
    "window.pageData = ",
    "window.pageData=",
)


class PaylocityAdapter(Adapter):
    name = "paylocity"

    def fetch(self) -> Iterable[Job]:
        company_id = self.config.get("company_id")
        company_slug = self.config.get("company_slug", "")
        if not company_id:
            raise ValueError(
                f"{self.company_id}: paylocity adapter requires config.company_id "
                f"(the UUID from the careers page URL, e.g. "
                f"recruiting.paylocity.com/recruiting/jobs/All/<company_id>/<slug>)"
            )

        fetch_descriptions = self.config.get("fetch_descriptions", True)
        # Paylocity needs a real-looking UA — minimal one is fine, but the
        # default RecruitRecon UA gets a bot block on some endpoints.
        ua = self.config.get("user_agent_override") or (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        )
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        list_url = (
            f"https://recruiting.paylocity.com/recruiting/jobs/All/"
            f"{company_id}/{company_slug}"
        )

        resp = self._request(
            "GET", list_url,
            headers=headers,
            log_fn=log.error,
            error_msg=f"paylocity fetch failed for {self.company_id}",
        )
        if resp is None:
            return

        page_data = self._extract_page_data(resp.text)
        if page_data is None:
            log.error(
                "paylocity[%s]: could not extract window.pageData from HTML",
                self.company_id,
            )
            return

        jobs = page_data.get("Jobs", []) or []
        log.debug(
            "paylocity[%s]: list page returned %d jobs", self.company_id, len(jobs)
        )

        # Per-job description fetch is rate-controlled — Paylocity is forgiving
        # but no point hammering. 200ms between requests = ~5 req/sec max per
        # worker; detail_concurrency bounds how many run at once.
        delay_s = self.config.get("detail_delay_ms", 200) / 1000.0
        detail_concurrency = int(self.config.get("detail_concurrency", 4))

        valid = []
        for j in jobs:
            job_id = j.get("JobId")
            title = (j.get("JobTitle") or "").strip()
            if job_id is None or not title:
                continue
            job_id = str(job_id)

            # Build location from JobLocation dict + IsRemote flag
            jloc = j.get("JobLocation") or {}
            loc_parts = [jloc.get("City", ""), jloc.get("State", "")]
            location = ", ".join(p for p in loc_parts if p) or (j.get("LocationName") or "")
            location = format_location([location], j.get("IsRemote", False))

            display_url = (
                f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}"
            )
            apply_url = (
                f"https://recruiting.paylocity.com/Recruiting/Jobs/Apply/{job_id}"
            )
            valid.append((job_id, title, location, display_url, apply_url, j))

        # Description fetches are independent per-posting network calls —
        # parallelize with a small bounded pool instead of one-at-a-time.
        def _describe(item):
            if not fetch_descriptions:
                return ""
            description = self._fetch_description(item[3], headers)
            time.sleep(delay_s)
            return description

        if fetch_descriptions and valid:
            with ThreadPoolExecutor(max_workers=detail_concurrency) as pool:
                descriptions = list(pool.map(_describe, valid))
        else:
            descriptions = ["" for _ in valid]

        for (job_id, title, location, display_url, apply_url, j), description in zip(valid, descriptions):
            yield Job(
                id=job_id,
                title=title,
                location=location,
                url=display_url,
                apply_url=apply_url,
                description=description,
                raw=j,
            )

    @staticmethod
    def _extract_page_data(html: str) -> Optional[dict]:
        """Extract and parse the window.pageData JSON object from a Paylocity page.

        Uses json.JSONDecoder.raw_decode() rather than a regex so that nested
        braces (e.g. inside description HTML strings) don't fool the parser
        into stopping at the first `}` that appears on its own line.
        """
        # Find the start of the assignment
        start = -1
        for marker in _PAGE_DATA_MARKERS:
            idx = html.find(marker)
            if idx != -1:
                start = idx + len(marker)
                break
        if start == -1:
            return None

        # Fast-forward to the opening brace (handle optional whitespace)
        brace_idx = html.find("{", start)
        if brace_idx == -1:
            return None

        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(html, brace_idx)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            log.warning("paylocity: window.pageData JSON parse failed: %s", e)
        return None

    def _fetch_description(self, detail_url: str, headers: dict) -> str:
        """Fetch a single job detail page and pull the Description field."""
        resp = self._request(
            "GET", detail_url,
            headers=headers,
            log_fn=log.warning,
            error_msg=f"paylocity: detail fetch failed for {detail_url}",
            critical=False,
        )
        if resp is None:
            return ""

        # Detail pages also embed window.pageData. Structured parse first.
        page_data = self._extract_page_data(resp.text)
        if page_data:
            # Detail pages use a top-level "Job" or "JobDetails" object,
            # both of which contain a "Description" field with HTML content.
            for key in ("Job", "JobDetails", "JobPosting"):
                obj = page_data.get(key)
                if isinstance(obj, dict) and obj.get("Description"):
                    return obj["Description"]
            if page_data.get("Description"):
                return page_data["Description"]

        # Fallback: regex out the first non-empty Description string.
        # JSON-escapes need decoding (e.g. \u003c -> <).
        for m in _DESC_HTML_RE.finditer(resp.text):
            raw = m.group(1)
            if not raw:
                continue
            try:
                return json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                return raw
        return ""
