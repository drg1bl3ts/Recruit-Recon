"""
Adapter interface.

Every source (Greenhouse / Lever / Workable / static / future scrapers)
implements `fetch()` and yields a list of Job dicts in the canonical shape.

Canonical Job dict (the "wire format"):
    id          str   - unique within the source
    title       str
    location    str
    url         str   - public posting URL (will be verified)
    apply_url   str   - if different from url
    description str   - HTML or plain text, used for cert extraction
    raw         dict  - original payload (stored in DB for debugging)
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import requests


_MAX_429_RETRIES = 2


@dataclass
class Job:
    id: str
    title: str
    url: str
    location: str = ""
    apply_url: str = ""
    description: str = ""
    raw: dict = field(default_factory=dict)


def format_location(parts: Iterable[str], is_remote: bool) -> str:
    """
    Join non-empty location parts with ", " and append " (Remote)" when
    `is_remote` is set and the text doesn't already mention "remote".
    """
    location = ", ".join(p for p in parts if p)
    if is_remote and "remote" not in location.lower():
        location = f"{location} (Remote)".strip()
    return location


class Adapter(ABC):
    """Base class. Subclass and implement fetch()."""

    name: str = "base"

    def __init__(self, company_id: str, company_name: str, config: dict, *,
                 user_agent: str = "RecruitRecon/0.1", timeout: int = 15,
                 title_filter: Optional[Callable[[str], bool]] = None):
        self.company_id = company_id
        self.company_name = company_name
        self.config = config or {}
        self.user_agent = user_agent
        self.timeout = timeout
        # Same title-relevance check recon.py applies after fetch(). Adapters
        # with an expensive per-posting step (e.g. Workday's description
        # fetch) can call it first to skip postings recon.py would discard
        # anyway. Never used to drop jobs from fetch() output — recon.py
        # stays the single place that filters.
        self.title_filter = title_filter
        # Reused across every request this adapter instance makes on the main
        # thread (the initial list fetch) for TCP/TLS connection reuse against
        # the same host, instead of a fresh handshake per request. NOT shared
        # into worker threads — see _get_session().
        self.session = requests.Session()
        self._thread_local = threading.local()
        # Set by _request on a failed critical request (the job list itself).
        # recon.py checks this right after fetch() returns to tell "genuinely
        # empty board" apart from "source was unreachable" — adapters swallow
        # their own network exceptions and yield nothing either way, so this
        # is the only signal.
        self.had_errors = False

    @abstractmethod
    def fetch(self) -> Iterable[Job]:
        """Yield Job objects. Must be idempotent and side-effect free."""
        raise NotImplementedError

    @property
    def is_static(self) -> bool:
        """Override in StaticAdapter; everything else is False."""
        return False

    def _get_session(self) -> requests.Session:
        """
        Return the Session to use for the current thread.

        requests.Session's cookie jar isn't safe for concurrent
        read-modify-write from multiple threads, so per-posting/per-job
        detail fetches issued from a ThreadPoolExecutor (workday.py,
        paylocity.py) must not share `self.session` with each other or with
        the main thread's initial list fetch. Each worker thread lazily gets
        its own Session instead — still gets connection-pool reuse across
        that thread's own requests, just not across threads.
        """
        if threading.current_thread() is threading.main_thread():
            return self.session
        if not hasattr(self._thread_local, "session"):
            self._thread_local.session = requests.Session()
        return self._thread_local.session

    def _request(self, method: str, url: str, *, log_fn: Callable, error_msg: str,
                 critical: bool = True, **kwargs) -> Optional[requests.Response]:
        """
        Issue an HTTP request using this adapter's default timeout.

        Returns the Response on success (status raised for non-2xx). On
        requests.RequestException, calls `log_fn("%s: %s", error_msg, exc)`
        — so callers pass e.g. `log_fn=log.error` and an `error_msg` matching
        their original log line — and returns None so the caller can bail out
        exactly as it did before this helper existed.

        Pass critical=False for per-job extras (description fetches): their
        failure only costs that job its description, while the job list is
        still complete. They must not set had_errors, or one timed-out
        description would make recon.py skip the company's disappearance
        check for the whole run (Arctic Wolf, 2026-10-06).
        """
        kwargs.setdefault("timeout", self.timeout)
        try:
            for attempt in range(_MAX_429_RETRIES + 1):
                resp = self._get_session().request(method, url, **kwargs)
                # 429 — back off and retry, honouring a numeric Retry-After
                # (capped), same policy as verify.py.
                if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                    try:
                        wait = min(int(resp.headers.get("Retry-After", 5)), 30)
                    except ValueError:
                        wait = 30
                    logging.getLogger(__name__).warning(
                        "%s: HTTP 429, retrying in %ds (attempt %d/%d)",
                        url, wait, attempt + 1, _MAX_429_RETRIES)
                    time.sleep(wait)
                    continue
                break
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if critical:
                self.had_errors = True
            log_fn("%s: %s", error_msg, e)
            return None

    def _request_json(self, method: str, url: str, *, log_fn: Callable, error_msg: str,
                       **kwargs) -> Optional[Any]:
        """Like `_request`, but returns parsed JSON (or None on failure)."""
        resp = self._request(method, url, log_fn=log_fn, error_msg=error_msg, **kwargs)
        return resp.json() if resp is not None else None
