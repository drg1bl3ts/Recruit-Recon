# RecruitRecon

A self-hosted, **local-only** job board for cybersecurity roles. It pulls
listings straight from company ATS APIs (Greenhouse, Lever, Workable,
Workday, Ashby, Paylocity, iCIMS, plus a generic HTML scraper), verifies each
one against the company's own page instead of trusting a stale aggregator,
and tracks new/closed roles over time in SQLite.

It's built around **GIAC certifications**: as you progress through the SANS
track (GFACT → GSEC → GCIH → GPEN → ...), the board promotes any job
mentioning a cert you've earned to "Tier A." That's just the default —
everything is config-driven, so it works just as well for OSCP, CISSP, or
any keyword set you want. See [Certifications](#certifications--tier-a-matching)
below.

> **This has no authentication and is meant to run on your own machine.**
> Don't expose it to the internet — run the dev server / build locally and
> keep it on `localhost`.

```text
┌─────────────────────────────────────────────────────────┐
│                cron  →  daily at 06:00                  │

└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
       ┌──────────────────────────────┐
       │   recon.py (orchestrator)    │
       └─┬──────────┬────────────┬────┘
         │          │            │
   greenhouse     lever      workable      ← ATS APIs (clean JSON)
     workday      ashby      paylocity     ← (Workday/Paylocity/iCIMS also
      icims    html_scraper   static         fetch per-job descriptions for
         │          │            │           cert extraction where available)
         └────┬─────┴────────────┘
              │
              ▼
       ┌────────────────────┐
       │ verify.py          │  ← GET each apply URL, classify response
       └────────┬───────────┘
                │
                ▼
       ┌────────────────────┐
       │ recon.db (SQLite)  │  ← first_seen / last_seen / verification_status
       │ + jobs.json        │
       └────────┬───────────┘
                │
                ▼
       ┌────────────────────┐
       │ React frontend     │  ← reads jobs.json locally, renders priority
       │ (App.jsx)          │     board with NEW + Tier-A cert badges
       └────────────────────┘
```

---

## Quick start

Requires Python 3.11+ and (optionally) Node.js for the frontend. Linux/macOS.

```bash
git clone <this repo>
cd recruit_recon
./setup.sh              # creates .venv, installs deps, inits the DB,
                         # installs frontend/node_modules if npm is present

python3 recon.py --company praetorian -v   # sanity check: one company
python3 recon.py                           # full collection + verification

cd frontend && npm run dev                 # view the board at localhost:5173
```

`setup.sh` is idempotent — re-run it any time (e.g. after pulling changes
that add a new dependency). You never need to `source .venv/bin/activate`:
`recon.py` detects when it isn't running inside `.venv` and re-execs itself
there automatically, so `python3 recon.py` just works.

## Running it

```bash
python3 recon.py                       # full collection + verification + snapshot
python3 recon.py --no-verify           # skip per-job verification (faster, less reliable)
python3 recon.py --company praetorian  # collect a single company
python3 recon.py --dump-json           # write jobs.json from the existing DB, no network
python3 recon.py --init-db             # create the schema only
python3 recon.py --debug-scrape <id>   # diagnose html_scraper selectors for one company
```

A successful run looks like:

```text
2026-04-21 22:14:28 [INFO] recon — collecting: Binary Defense (adapter=paylocity)
2026-04-21 22:14:37 [INFO] recon —   binary-defense: 7 total jobs from source
2026-04-21 22:14:37 [INFO] recon —     NEW  [verified_live] Cybersecurity Incident Response Analyst — https://...
2026-04-21 22:16:05 [INFO] recon — done: 1 new, 68 updated, 8 disappeared, 0 errors
```

## Schedule it

The collector is idempotent — running it twice does nothing destructive.
This is still purely local automation (cron just runs the script on a
timer); nothing here opens a port or talks to anything but the ATS APIs.

```bash
crontab -e
# add (use your actual clone path):
0 6 * * * cd /path/to/recruit_recon && python3 recon.py >> data/recon.log 2>&1
```

`recon.py` auto-copies `data/jobs.json` → `frontend/public/jobs.json` after
every run, so the dev server always has the latest snapshot.

---

## How verification works

Every collected job's `apply_url` is fetched via HTTP GET and classified:

| Result | Meaning |
| --- | --- |
| `verified_live` | HTTP 200 and no dead-text marker in the page |
| `closed_404` / `closed_410` | HTTP 404, or 410 (Workable's hard-delete) |
| `closed_inactive_text` | HTTP 200 but the page text matches a known "this job is closed" phrase |
| `disappeared_from_api` | Missing from the ATS's list response, and re-checking its own URL couldn't confirm it's still live |
| `error` | Network failure, or an HTTP error other than 404/410 |
| `skipped` | Verification disabled, or the entry is `static` (no live URL to check) |

Dead-text markers live in `verify.py:DEAD_TEXT_MARKERS` — add more as you
find them. A job missing from an ATS's list isn't assumed dead: its own URL
gets re-checked like any other job before it's marked `disappeared_from_api`,
so a flaky or partial API response can't wrongly close a still-live posting.

The frontend buckets cards by status + cert match:

- **Tier A** — `verified_live` and mentions a cert in your `gpen_marker` list
- **Tier B** — `verified_live`, no cert match
- **Tier C** — `static` or unverified entries
- **Closed** — `closed_404`, `closed_410`, `closed_inactive_text`, `disappeared_from_api`, or `error`

---

## Certifications & Tier A matching

`gpen_marker` in `config.yaml` is the list of certs that promote a job to
Tier A — by default, the GIAC/SANS track:

```yaml
gpen_marker:
  - GFACT
  - GSEC
  - GCIH
  - GPEN
```

Leave certs you've already passed in the list as you progress — a job
mentioning a cert you hold is still a Tier A target. The badge shows the
*actual* matched cert, so a SOC role mentioning GCIH shows "GCIH", not a
generic label.

`certs_to_flag` is the broader list of certs the description-scanner looks
for and displays on a job card (`certs_mentioned`), independent of Tier A:

```yaml
certs_to_flag:
  - GFACT
  - GSEC
  - GCIH
  - GPEN
  - OSCP
  - CISSP
  - CEH
  # ... add whatever's relevant to you
```

None of this is GIAC-specific under the hood — swap `gpen_marker` for
`[OSCP]`, or `[CISSP, CEH]`, or anything else, and Tier A promotion follows
whatever list you put there.

`title_keywords` controls which job titles are considered relevant at all
(everything else is filtered out before cert matching even runs) — edit this
list to match the kind of roles you're after.

---

## Adding a new company

Edit `config.yaml`. Find the ATS token from the company's public careers page:

| ATS | Token location | Adapter config |
| --- | --- | --- |
| **Greenhouse** | `boards.greenhouse.io/{token}` or `job-boards.greenhouse.io/{token}` | `board_token: my-company` |
| **Lever** | `jobs.lever.co/{company}` | `company: my-company` |
| **Workable** | `apply.workable.com/{account}` | `account: my-company` |
| **Workday** | `{tenant}.{region}.myworkdayjobs.com/{locale}/{site_id}` | `tenant`, `region`, `site_id` |
| **Ashby** | `jobs.ashbyhq.com/{slug}` | `slug: my-company` |
| **Paylocity** | `recruiting.paylocity.com/recruiting/jobs/All/{company_id}/{company_slug}` | `company_id`, `company_slug` |
| **iCIMS** | `{tenant}.icims.com` or a custom CNAME | `tenant: my-company` OR `custom_domain: careers-co.icims.com` |

Then add an entry (Greenhouse example):

```yaml
- id: my-company
  name: My Company
  adapter: greenhouse
  config:
    board_token: my-company
```

No ATS API? Use `adapter: static` with a `static_roles` list — this puts
cards on the board linking straight to the careers page; verification still
checks that page is up. For something scrapeable, see the next section.

**The frontend never needs code changes for new companies** — it reads
`jobs.json` dynamically. Add to `config.yaml`, re-run `recon.py`, done.

## Writing a scraper for a site with no ATS

`adapters/html_scraper.py` is a generic, config-driven scraper for career
pages. If the default selectors don't match, diagnose with:

```bash
python3 recon.py --debug-scrape <company_id>
```

This prints matching CSS selector candidates, the page's headings, and
job-looking links, so you can tune selectors without guessing. Example —
Black Lantern Security uses one shared apply form for every role:

```yaml
- id: black-lantern
  adapter: html_scraper
  config:
    url: https://www.blacklanternsecurity.com/careers/
    job_selector: "div.accordion-header"
    title_selector: "self"
    shared_apply_link_selector: "a[href*='monday.com']"
```

For a prose-style page (each role is a heading followed by paragraphs, one
shared apply link for the whole page), use `mode: heading_list` instead —
see the docstring at the top of `adapters/html_scraper.py` for its options.

Need something the generic scraper can't handle? Subclass `Adapter` in a new
file under `adapters/`, implement `fetch()` to yield `Job(...)` objects, and
register it in `adapters/__init__.py`.

## Other things to customize

`config.yaml`'s `runtime:` block controls request pacing and storage:

```yaml
runtime:
  request_timeout_seconds: 15
  inter_request_delay_ms: 250       # politeness delay between requests
  verify_concurrency: 4             # bounded parallel workers for verification
                                     # and per-posting detail fetches
  purge_closed_after_days: 60       # auto-delete old closed listings; 0 disables
```

---

## License

[MIT](LICENSE)

41 companies · 9 adapters · runs in ~5 minutes end-to-end.
