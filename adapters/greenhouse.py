"""
Greenhouse Job Board API adapter.

Endpoint: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
Public, no auth required. Returns all live published jobs.

Used by: Praetorian, SpecterOps, many others.
"""

import logging
from typing import Iterable

from .base import Adapter, Job


log = logging.getLogger(__name__)


class GreenhouseAdapter(Adapter):
    name = "greenhouse"

    def fetch(self) -> Iterable[Job]:
        token = self.config.get("board_token")
        if not token:
            raise ValueError(f"{self.company_id}: greenhouse adapter requires config.board_token")

        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        params = {"content": "true"}  # include description in response

        data = self._request_json(
            "GET", url,
            params=params,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            log_fn=log.error,
            error_msg=f"greenhouse fetch failed for {self.company_id}",
        )
        if data is None:
            return

        for j in data.get("jobs", []):
            location = ""
            if isinstance(j.get("location"), dict):
                location = j["location"].get("name", "")

            # Greenhouse content field is HTML-encoded
            description = j.get("content", "") or ""

            yield Job(
                id=str(j["id"]),
                title=j.get("title", ""),
                location=location,
                url=j.get("absolute_url", ""),
                apply_url=j.get("absolute_url", ""),
                description=description,
                raw=j,
            )
