"""
Workable accounts API adapter.

Endpoint: https://apply.workable.com/api/v3/accounts/{account}/jobs
Workable publishes a public JSON feed used by their candidate-facing site.

Used by: Trail of Bits, many others.

Note: Workable also has an older /spi/v3/ endpoint that requires auth. We use the
public widget endpoint that the apply.workable.com career site itself fetches.
"""

import logging
from typing import Iterable

from .base import Adapter, Job, format_location


log = logging.getLogger(__name__)


class WorkableAdapter(Adapter):
    name = "workable"

    def fetch(self) -> Iterable[Job]:
        account = self.config.get("account")
        if not account:
            raise ValueError(f"{self.company_id}: workable adapter requires config.account")

        # Workable's public widget feed
        url = f"https://apply.workable.com/api/v3/accounts/{account}/jobs"

        # POST with empty filters returns all published jobs
        data = self._request_json(
            "POST", url,
            json={"query": "", "location": [], "department": [],
                  "workplace": [], "remote": [], "worktype": []},
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            log_fn=log.error,
            error_msg=f"workable fetch failed for {self.company_id}",
        )
        if data is None:
            return

        results = data.get("results", []) if isinstance(data, dict) else []

        for j in results:
            loc = j.get("location") or {}
            location = format_location(
                [loc.get("city", ""), loc.get("country", "")],
                j.get("workplace") == "remote",
            )

            shortcode = j.get("shortcode", "")
            apply_url = f"https://apply.workable.com/{account}/j/{shortcode}/" if shortcode else ""

            job_id = shortcode or str(j.get("id") or "")
            if not job_id:
                log.warning("workable[%s]: job missing both shortcode and id, skipping: %r",
                            self.company_id, j.get("title"))
                continue

            yield Job(
                id=job_id,
                title=j.get("title", ""),
                location=location,
                url=apply_url,
                apply_url=apply_url,
                description=j.get("description", "") or "",
                raw=j,
            )
