"""
iCIMS adapter — HTML scraping.

iCIMS career boards serve an iframe-embeddable job list at a predictable URL.
We fetch it with BeautifulSoup and page through using iCIMS's sn/sc parameters.

Config:
    tenant:        iCIMS subdomain — maps to {tenant}.icims.com
    custom_domain: full domain override when the company uses a custom CNAME
                   (e.g. "careers-peraton.icims.com" instead of "peraton.icims.com")
    page_size:     jobs per request (default 100; iCIMS silently caps at ~200)

URL patterns:
    Board:  https://{base}/jobs/search?ss=1&searchRelation=keyword_all&in_iframe=1
    Job:    https://{base}/jobs/{id}/{slug}/job
    Apply:  https://{base}/jobs/{id}/apply
"""

import logging
import re
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import Adapter, Job


log = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 100


class ICIMSAdapter(Adapter):
    name = "icims"

    def fetch(self) -> Iterable[Job]:
        tenant = self.config.get("tenant")
        custom_domain = self.config.get("custom_domain")
        if not tenant and not custom_domain:
            raise ValueError(
                f"{self.company_id}: icims adapter requires config.tenant or config.custom_domain"
            )

        base = custom_domain or f"{tenant}.icims.com"
        page_size = int(self.config.get("page_size", _DEFAULT_PAGE_SIZE))

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

        sn = 1
        seen_ids: set[str] = set()

        while True:
            url = (
                f"https://{base}/jobs/search"
                f"?ss=1&searchRelation=keyword_all&in_iframe=1"
                f"&sn={sn}&sc={page_size}"
            )

            resp = self._request(
                "GET", url,
                headers=headers,
                log_fn=log.error,
                error_msg=f"icims[{self.company_id}]: fetch failed (sn={sn})",
            )
            if resp is None:
                return

            page_jobs = list(self._parse_page(resp.text, base))
            new_jobs = [j for j in page_jobs if j.id not in seen_ids]

            if not new_jobs:
                break

            for j in new_jobs:
                seen_ids.add(j.id)
                yield j

            # Stop if we got fewer results than requested — means last page.
            # Also stop if no new jobs were found to guard against infinite loops.
            if len(page_jobs) < page_size:
                break

            sn += page_size

        log.info("icims[%s]: collected %d jobs total", self.company_id, len(seen_ids))

    def _parse_page(self, html: str, base: str) -> Iterable[Job]:
        soup = BeautifulSoup(html, "html.parser")

        # iCIMS standard skin: job table rows each hold an .iCIMS_Anchor link
        rows = soup.select(".iCIMS_TableRow")

        # Some custom iCIMS skins use a flat anchor list instead of table rows
        if not rows:
            anchors = soup.select("a.iCIMS_Anchor")
            if anchors:
                rows = anchors  # treat each anchor as its own "row"

        if not rows:
            log.warning(
                "icims[%s]: no job rows matched in response (%d bytes). "
                "The page skin may use non-standard CSS classes — "
                "check the HTML and update selectors.",
                self.company_id, len(html),
            )
            return

        for row in rows:
            anchor = (
                row
                if row.name == "a"
                else row.select_one("a.iCIMS_Anchor, a[href*='/jobs/']")
            )
            if not anchor:
                continue

            title = anchor.get_text(" ", strip=True)
            href = anchor.get("href", "")
            if not href or not title:
                continue

            # Extract numeric job ID from /jobs/{id}/...
            m = re.search(r"/jobs/(\d+)", href)
            if not m:
                continue
            job_id = m.group(1)

            job_url = urljoin(f"https://{base}", href)
            apply_url = f"https://{base}/jobs/{job_id}/apply"

            location_el = row.select_one(
                ".iCIMS_InfoField_Location, .icims-location, "
                "[class*='Location'], [class*='location']"
            )
            location = location_el.get_text(" ", strip=True) if location_el else ""

            yield Job(
                id=job_id,
                title=title,
                location=location,
                url=job_url,
                apply_url=apply_url,
                description="",
                raw={"href": href, "base": base},
            )
