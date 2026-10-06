"""
Amazon Jobs adapter (amazon.jobs — Amazon's own career site, not a 3rd-party ATS).

Endpoint: https://www.amazon.jobs/en/search.json
    ?base_query={term}&result_limit=100&offset={n}
Public, no auth. Undocumented but it's what the amazon.jobs search page
itself calls. Each result includes the full description and qualifications,
so no per-job detail fetch is needed.

Response (truncated):
    {
      "hits": 291,
      "jobs": [
        {
          "id_icims": "10406134",
          "title": "Security Engineer, AWS Security",
          "job_path": "/en/jobs/10406134/security-engineer-aws-security",
          "normalized_location": "Seattle, Washington, USA",
          "description": "<p>...</p>",
          "basic_qualifications": "...",
          "preferred_qualifications": "...",
          "url_next_step": "https://account.amazon.jobs/jobs/10406134/apply"
        }, ...
      ]
    }

Config:
    search_text: string or list of strings (default "security"). One search
                 per term, merged and deduped by job id. The board is ~10k+
                 jobs; "security" alone matches ~9.4k (most AWS postings
                 mention it) but pages fine, and was checked (2026-10) to
                 cover every title-matching role except a few found by
                 "threat".
    params:      optional dict of extra query params, e.g.
                 {"normalized_country_code[]": "USA"} to limit to the US.
    max_pages:   optional, default 120 (x100 results). Offsets past ~10000
                 return no jobs.

result_limit is capped at 100 — larger values return "jobs": null.
Missing/closed job pages return 404, so verify.py works as-is.
"""

import logging
from typing import Iterable

from .base import Adapter, Job


log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_PAGE_SIZE = 100  # API maximum


class AmazonAdapter(Adapter):
    name = "amazon"

    def fetch(self) -> Iterable[Job]:
        search_terms = self.config.get("search_text", "security")
        if isinstance(search_terms, str):
            search_terms = [search_terms]
        extra_params = self.config.get("params") or {}
        max_pages = int(self.config.get("max_pages", 120))

        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}

        seen = set()
        for term in search_terms:
            offset = 0
            for _ in range(max_pages):
                data = self._request_json(
                    "GET", _SEARCH_URL,
                    params={
                        **extra_params,
                        "base_query": term,
                        "result_limit": _PAGE_SIZE,
                        "offset": offset,
                    },
                    headers=headers,
                    log_fn=log.error,
                    error_msg=f"amazon fetch failed for {self.company_id} "
                              f"(search={term!r}, offset={offset})",
                )
                if data is None:
                    return

                jobs = data.get("jobs") or []
                for j in jobs:
                    job_id = str(j.get("id_icims") or j.get("id") or "")
                    if not job_id or job_id in seen:
                        continue
                    seen.add(job_id)
                    yield self._to_job(job_id, j)

                offset += _PAGE_SIZE
                if not jobs or offset >= (data.get("hits") or 0):
                    break
            else:
                log.warning(
                    "amazon[%s]: stopped at max_pages=%d for search %r — "
                    "raise max_pages or narrow search_text",
                    self.company_id, max_pages, term,
                )

    @staticmethod
    def _to_job(job_id: str, j: dict) -> Job:
        url = f"https://www.amazon.jobs{j['job_path']}" if j.get("job_path") else ""
        # Certs are usually listed under qualifications, not the description
        # body, so fold all three into the text the cert extractor scans.
        description = "\n\n".join(
            part for part in (
                j.get("description"),
                j.get("basic_qualifications"),
                j.get("preferred_qualifications"),
            ) if part
        )
        return Job(
            id=job_id,
            title=j.get("title", ""),
            location=j.get("normalized_location") or j.get("location") or "",
            url=url,
            apply_url=j.get("url_next_step") or url,
            description=description,
            raw={k: v for k, v in j.items()
                 if k not in ("description", "basic_qualifications", "preferred_qualifications")},
        )
