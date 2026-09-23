"""
Title filtering + certification extraction.

Two responsibilities:
1. Decide which API-returned jobs are pen-test relevant
2. Extract which certs are mentioned in a job description
"""

import re
from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=8)
def _lowered_keywords(keywords: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(kw.lower() for kw in keywords)


def title_matches(title: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match against any keyword."""
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in _lowered_keywords(tuple(keywords)))


@lru_cache(maxsize=8)
def _compiled_cert_patterns(cert_list: tuple[str, ...]) -> tuple[tuple[str, "re.Pattern"], ...]:
    # \b doesn't always work with mixed alphanumerics; use lookarounds.
    # Compiled once per distinct cert_list (fixed for the run) instead of
    # once per job x per cert.
    return tuple(
        (cert, re.compile(rf"(?<![A-Za-z0-9]){re.escape(cert)}(?![A-Za-z0-9])", re.IGNORECASE))
        for cert in cert_list
    )


def extract_certs(text: str, cert_list: Iterable[str]) -> list[str]:
    """
    Return list of certs mentioned in text.
    Whole-word match, case-insensitive, dedup preserving config order.
    """
    if not text:
        return []
    found = []
    for cert, pattern in _compiled_cert_patterns(tuple(cert_list)):
        if pattern.search(text):
            found.append(cert)
    return found


def has_gpen(certs_found: Iterable[str], gpen_marker="GPEN") -> bool:
    """
    Return True if ANY of the Tier A marker certs appear in certs_found.

    `gpen_marker` can be either:
      - a string (back-compat): a single cert name, e.g. "GPEN"
      - a list of strings: e.g. ["GFACT", "GSEC", "GCIH", "GPEN"]

    Comparison is case-insensitive. Historical name preserved even though
    this now supports multiple certs — renaming would break existing DBs.
    """
    return bool(matched_tier_a_certs(certs_found, gpen_marker))


def matched_tier_a_certs(certs_found: Iterable[str], gpen_marker="GPEN") -> list[str]:
    """
    Return the subset of certs_found that are also in the Tier A marker list.
    Preserves the original casing from certs_found (so we display "GSEC" not
    "gsec" on the badge). Order matches the iteration order of certs_found.

    Used by output.py to populate `tier_a_certs` on each job in jobs.json
    so the frontend can show *which* cert promoted the job to Tier A,
    instead of always showing "GPEN".
    """
    if isinstance(gpen_marker, str):
        markers = {gpen_marker.upper()}
    else:
        markers = {str(m).upper() for m in gpen_marker}
    return [c for c in certs_found if c.upper() in markers]


def strip_html(html: str) -> str:
    """
    Quick HTML strip for cert-extraction text. We don't need parsed structure,
    just the words. BeautifulSoup is overkill; a regex pass is fine.
    """
    if not html:
        return ""
    # Replace block-level tags with newlines so words don't run together
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
