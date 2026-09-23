"""
Ashby public job board API adapter.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
Public, no auth required. Returns all listed jobs in one response — no pagination.

Used by: HackerOne, many modern startups (Ashby is increasingly common, especially
at YC-backed and Series A/B companies).

Find the slug from the public job board URL:
    https://jobs.ashbyhq.com/{slug}     ← the slug is the last path segment
    https://jobs.ashbyhq.com/hackerone  → slug = "hackerone"

Some companies use a custom domain (jobs.example.com) instead of jobs.ashbyhq.com.
The slug in their config still matches the underlying Ashby tenant — try View
Source on their careers page and search for "ashbyhq" to confirm.

Response shape (truncated):
    {
      "apiVersion": "1",
      "jobs": [
        {
          "title": "Senior Security Engineer",
          "location": "Remote, US",
          "isListed": true,
          "isRemote": true,
          "workplaceType": "Remote",
          "descriptionHtml": "<p>Join our team...</p>",
          "descriptionPlain": "Join our team...",
          "publishedAt": "2026-04-15T10:00:00.000+00:00",
          "employmentType": "FullTime",
          "jobUrl": "https://jobs.ashbyhq.com/example/<id>",
          "applyUrl": "https://jobs.ashbyhq.com/example/<id>/application",
          "compensation": {
            "compensationTierSummary": "$150K – $200K",
            "scrapeableCompensationSalarySummary": "$150K - $200K"
          }
        },
        ...
      ]
    }
"""

import logging
from typing import Iterable

from .base import Adapter, Job, format_location


log = logging.getLogger(__name__)


class AshbyAdapter(Adapter):
    name = "ashby"

    def fetch(self) -> Iterable[Job]:
        slug = self.config.get("slug")
        if not slug:
            raise ValueError(
                f"{self.company_id}: ashby adapter requires config.slug "
                f"(the part after jobs.ashbyhq.com/)"
            )

        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        params = {"includeCompensation": "true"}

        data = self._request_json(
            "GET", url,
            params=params,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            log_fn=log.error,
            error_msg=f"ashby fetch failed for {self.company_id}",
        )
        if data is None:
            return
        if not isinstance(data, dict):
            log.warning("ashby[%s]: unexpected response shape", self.company_id)
            return

        jobs = data.get("jobs", []) or []

        for j in jobs:
            # Filter out unlisted/draft jobs (default isListed=true if missing,
            # but we explicitly check)
            if not j.get("isListed", True):
                continue

            # Build a stable ID from the apply URL (Ashby uses UUIDs)
            apply_url = j.get("applyUrl", "") or j.get("jobUrl", "")
            job_id = ""
            if apply_url:
                # https://jobs.ashbyhq.com/{slug}/{uuid}/application → use {uuid}
                parts = apply_url.rstrip("/").split("/")
                # Find the UUID-like segment (it's typically the second-to-last
                # before "application" or the last segment of jobUrl)
                for p in reversed(parts):
                    if "-" in p and len(p) >= 30:  # UUID heuristic
                        job_id = p
                        break
                if not job_id:
                    job_id = parts[-1] if parts else ""

            # Location can be a string or a dict; normalize
            location = ""
            loc_field = j.get("location")
            if isinstance(loc_field, str):
                location = loc_field
            elif isinstance(loc_field, dict):
                location = loc_field.get("location", "") or ""
            location = format_location([location], j.get("isRemote", False))

            # Description: prefer plain text for cert extraction,
            # fall back to HTML (filters.strip_html will handle it)
            description = j.get("descriptionPlain") or j.get("descriptionHtml", "") or ""

            # Optionally append compensation to description so cert-extraction
            # picks up nothing useful, but the user can see it via the raw payload
            comp = j.get("compensation") or {}
            comp_summary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or ""
            if comp_summary:
                description = f"{description}\n\nCompensation: {comp_summary}"

            yield Job(
                id=job_id or j.get("title", "unknown"),
                title=j.get("title", ""),
                location=location,
                url=j.get("jobUrl", "") or apply_url,
                apply_url=apply_url,
                description=description,
                raw=j,
            )
