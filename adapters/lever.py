"""
Lever Postings API adapter.

Endpoint: https://api.lever.co/v0/postings/{company}?mode=json
Public, no auth required.

Used by: Coalfire and many others.
"""

import logging
from typing import Iterable

from .base import Adapter, Job


log = logging.getLogger(__name__)


class LeverAdapter(Adapter):
    name = "lever"

    def fetch(self) -> Iterable[Job]:
        company = self.config.get("company")
        if not company:
            raise ValueError(f"{self.company_id}: lever adapter requires config.company")

        url = f"https://api.lever.co/v0/postings/{company}"
        params = {"mode": "json"}

        data = self._request_json(
            "GET", url,
            params=params,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            log_fn=log.error,
            error_msg=f"lever fetch failed for {self.company_id}",
        )
        if data is None:
            return

        # Lever returns either a flat list or a list grouped by category;
        # mode=json gives a flat list of postings
        if isinstance(data, dict):
            data = data.get("postings", [])

        for p in data:
            cats = p.get("categories", {}) or {}
            location = cats.get("location", "") or ""

            # description fields: descriptionPlain, lists (responsibilities, etc.)
            text_chunks = [p.get("descriptionPlain", "") or p.get("description", "")]
            for lst in p.get("lists", []):
                text_chunks.append(lst.get("text", ""))
            text_chunks.append(p.get("additionalPlain", "") or p.get("additional", ""))
            description = "\n".join(c for c in text_chunks if c)

            yield Job(
                id=str(p["id"]),
                title=p.get("text", ""),
                location=location,
                url=p.get("hostedUrl", "") or p.get("applyUrl", ""),
                apply_url=p.get("applyUrl", "") or p.get("hostedUrl", ""),
                description=description,
                raw=p,
            )
