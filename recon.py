#!/usr/bin/env python3
"""
RecruitRecon — pen test job board collector.

Usage:
    python recon.py                       # full collection + verification + snapshot
    python recon.py --no-verify           # skip per-job verification (faster, less reliable)
    python recon.py --company praetorian  # collect a single company
    python recon.py --init-db             # create schema only
    python recon.py --dump-json           # write jobs.json from existing DB without re-collecting

Exit codes:
    0 — success
    1 — config / fatal error
    2 — partial failure (some companies failed), or snapshot aborted because
        too many sources failed (jobs.json left untouched)
"""

import os
import sys
from pathlib import Path


def _ensure_venv() -> None:
    """
    Re-exec this script under .venv's own Python if we're not already running
    there, so `python3 recon.py` just works after `./setup.sh` — no need to
    `source .venv/bin/activate` every session.

    Only stdlib is used here deliberately: this runs *before* the
    third-party imports below, so it works even when invoked with a bare
    system `python3` that doesn't have requests/yaml/bs4 installed.

    Set RECRUITRECON_SKIP_VENV=1 to bypass (e.g. you manage deps yourself,
    or you're already inside a container with them preinstalled).
    """
    if os.environ.get("RECRUITRECON_SKIP_VENV"):
        return
    venv_dir = Path(__file__).resolve().parent / ".venv"
    venv_python = venv_dir / "bin" / "python3"
    if not venv_python.exists():
        return  # no venv yet — run ./setup.sh, or deps are installed elsewhere
    # Compare sys.prefix, not sys.executable — venvs are commonly symlinks to
    # the system Python binary, so resolving symlinks on sys.executable would
    # collapse it to the same file either way and this check would never
    # trigger. sys.prefix correctly reflects whether the venv's isolated
    # site-packages are actually active, regardless of the binary's identity.
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return  # already running with this venv active
    os.execv(str(venv_python), [str(venv_python), *sys.argv])


_ensure_venv()

import argparse
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
    import yaml

    import db
    import filters
    import output
    import verify
    from adapters import get_adapter  # imports bs4 transitively (html_scraper.py)
except ImportError:
    sys.exit(
        "Missing dependencies. Run ./setup.sh once (creates .venv and installs "
        "requirements.txt), then re-run this command."
    )

# One requests.Session per verification worker thread. A Session's cookie jar
# isn't safe for concurrent read-modify-write, so the ThreadPoolExecutor
# workers in run() must not share a single Session with each other or with
# the adapter's own (main-thread) session — see adapters/base.py's
# Adapter._get_session for the same pattern applied to adapter detail fetches.
_verify_session_local = threading.local()


def _verify_session() -> requests.Session:
    if not hasattr(_verify_session_local, "session"):
        _verify_session_local.session = requests.Session()
    return _verify_session_local.session


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(cfg: dict, *, only_company: str = None, verify_pages: bool = True) -> tuple[int, int, int, int, int]:
    """
    Returns (new_jobs, updated_jobs, closed_jobs, error_count, total_companies).
    total_companies counts companies matching `only_company` (or all, if unset) —
    used by main() as the denominator for the partial-failure circuit breaker.

    Assumes db.init_db() has already been called by the caller (main()).
    """
    log = logging.getLogger("recon")

    runtime = cfg.get("runtime", {})
    db_path = runtime.get("database_path", "data/recon.db")
    user_agent = runtime.get("user_agent", "RecruitRecon/0.1")
    timeout = runtime.get("request_timeout_seconds", 15)
    delay_ms = runtime.get("inter_request_delay_ms", 250)
    # Bounded worker count for per-job verification (within one company) and
    # for adapters' own per-posting detail fetches. Each worker still paces
    # itself with delay_ms/detail_delay_ms, so this trades wall-clock time for
    # request concurrency without removing the per-request politeness delay.
    verify_concurrency = int(runtime.get("verify_concurrency", 4))

    title_keywords = cfg.get("title_keywords", [])
    cert_list = cfg.get("certs_to_flag", [])
    gpen_marker = cfg.get("gpen_marker", "GPEN")

    new_jobs = updated_jobs = closed_jobs = error_count = total_companies = 0

    with db.connect(db_path) as conn:
        # start_run returns the same timestamp it wrote to the DB so there's no
        # microsecond skew between the runs.started_at row and the in-memory
        # threshold used to compute first_seen_at_run / NEW badges.
        run_id, run_started = db.start_run(conn)

        for company in cfg.get("companies", []):
            cid = company["id"]
            if only_company and cid != only_company:
                continue
            total_companies += 1

            log.info("=" * 60)
            log.info("collecting: %s (adapter=%s)", company["name"], company["adapter"])

            db.upsert_company(conn, company)

            try:
                AdapterCls = get_adapter(company["adapter"])
            except KeyError as e:
                log.error("%s — %s", cid, e)
                error_count += 1
                continue

            adapter = AdapterCls(
                company_id=cid,
                company_name=company["name"],
                config=company.get("config", {}),
                user_agent=user_agent,
                timeout=timeout,
            )

            try:
                jobs = list(adapter.fetch())
            except Exception as e:
                log.exception("%s adapter failed: %s", cid, e)
                error_count += 1
                continue

            # Adapters swallow their own network exceptions and yield nothing,
            # so an unreachable source and a genuinely empty board both come
            # back as an empty list — had_errors is the explicit signal that
            # distinguishes them (set by Adapter._request on RequestException).
            fetch_failed = adapter.had_errors
            if fetch_failed:
                error_count += 1
                log.error("  %s: fetch error(s) — skipping disappearance check", cid)

            log.info("  %s: %d total jobs from source", cid, len(jobs))

            # Pass 1 (cheap, sequential): title filter + cert extraction.
            candidates = []
            for job in jobs:
                # Filter by title relevance — but not for static entries
                # (the user already vetted those when adding to config)
                if not adapter.is_static:
                    if not filters.title_matches(job.title, title_keywords):
                        log.debug("    skip (title): %s", job.title)
                        continue

                desc_text = filters.strip_html(job.description or "")
                certs_found = filters.extract_certs(desc_text, cert_list)
                gpen_explicit = filters.has_gpen(certs_found, gpen_marker)
                candidates.append((job, desc_text, certs_found, gpen_explicit))

            # Pass 2: verification. Independent per job, so it's the real
            # serial cost for companies with many postings — parallelize with
            # a small bounded pool. Each worker gets its own Session (see
            # _verify_session) so it still reuses connections across the jobs
            # it personally handles. executor.map preserves input/output
            # order, so results line up positionally with `candidates` for
            # pass 3.
            if not verify_pages:
                verifications = [
                    {"status": "skipped", "http_status": None, "checked_at": db.now_iso()}
                    for _ in candidates
                ]
            elif candidates:
                def _verify(item):
                    job = item[0]
                    return verify.verify_url(
                        job.url or job.apply_url,
                        user_agent=user_agent,
                        timeout=timeout,
                        delay_ms=delay_ms,
                        session=_verify_session(),
                    )

                with ThreadPoolExecutor(max_workers=verify_concurrency) as pool:
                    verifications = list(pool.map(_verify, candidates))
            else:
                verifications = []

            # Pass 3 (sequential — sqlite3 connections aren't thread-safe):
            # build rows and upsert.
            seen_ids = []
            for (job, desc_text, certs_found, gpen_explicit), v in zip(candidates, verifications):
                row = {
                    "id": f"{cid}:{job.id}",
                    "company_id": cid,
                    "title": job.title,
                    "location": job.location,
                    "url": job.url,
                    "apply_url": job.apply_url or job.url,
                    "description": desc_text[:5000],  # cap stored description size
                    "certs_mentioned": certs_found,
                    "gpen_explicit": gpen_explicit,
                    "verification_status": v["status"],
                    "verification_http_status": v["http_status"],
                    "verification_checked_at": v["checked_at"],
                    "raw": job.raw,
                    "is_static": adapter.is_static,
                }

                was_new, was_updated = db.upsert_job(conn, row)
                seen_ids.append(row["id"])

                if was_new:
                    new_jobs += 1
                    log.info("    NEW  [%s] %s — %s",
                             v["status"], job.title, job.url[:80])
                else:
                    updated_jobs += 1
                    log.debug("    seen [%s] %s", v["status"], job.title)

            # For ATS adapters: jobs we previously saw but didn't see this run.
            # Called even when seen_ids is empty — if ALL jobs were filtered out
            # by title, existing DB rows should still be checked rather than
            # left as perpetually "verified_live".
            #
            # NEVER called when the fetch errored: an unreachable source returns
            # zero jobs, which would otherwise mark every posting for that
            # company as closed. See the 2026-07-23 run (251 false closures).
            if not adapter.is_static and not fetch_failed:
                stale = db.find_stale_jobs(conn, cid, seen_ids)
                if stale:
                    if verify_pages:
                        # Re-check each job's own URL rather than assuming the
                        # source API's miss means it's gone — a transient or
                        # partial list response (e.g. a flaky paginated page)
                        # shouldn't be able to wrongly close a still-live
                        # posting for up to purge_closed_after_days.
                        def _verify_stale(j):
                            return j, verify.verify_url(
                                j["apply_url"] or j["url"],
                                user_agent=user_agent,
                                timeout=timeout,
                                delay_ms=delay_ms,
                                session=_verify_session(),
                            )

                        with ThreadPoolExecutor(max_workers=verify_concurrency) as pool:
                            results = list(pool.map(_verify_stale, stale))

                        still_live = 0
                        for j, v in results:
                            # "skipped" (e.g. a job with no stored URL) can't
                            # confirm liveness either way — treat it as gone
                            # rather than mislabeling it "static/unverified".
                            status = v["status"] if v["status"] != "skipped" else "disappeared_from_api"
                            db.update_job_verification(conn, j["id"], status, v["http_status"], v["checked_at"])
                            if status == "verified_live":
                                still_live += 1
                        closed_now = len(stale) - still_live
                        log.info("  %s: %d jobs missing from source API, re-verified "
                                  "(%d still live, %d closed)", cid, len(stale), still_live, closed_now)
                        closed_jobs += closed_now
                    else:
                        # Can't re-check without verification enabled — fall
                        # back to marking them disappeared directly so
                        # --no-verify runs still track removal.
                        n = db.mark_disappeared_jobs(conn, [j["id"] for j in stale], run_started)
                        log.info("  %s: %d jobs disappeared from API since last run", cid, n)
                        closed_jobs += n

        db.finish_run(
            conn, run_id,
            new_jobs=new_jobs, updated_jobs=updated_jobs,
            closed_jobs=closed_jobs, error_count=error_count,
            notes="",
        )

    log.info("=" * 60)
    log.info("done: %d new, %d updated, %d disappeared, %d errors",
             new_jobs, updated_jobs, closed_jobs, error_count)
    return new_jobs, updated_jobs, closed_jobs, error_count, total_companies


def dump_json(cfg: dict, run_meta: dict = None) -> None:
    """
    Write jobs.json from current DB state. Pass run_meta to populate stats.
    Assumes db.init_db() has already been called by the caller (main()).
    """
    log = logging.getLogger("recon")
    runtime = cfg.get("runtime", {})
    db_path = runtime.get("database_path", "data/recon.db")
    json_path = runtime.get("output_json_path", "data/jobs.json")
    gpen_marker = cfg.get("gpen_marker", "GPEN")
    purge_days = runtime.get("purge_closed_after_days", 60)

    with db.connect(db_path) as conn:
        # Prune dead listings older than purge_closed_after_days (default 60d)
        # before writing the snapshot so they never appear in the frontend.
        if purge_days:
            purged = db.purge_old_closed_jobs(conn, days=int(purge_days))
            if purged:
                log.info("purged %d old closed jobs (>%dd)", purged, purge_days)

        jobs = db.all_active_jobs(conn)
        # ISO8601 timestamps sort lexicographically — output.py uses string compare.
        threshold = db.last_run_started_at(conn)

    if run_meta is None:
        run_meta = {
            "started_at": None, "finished_at": None,
            "new_jobs": 0, "updated_jobs": 0, "closed_jobs": 0, "errors": 0,
        }
    output.write_snapshot(
        json_path,
        jobs=jobs,
        run_meta=run_meta,
        run_started_at=threshold,
        gpen_marker=gpen_marker,
    )
    log.info("wrote %d jobs to %s", len(jobs), json_path)

    # Auto-sync to the Vite dev-server location so the frontend picks up new
    # data without a manual copy step.  Derives path relative to jobs.json.
    frontend_dst = Path(json_path).parent.parent / "frontend" / "public" / "jobs.json"
    if frontend_dst.parent.exists():
        shutil.copy2(json_path, frontend_dst)
        log.info("synced snapshot → %s", frontend_dst)


def main() -> int:
    parser = argparse.ArgumentParser(description="RecruitRecon job collector")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--company", help="collect only this company id")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip per-job company-page verification")
    parser.add_argument("--init-db", action="store_true",
                        help="create database schema only")
    parser.add_argument("--dump-json", action="store_true",
                        help="write jobs.json from existing DB without collecting")
    parser.add_argument("--debug-scrape", metavar="COMPANY_ID",
                        help="diagnose html_scraper selectors for one company")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("recon")

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error("config not found: %s", cfg_path)
        return 1

    cfg = load_config(args.config)
    runtime = cfg.get("runtime", {})

    if args.init_db:
        db.init_db(runtime.get("database_path", "data/recon.db"))
        log.info("schema initialized at %s", runtime.get("database_path"))
        return 0

    if args.debug_scrape:
        from adapters.html_scraper import debug_scrape
        target = args.debug_scrape
        company = next((c for c in cfg["companies"] if c["id"] == target), None)
        if not company:
            log.error("company id not found in config: %s", target)
            return 1
        debug_scrape(
            target,
            company.get("config", {}),
            user_agent=runtime.get("user_agent", "RecruitRecon/0.1"),
            timeout=runtime.get("request_timeout_seconds", 15),
        )
        return 0

    db.init_db(runtime.get("database_path", "data/recon.db"))

    if args.dump_json:
        dump_json(cfg)
        return 0

    run_start = db.now_iso()
    new, upd, closed, errs, total = run(
        cfg,
        only_company=args.company,
        verify_pages=runtime.get("verify_company_page", True) and not args.no_verify,
    )

    # Circuit breaker: a broad outage (no DNS on resume, VPN flap, ISP blip)
    # fails most sources at once. Writing the snapshot in that state would
    # publish a near-empty board to the frontend. Leave jobs.json alone and
    # let the previous good snapshot stand.
    if total and errs >= max(1, total // 2):
        log.error("aborting snapshot: %d/%d sources failed — jobs.json untouched",
                  errs, total)
        return 2

    # Otherwise write the snapshot, even on partial failure.
    dump_json(cfg, run_meta={
        "started_at": run_start, "finished_at": db.now_iso(),
        "new_jobs": new, "updated_jobs": upd,
        "closed_jobs": closed, "errors": errs,
    })

    return 0 if errs == 0 else 2


if __name__ == "__main__":
    sys.exit(main())