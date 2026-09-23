"""
Generic HTML scraper for company career pages that don't expose an ATS API.

Two scrape modes supported:

1. `mode: container` (default) — finds repeating job-container elements, extracts
   title/link/location via sub-selectors. Works for most ATS-style pages.

2. `mode: heading_list` — for prose-style pages (common on GitHub Pages sites)
   where each role is an <h2> or <h3> heading followed by paragraphs, and there's
   ONE shared apply link for the whole page. Extracts headings whose text matches
   role keywords, pairs each with the shared apply link.

Config per company:
    url:              required — the careers page to fetch
    mode:             optional — "container" (default) or "heading_list"

For mode=container:
    job_selector:       CSS selector matching each job container
    title_selector:     relative selector for title (default: first heading)
    link_selector:      relative selector for apply link (default: first <a>)
    location_selector:  relative selector for location text
    description_selector: relative selector for description blurb

For mode=heading_list:
    heading_selector:   which headings are role titles (default: "h2, h3")
    role_keywords:      list of substrings; only headings matching one are kept
                        (case-insensitive). Default: falls back to title_keywords
                        from top-level config via recon.py.
    apply_link_selector: CSS selector for the shared apply URL (default:
                        "a[href*='apply'], a[href*='job']"). First match wins.

Example — Black Lantern Security:
    mode: heading_list
    heading_selector: "h2"
    role_keywords: ["pen test", "penetration", "security", "attack surface"]
    apply_link_selector: "a[href*='monday.com']"

If selectors don't match on first run, use `python recon.py --debug-scrape <company_id>`
to dump what the page actually contains so you can tune selectors without guessing.

IMPORTANT: This adapter is intentionally forgiving. If it can't find anything,
it returns empty rather than crashing — that way a temporary site layout change
doesn't break the whole collection run.
"""

import logging
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Comment, Tag

from .base import Adapter, Job


log = logging.getLogger(__name__)


# Default selectors — wide net. Override per-company in config.yaml for precision.
DEFAULT_JOB_SELECTOR = (
    ".job, .job-listing, .position, .career-opportunity, .career-item, "
    "[data-job], article.job, li.job, .role, .opening"
)
DEFAULT_TITLE_SELECTOR = "h1, h2, h3, h4, .title, .job-title, .position-title"
DEFAULT_LINK_SELECTOR = "a[href]"

# heading_list mode defaults
DEFAULT_HEADING_SELECTOR = "h2, h3"
DEFAULT_APPLY_LINK_SELECTOR = "a[href*='apply'], a[href*='job'], a[href*='monday.com']"
# Structural tags that mean "we've left the role content and hit page
# chrome" — stops the heading_list sibling-walk from running to the end of
# the document (and absorbing footer/nav boilerplate) when a role's heading
# is the last one on the page with no following same-or-higher heading.
BOILERPLATE_STOP_TAGS = {"footer", "nav", "header", "script", "style"}

DEFAULT_ROLE_KEYWORDS = [
    "pen test", "penetration", "pentest", "offensive security",
    "red team", "security consultant", "security engineer",
    "application security", "appsec", "attack surface",
    "vulnerability", "ethical hack", "asm", "analyst",
]


class HtmlScraperAdapter(Adapter):
    name = "html_scraper"

    def fetch(self) -> Iterable[Job]:
        url = self.config.get("url")
        if not url:
            raise ValueError(f"{self.company_id}: html_scraper requires config.url")

        resp = self._request(
            "GET", url,
            headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*"},
            verify=self.config.get("verify_tls", True),
            log_fn=log.error,
            error_msg=f"html_scraper fetch failed for {self.company_id}",
        )
        if resp is None:
            return

        soup = BeautifulSoup(resp.text, "html.parser")

        mode = self.config.get("mode", "container")
        if mode == "heading_list":
            yield from self._heading_list(soup, url)
        else:
            yield from self._container(soup, url)

    # ------------------------------------------------------------------
    # mode 1 — container-style pages (repeating job cards)
    # ------------------------------------------------------------------
    def _container(self, soup, url):
        job_sel = self.config.get("job_selector", DEFAULT_JOB_SELECTOR)
        title_sel = self.config.get("title_selector", DEFAULT_TITLE_SELECTOR)
        link_sel = self.config.get("link_selector", DEFAULT_LINK_SELECTOR)
        location_sel = self.config.get("location_selector")
        description_sel = self.config.get("description_selector")
        # If containers don't have their own apply link, point them all at a
        # single page-level link (Black Lantern uses one Monday.com form for all roles).
        shared_apply_sel = self.config.get("shared_apply_link_selector")

        shared_apply_url = None
        if shared_apply_sel:
            el = soup.select_one(shared_apply_sel)
            if el and el.get("href"):
                shared_apply_url = urljoin(url, el["href"])
                log.info("html_scraper[%s]: shared apply_url = %s",
                         self.company_id, shared_apply_url)
            else:
                log.warning("html_scraper[%s]: shared_apply_link_selector %r matched nothing",
                            self.company_id, shared_apply_sel)

        containers = soup.select(job_sel)
        if not containers:
            log.warning(
                "html_scraper[%s]: no job containers matched selector %r. "
                "Try mode=heading_list or run --debug-scrape %s to diagnose.",
                self.company_id, job_sel, self.company_id,
            )
            return

        log.info("html_scraper[%s]: found %d candidate containers",
                 self.company_id, len(containers))

        # Pre-compute a set of container element identities so sibling-walking
        # can stop at the next container in O(1) instead of calling soup.select()
        # for every sibling of every container (which would be O(containers×siblings)).
        container_ids = {id(c) for c in containers}

        seen_ids = set()
        for idx, el in enumerate(containers, 1):
            # title_selector == "self" means the matched element IS the title
            if title_sel == "self":
                title = el.get_text(" ", strip=True)
            else:
                title_el = el.select_one(title_sel)
                title = (title_el.get_text(strip=True) if title_el else "").strip()

            link_el = el.select_one(link_sel) if link_sel else None
            href = link_el.get("href") if link_el else None

            if not title and not href and not shared_apply_url:
                continue

            if href:
                apply_url = urljoin(url, href)
                # Keep the query string: some career pages differentiate
                # postings only by a query param (e.g. "?jobId=123"), so
                # stripping it collapsed distinct jobs onto one id and the
                # seen_ids dedup below silently dropped all but the first.
                job_id = href.split("#")[0] or f"scraped-{idx}"
            elif shared_apply_url:
                apply_url = shared_apply_url
                # Use title-derived ID so each role is distinct in the DB
                job_id = f"container-{idx}-{title[:40]}"
            else:
                apply_url = url
                job_id = f"scraped-{idx}-{title[:40]}"

            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            location = ""
            if location_sel:
                loc_el = el.select_one(location_sel)
                if loc_el:
                    location = loc_el.get_text(" ", strip=True)

            description = ""
            if description_sel:
                desc_el = el.select_one(description_sel)
                if desc_el:
                    description = desc_el.get_text(" ", strip=True)
            else:
                # For container-with-shared-apply pages, the container itself is
                # often just the title (e.g. <div class="accordion-header">Role</div>).
                # The body follows as the NEXT sibling, then another accordion-header
                # starts the next role. Walk same-depth siblings until we hit another
                # matching container.
                if shared_apply_url and title_sel == "self":
                    parts = []
                    for sib in el.find_next_siblings():
                        # Stop when we reach the next job container.
                        # Uses the pre-computed id-set — O(1) vs re-running
                        # soup.select() on the whole document for every sibling.
                        if id(sib) in container_ids:
                            break
                        # Stop at page-level headings
                        if sib.name in ("h1", "h2"):
                            break
                        # Inside the body, find any h3 starting with "Location"
                        if not location:
                            for h3 in sib.find_all(["h3", "h4"]):
                                t3 = h3.get_text(" ", strip=True)
                                if t3.lower().startswith("location"):
                                    loc_raw = t3.split(":", 1)[-1].strip()
                                    # Keep only the first location chunk;
                                    # strip "Travel:" trailing info if present.
                                    if "Travel" in loc_raw:
                                        loc_raw = loc_raw.split("Travel")[0].strip()
                                    location = loc_raw
                                    break
                        # Accumulate text for cert extraction
                        t = sib.get_text(" ", strip=True)
                        if t:
                            parts.append(t)
                        if sum(len(p) for p in parts) > 3000:
                            break
                    description = " ".join(parts)[:5000]
                else:
                    description = el.get_text(" ", strip=True)

            yield Job(
                id=job_id,
                title=title or "(untitled)",
                location=location,
                url=apply_url,
                apply_url=apply_url,
                description=description,
                raw={"mode": "container", "selector": job_sel, "href": href, "idx": idx},
            )

    # ------------------------------------------------------------------
    # mode 2 — prose pages with headings + one shared apply link
    # ------------------------------------------------------------------
    def _heading_list(self, soup, url):
        heading_sel = self.config.get("heading_selector", DEFAULT_HEADING_SELECTOR)
        keywords = self.config.get("role_keywords", DEFAULT_ROLE_KEYWORDS)
        apply_sel = self.config.get("apply_link_selector", DEFAULT_APPLY_LINK_SELECTOR)
        # Optional: grab a sibling for the location (e.g. "h3:contains('Location')")
        # BeautifulSoup doesn't support :contains; we walk manually below instead.

        # Find the shared apply link
        apply_el = soup.select_one(apply_sel)
        shared_apply_url = urljoin(url, apply_el["href"]) if apply_el and apply_el.get("href") else url
        log.info("html_scraper[%s]: shared apply_url = %s",
                 self.company_id, shared_apply_url)

        headings = soup.select(heading_sel)
        kw_lower = [k.lower() for k in keywords]

        seen_titles = set()
        yielded = 0
        for idx, h in enumerate(headings, 1):
            title = h.get_text(" ", strip=True)
            if not title:
                continue
            if title.lower() in seen_titles:
                continue
            t_lc = title.lower()
            if not any(kw in t_lc for kw in kw_lower):
                continue
            seen_titles.add(title.lower())

            # Walk forward in document order until the next heading at
            # same-or-higher level to capture description text (useful for
            # cert extraction). We don't break on lower-level headings (e.g.
            # <h3>Location: ...</h3> under an <h2>role) so their text gets
            # folded in.
            #
            # Uses next_elements (every node, Tags AND loose text) rather
            # than find_all_next (Tags only) — a NavigableString sitting
            # directly under the heading's parent with no wrapping tag (e.g.
            # "<h2>Role</h2>Some loose text...") is real content that
            # find_all_next silently skips. Tags are visited only to check
            # the stop conditions; their own text isn't pulled via
            # get_text() to avoid double-counting the same text once via the
            # tag and again via its NavigableString children.
            level_order = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
            my_level = level_order.get(h.name, 2)
            description_parts = []
            location = ""
            for sib in h.next_elements:
                if isinstance(sib, Tag):
                    if sib.name in level_order and level_order[sib.name] <= my_level:
                        break
                    if sib.name in BOILERPLATE_STOP_TAGS:
                        break
                    continue
                if isinstance(sib, Comment):
                    continue
                txt = str(sib).strip()
                if not txt:
                    continue
                if not location and txt.lower().startswith("location"):
                    # "Location: Remote" or "Location: Remote Travel: ..."
                    location = txt.split(":", 1)[-1].strip()
                description_parts.append(txt)
                if len(" ".join(description_parts)) > 3000:
                    break
            description = " ".join(description_parts)[:5000]

            yield Job(
                id=f"heading-{idx}-{title[:60]}",
                title=title,
                location=location,
                url=shared_apply_url,
                apply_url=shared_apply_url,
                description=description,
                raw={"mode": "heading_list", "heading": h.name, "idx": idx},
            )
            yielded += 1

        log.info("html_scraper[%s]: matched %d of %d headings against role_keywords",
                 self.company_id, yielded, len(headings))


def debug_scrape(company_id: str, config: dict, user_agent: str = "RecruitRecon/0.1",
                 timeout: int = 15) -> None:
    """
    Print diagnostics for one company's career page.
    Useful when selectors aren't matching.
    """
    url = config.get("url")
    if not url:
        print(f"{company_id}: no url in config")
        return

    print(f"\n=== debug_scrape: {company_id} ===")
    print(f"URL: {url}\n")

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"FETCH FAILED: {e}")
        return

    print(f"HTTP {resp.status_code}, {len(resp.text):,} bytes\n")

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try several candidate selectors and report hits
    candidates = [
        ".job", ".job-listing", ".position", ".career-opportunity",
        "[data-job]", "article.job", "li.job", ".role", ".opening",
        ".careers article", ".careers li", ".job-board li",
        "section article", "main article", "a[href*='job']",
        "a[href*='career']", "a[href*='apply']", "a[href*='monday.com']",
    ]
    print("Candidate selectors and match counts:")
    for sel in candidates:
        try:
            n = len(soup.select(sel))
            if n:
                print(f"  {n:>3}  {sel}")
        except Exception:
            pass

    # Show headings and links for context
    print("\nHeadings on page (first 20):")
    for i, h in enumerate(soup.find_all(["h1", "h2", "h3", "h4"])[:20], 1):
        text = h.get_text(" ", strip=True)[:80]
        print(f"  [{h.name}] {text}")

    print("\nLinks that look job-related (first 20):")
    seen = set()
    keywords = ["job", "career", "apply", "position", "opening",
                "pentest", "penetration", "monday.com", "workable"]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href in seen:
            continue
        if not any(k in href.lower() or k in a.get_text(strip=True).lower() for k in keywords):
            continue
        seen.add(href)
        if len(seen) > 20:
            break
        print(f"  {a.get_text(strip=True)[:60]!r} -> {href[:100]}")
