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
    closed_unposted        - Workday API says the posting was taken down (403 S22)

Workday and Eightfold job pages can't be judged from the page itself (see
_WORKDAY_PUBLIC_RE / _EIGHTFOLD_PUBLIC_RE), so those URLs are checked via
the ATS's own job API first.
    error                  - network / parse failure after all retries
    skipped                - URL is non-HTTP (mailto:, empty) or verification disabled
"""

import logging
import re
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
    "this job may have been taken down",  # google careers (served with 200)
]

_MAX_ATTEMPTS = 3

# Workday's public job page (…myworkdayjobs.com/{locale}/{site}/job/…) is an
# empty JS shell that returns HTTP 200 whether or not the posting still
# exists, so the page check can never see a closed Workday job. The CXS JSON
# API behind it can: 200 = live, 404 (errorCode S21) = never existed,
# 403 with errorCode S22 = existed but has been unposted. Locale is optional
# in public URLs.
_WORKDAY_PUBLIC_RE = re.compile(
    r"^https://(?P<tenant>[^./]+)\.(?P<region>[^./]+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)(?P<path>/job/.+)$"
)
_WORKDAY_UNPOSTED_CODE = "S22"


def _workday_api_url(url: str) -> Optional[str]:
    """Map a public Workday job URL to its CXS API URL, or None if `url`
    isn't one."""
    m = _WORKDAY_PUBLIC_RE.match(url)
    if not m:
        return None
    return (f"https://{m['tenant']}.{m['region']}.myworkdayjobs.com"
            f"/wday/cxs/{m['tenant']}/{m['site']}{m['path']}")


# Eightfold (Microsoft, Netflix): the public page is also a JS shell on some
# tenants, and the v2 detail API keeps returning 200 for closed postings.
# The PCSX position_details endpoint 404s once a job is closed. The eightfold
# adapter emits job URLs with ?domain= so this call can be built from them.
_EIGHTFOLD_PUBLIC_RE = re.compile(
    r"^https://(?P<host>[^/]+)/careers/job/(?P<id>\d+)\?(?:.*&)?domain=(?P<domain>[^&#]+)"
)


def _eightfold_api_url(url: str) -> Optional[str]:
    """Map an eightfold-adapter job URL to its PCSX position_details URL,
    or None if `url` isn't one."""
    m = _EIGHTFOLD_PUBLIC_RE.match(url)
    if not m:
        return None
    return (f"https://{m['host']}/api/pcsx/position_details"
            f"?position_id={m['id']}&domain={m['domain']}")


def _result(status: str, http_status) -> dict:
    return {"status": status, "http_status": http_status, "checked_at": now_iso()}


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
        return _result("skipped", None)

    # Pace requests: sleep before issuing the GET so rapid back-to-back calls
    # naturally rate-limit without the caller needing to manage timing.
    time.sleep(delay_ms / 1000)

    requester = session or requests

    api_url = _workday_api_url(url) or _eightfold_api_url(url)
    if api_url:
        resp = _get(requester, api_url, "application/json", user_agent, timeout)
        if isinstance(resp, requests.Response):
            http = resp.status_code
            if http == 200:
                return _result("verified_live", http)
            if http == 404:
                return _result("closed_404", http)
            if http == 403 and _workday_error_code(resp) == _WORKDAY_UNPOSTED_CODE:
                return _result("closed_unposted", http)
        # Anything else (network failure, a tenant that blocks the API
        # outright) isn't a reliable signal either way — fall back to the
        # public page check below rather than call a live job closed.
        log.debug("job API check inconclusive for %s, falling back to page", url)

    resp = _get(requester, url, "text/html,*/*", user_agent, timeout)
    if not isinstance(resp, requests.Response):
        return resp  # already a result dict (network failure / repeated 429)

    http = resp.status_code
    if http == 404:
        return _result("closed_404", 404)
    if http == 410:
        return _result("closed_410", 410)
    if http >= 400:
        return _result("error", http)

    body_lower = resp.text.lower() if resp.text else ""
    for marker in DEAD_TEXT_MARKERS:
        if marker in body_lower:
            return _result("closed_inactive_text", http)

    return _result("verified_live", http)


def _workday_error_code(resp: requests.Response) -> Optional[str]:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data.get("errorCode") if isinstance(data, dict) else None


def _get(requester, url: str, accept: str, user_agent: str, timeout: int):
    """
    GET `url`, retrying up to _MAX_ATTEMPTS times on:
    - transient network errors (ConnectionError, Timeout)
    - HTTP 429 Too Many Requests (honours Retry-After header, capped at 30s)

    Returns the Response, or a final result dict ("error") once retries are
    exhausted.
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requester.get(
                url,
                headers={"User-Agent": user_agent, "Accept": accept},
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
            return _result("error", None)

        # 429 — back off and retry, honouring the server's Retry-After if present.
        # Retry-After may be seconds (RFC 7231) or an HTTP-date; we only handle
        # the numeric form and fall back to the cap otherwise.
        if resp.status_code == 429:
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
            return _result("error", 429)

        return resp

    # Should only be reached if _MAX_ATTEMPTS is 0 (impossible in practice)
    return _result("error", None)
