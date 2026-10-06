"""
Google Careers adapter — scrapes data embedded in the server-rendered results page.

Google has no public jobs API. The results page
    https://www.google.com/about/careers/applications/jobs/results/?q={term}&page={n}
(page is 1-indexed, 20 jobs each) embeds its data in an inline script:

    AF_initDataCallback({key: 'ds:1', hash: '...', data:[JOBS, null, TOTAL, 20], sideChannel: {}});

Each JOB is a positional array (verified 2026-10):
    [0]  id                  "140694610140635846"
    [1]  title
    [3]  [null, responsibilities HTML]
    [4]  [null, qualifications HTML]
    [7]  company             "Google" / "DeepMind" / "YouTube" ...
    [9]  locations           [["Singapore", [...], null, null, null, "SG"], ...]
    [10] [null, about-the-job HTML]
    [19] [null, minimum-qualifications HTML]
Descriptions are included, so no per-job fetch is needed.

THIS IS FRAGILE: it depends on Google's page internals, not a published API.
If the ds:1 block disappears or changes shape, the adapter marks the fetch as
failed (had_errors) rather than returning zero jobs — so recon.py skips the
disappearance check instead of closing every Google posting. Look for
"google[...]: could not find job data" in the log.

Job pages: {RESULTS}/{id}-{slug}. A taken-down job still returns 200 but
renders "Job not found. This job may have been taken down." — caught by a
verify.py DEAD_TEXT_MARKERS entry.

Pages are ~1.3 MB each (mostly inline JS), so prefer narrow search_text.

Config:
    search_text: string or list of strings (default "security"), merged and
                 deduped by job id
    max_pages:   optional, default 100 (x20 results per search term)
    page_delay_ms: optional, default 500
"""

import json
import logging
import re
import time
from typing import Any, Iterable, Optional

from .base import Adapter, Job


log = logging.getLogger(__name__)

RESULTS_URL = "https://www.google.com/about/careers/applications/jobs/results"

_DS1_RE = re.compile(
    r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel: \{\}\}\);</script>",
    re.S,
)


def _at(seq: Any, *path: int) -> Any:
    """Positional lookup that returns None instead of raising when the
    structure is shorter or shaped differently than expected."""
    for i in path:
        if not isinstance(seq, list) or i >= len(seq):
            return None
        seq = seq[i]
    return seq


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


class GoogleAdapter(Adapter):
    name = "google"

    def fetch(self) -> Iterable[Job]:
        search_terms = self.config.get("search_text", "security")
        if isinstance(search_terms, str):
            search_terms = [search_terms]
        max_pages = int(self.config.get("max_pages", 100))
        delay_s = int(self.config.get("page_delay_ms", 500)) / 1000.0

        headers = {"User-Agent": self.user_agent, "Accept": "text/html"}

        seen = set()
        for term in search_terms:
            for page in range(1, max_pages + 1):
                resp = self._request(
                    "GET", f"{RESULTS_URL}/",
                    params={"q": term, "page": page},
                    headers=headers,
                    log_fn=log.error,
                    error_msg=f"google fetch failed for {self.company_id} "
                              f"(search={term!r}, page={page})",
                )
                if resp is None:
                    return

                data = self._extract(resp.text)
                if data is None:
                    self.had_errors = True
                    log.error(
                        "google[%s]: could not find job data (ds:1) on results "
                        "page %d for %r — page format probably changed",
                        self.company_id, page, term,
                    )
                    return

                jobs = data[0] or []
                for j in jobs:
                    job = self._to_job(j)
                    if job and job.id not in seen:
                        seen.add(job.id)
                        yield job

                total = data[2] if isinstance(data[2], int) else 0
                if not jobs or page * 20 >= total:
                    break
                time.sleep(delay_s)
            else:
                log.warning(
                    "google[%s]: stopped at max_pages=%d for search %r — "
                    "raise max_pages or narrow search_text",
                    self.company_id, max_pages, term,
                )

    @staticmethod
    def _extract(html: str) -> Optional[list]:
        m = _DS1_RE.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
        # Expected: [jobs, null, total, page_size]
        if not (isinstance(data, list) and len(data) >= 3
                and (data[0] is None or isinstance(data[0], list))):
            return None
        return data

    @staticmethod
    def _to_job(j: list) -> Optional[Job]:
        job_id = _at(j, 0)
        title = _at(j, 1)
        if not (isinstance(job_id, str) and isinstance(title, str)):
            return None

        locations = [
            loc[0] for loc in (_at(j, 9) or [])
            if isinstance(loc, list) and loc and isinstance(loc[0], str)
        ]
        company = _at(j, 7)
        location = "; ".join(locations)
        if isinstance(company, str) and company != "Google":
            # DeepMind, YouTube, etc. — surface the sub-brand on the card.
            location = f"{location} ({company})" if location else company

        description = "\n\n".join(
            part for part in (_at(j, 10, 1), _at(j, 3, 1), _at(j, 19, 1), _at(j, 4, 1))
            if isinstance(part, str) and part
        )
        url = f"{RESULTS_URL}/{job_id}-{_slug(title)}"
        return Job(
            id=job_id,
            title=title,
            location=location,
            url=url,
            apply_url=url,
            description=description,
            raw={"id": job_id, "title": title, "company": company, "locations": locations},
        )
