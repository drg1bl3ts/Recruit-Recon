"""
Per-job verification — fetch the company's job page and confirm it's actually live.

Why this exists: ATS APIs (Greenhouse, Lever, Workable) sometimes return jobs
that the company has "internally killed" but not depublished. Aggregators
cache stale postings for weeks. The only reliable signal is to actually fetch
the apply URL and check the response.

Verification result codes:
    verified_live          - HTTP 200 and no "dead" markers in the page text
    closed_404             - HTTP 404
    closed_410             - HTTP 410 (Workable's hard-delete)
    closed_inactive_text   - HTTP 200 but page contains a "this job is closed" marker
    error                  - network / parse failure after all retries
    skipped                - URL is non-HTTP (mailto:, empty) or verification disabled
"""

import logging
import time
from typing import Optional

import requests

from db import now_iso


log = logging.getLogger(__name__)


# Markers that mean "this job page rendered but the role is dead".
# Lowercased substring matches against the page body.
DEAD_TEXT_MARKERS = [
    "this job is no longer",
    "no longer accepting applications",
    "no longer available",
    "this position is currently not accepting applications",  # appone.com
    "we're sorry, that job does not exist or is not currently active",  # paylocity
    "this job does not exist",
    "sorry, this job was removed",  # built in
    "position has been filled",
    # Specific phrasing rather than a bare "the position you are looking
    # for" — that fragment is generic enough to false-positive on a live
    # page's "browse other openings you might be looking for" copy.
    "the position you are looking for is no longer available",
    "the position you are looking for could not be found",
    "page not found",
]

_MAX_ATTEMPTS = 3


def verify_url(
    url: str,
    *,
    user_agent: str = "RecruitRecon/0.1",
    timeout: int = 15,
    delay_ms: int = 250,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    Fetch url and return a verification dict: {status, http_status, checked_at}.

    Retries up to _MAX_ATTEMPTS times on:
    - transient network errors (ConnectionError, Timeout)
    - HTTP 429 Too Many Requests (honours Retry-After header, capped at 30s)

    Pass `session` (a requests.Session) to reuse connections across calls —
    e.g. recon.py passes the same session used for a company's adapter fetch
    so verification requests to that company's own host reuse the pool.
    Falls back to a one-off `requests.get` when omitted.
    """
    if not url or not url.startswith("http"):
        return {"status": "skipped", "http_status": None, "checked_at": now_iso()}

    # Pace requests: sleep before issuing the GET so rapid back-to-back calls
    # naturally rate-limit without the caller needing to manage timing.
    time.sleep(delay_ms / 1000)

    requester = session or requests

    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requester.get(
                url,
                headers={"User-Agent": user_agent, "Accept": "text/html,*/*"},
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            if attempt < _MAX_ATTEMPTS - 1:
                backoff = 2 ** attempt
                log.warning(
                    "verify network error for %s (attempt %d/%d, retrying in %ds): %s",
                    url, attempt + 1, _MAX_ATTEMPTS, backoff, e,
                )
                time.sleep(backoff)
                continue
            log.warning("verify network error for %s (all %d attempts failed): %s",
                        url, _MAX_ATTEMPTS, e)
            return {"status": "error", "http_status": None, "checked_at": now_iso()}

        http = resp.status_code

        # 429 — back off and retry, honouring the server's Retry-After if present.
        # Retry-After may be seconds (RFC 7231) or an HTTP-date; we only handle
        # the numeric form and fall back to the cap otherwise.
        if http == 429:
            try:
                retry_after = min(int(resp.headers.get("Retry-After", 5)), 30)
            except ValueError:
                retry_after = 30
            log.warning(
                "verify 429 for %s (attempt %d/%d, backing off %ds)",
                url, attempt + 1, _MAX_ATTEMPTS, retry_after,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(retry_after)
                continue
            return {"status": "error", "http_status": 429, "checked_at": now_iso()}

        if http == 404:
            return {"status": "closed_404",  "http_status": 404, "checked_at": now_iso()}
        if http == 410:
            return {"status": "closed_410",  "http_status": 410, "checked_at": now_iso()}
        if http >= 400:
            return {"status": "error",       "http_status": http, "checked_at": now_iso()}

        body_lower = resp.text.lower() if resp.text else ""
        for marker in DEAD_TEXT_MARKERS:
            if marker in body_lower:
                return {
                    "status": "closed_inactive_text",
                    "http_status": http,
                    "checked_at": now_iso(),
                }

        return {"status": "verified_live", "http_status": http, "checked_at": now_iso()}

    # Should only be reached if _MAX_ATTEMPTS is 0 (impossible in practice)
    return {"status": "error", "http_status": None, "checked_at": now_iso()}
