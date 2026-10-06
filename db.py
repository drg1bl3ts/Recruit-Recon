"""
SQLite persistence layer for RecruitRecon.

Schema design:
- companies: static reference data (name, adapter type)
- jobs: every role we've ever seen, with first_seen / last_seen for diff tracking
- runs: history of recon executions for audit / debugging

Diff signal: when a job appears in the API but not in the DB → INSERT.
When in both → UPDATE last_seen. When in DB but not in API → leave it,
but verification will mark it dead next time we hit its URL.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    adapter     TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                       TEXT PRIMARY KEY,        -- "{company_id}:{ats_job_id}"
    company_id               TEXT NOT NULL,
    title                    TEXT NOT NULL,
    location                 TEXT,
    url                      TEXT NOT NULL,
    apply_url                TEXT,
    description              TEXT,
    certs_mentioned          TEXT,                    -- JSON array
    gpen_explicit            INTEGER DEFAULT 0,       -- 0/1 boolean
    first_seen               TEXT NOT NULL,
    last_seen                TEXT NOT NULL,
    verification_status      TEXT,                    -- verified_live | unverified | closed_404 | closed_410 | closed_inactive_text | error
    verification_http_status INTEGER,
    verification_checked_at  TEXT,
    raw_json                 TEXT,                    -- full ATS payload for debugging
    is_static                INTEGER DEFAULT 0,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(verification_status);
CREATE INDEX IF NOT EXISTS idx_jobs_lastseen  ON jobs(last_seen);
-- composite index for the priority-board ORDER BY (gpen_explicit DESC, last_seen DESC)
CREATE INDEX IF NOT EXISTS idx_jobs_priority  ON jobs(gpen_explicit DESC, last_seen DESC);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    new_jobs      INTEGER DEFAULT 0,
    updated_jobs  INTEGER DEFAULT 0,
    closed_jobs   INTEGER DEFAULT 0,
    error_count   INTEGER DEFAULT 0,
    notes         TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Verification statuses that mean "this posting is dead" — see verify.py's
# status-code docstring for what each one means. Single source of truth for
# this module's purge and output.py's stats.closed; shipped to the frontend
# via jobs.json's stats.closed_statuses so it doesn't keep its own copy.
CLOSED_STATUSES = frozenset({
    "closed_404",
    "closed_410",
    "closed_inactive_text",
    "closed_unposted",
    "disappeared_from_api",
    "error",
})


@contextmanager
def connect(db_path: str):
    """Context manager for SQLite connections with sane defaults."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Create tables if they don't exist."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def upsert_company(conn, company: dict) -> None:
    conn.execute(
        """
        INSERT INTO companies (id, name, adapter, config_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            adapter = excluded.adapter,
            config_json = excluded.config_json
        """,
        (
            company["id"],
            company["name"],
            company["adapter"],
            json.dumps(company.get("config", {})),
        ),
    )


def upsert_job(conn, job: dict) -> tuple[bool, bool]:
    """
    Insert-or-update a job. Returns (was_new, was_updated).
    Sets first_seen on insert, always updates last_seen.
    """
    now = now_iso()
    existing = conn.execute(
        "SELECT id FROM jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    was_new = existing is None

    conn.execute(
        """
        INSERT INTO jobs (
            id, company_id, title, location, url, apply_url, description,
            certs_mentioned, gpen_explicit, first_seen, last_seen,
            verification_status, verification_http_status,
            verification_checked_at, raw_json, is_static
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            location = excluded.location,
            url = excluded.url,
            apply_url = excluded.apply_url,
            description = excluded.description,
            certs_mentioned = excluded.certs_mentioned,
            gpen_explicit = excluded.gpen_explicit,
            last_seen = excluded.last_seen,
            verification_status = excluded.verification_status,
            verification_http_status = excluded.verification_http_status,
            verification_checked_at = excluded.verification_checked_at,
            raw_json = excluded.raw_json,
            is_static = excluded.is_static
        """,
        (
            job["id"],
            job["company_id"],
            job["title"],
            job.get("location"),
            job["url"],
            job.get("apply_url"),
            job.get("description"),
            json.dumps(job.get("certs_mentioned", [])),
            1 if job.get("gpen_explicit") else 0,
            now,
            now,
            job.get("verification_status"),
            job.get("verification_http_status"),
            job.get("verification_checked_at"),
            json.dumps(job.get("raw")) if job.get("raw") is not None else None,
            1 if job.get("is_static") else 0,
        ),
    )
    return was_new, not was_new


def find_stale_jobs(conn, company_id: str, seen_ids: Iterable[str]) -> list[dict]:
    """
    Return jobs for this company that are in the DB but weren't in this run's
    API response — candidates that MIGHT be gone.

    The caller should re-verify each one's own URL (see verify.verify_url)
    before concluding it's actually dead: the source's list API can have a
    transient or partial miss (e.g. a flaky page 2 of pagination) without
    erroring, which would otherwise wrongly close a still-live posting.
    """
    seen = set(seen_ids)
    rows = conn.execute(
        "SELECT id, url, apply_url FROM jobs WHERE company_id = ?", (company_id,)
    ).fetchall()
    return [dict(r) for r in rows if r["id"] not in seen]


def update_job_verification(conn, job_id: str, status: str, http_status, checked_at: str) -> None:
    """Update just a job's verification fields, e.g. after re-checking one
    that disappeared from the source API's list (see find_stale_jobs)."""
    conn.execute(
        "UPDATE jobs SET verification_status = ?, verification_http_status = ?, "
        "verification_checked_at = ? WHERE id = ?",
        (status, http_status, checked_at, job_id),
    )


def mark_disappeared_jobs(conn, stale_ids: Iterable[str], run_started_at: str) -> int:
    """
    Directly mark jobs as disappeared without re-verifying their URL — the
    fallback for when verification is disabled (--no-verify) and there's no
    other way to tell whether a stale job is actually gone. Prefer
    find_stale_jobs + a real verify_url check when verification is on.
    Returns count of jobs marked.
    """
    stale = list(stale_ids)
    if not stale:
        return 0
    placeholders = ",".join("?" * len(stale))
    conn.execute(
        f"UPDATE jobs SET verification_status = 'disappeared_from_api', "
        f"verification_checked_at = ? WHERE id IN ({placeholders})",
        [run_started_at, *stale],
    )
    return len(stale)


def all_active_jobs(conn) -> list[dict]:
    """Return every job for the JSON snapshot (frontend will filter)."""
    rows = conn.execute(
        """
        SELECT j.*, c.name AS company_name, c.adapter AS company_adapter
        FROM jobs j
        JOIN companies c ON j.company_id = c.id
        ORDER BY j.gpen_explicit DESC, j.last_seen DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "company_id": r["company_id"],
            "company_name": r["company_name"],
            "title": r["title"],
            "location": r["location"],
            "url": r["url"],
            "apply_url": r["apply_url"],
            "certs_mentioned": json.loads(r["certs_mentioned"] or "[]"),
            "gpen_explicit": bool(r["gpen_explicit"]),
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "verification": {
                "status": r["verification_status"],
                "http_status": r["verification_http_status"],
                "checked_at": r["verification_checked_at"],
            },
            "is_static": bool(r["is_static"]),
        })
    return out


def start_run(conn) -> tuple[int, str]:
    """Insert a new run record. Returns (run_id, started_at) so callers use the
    same timestamp that was written to the DB — avoids a microsecond skew between
    the DB row and the in-memory threshold used for NEW-badge computation."""
    now = now_iso()
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now,))
    return cur.lastrowid, now


def last_run_started_at(conn) -> Optional[str]:
    """
    Return the started_at timestamp of the most recent run, or None if the
    runs table is empty. Used to compute per-job 'is this new this run?'
    flags when writing the JSON snapshot.
    """
    row = conn.execute(
        "SELECT started_at FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["started_at"] if row else None


def finish_run(conn, run_id: int, *, new_jobs: int, updated_jobs: int,
               closed_jobs: int, error_count: int, notes: str = "") -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, new_jobs = ?, updated_jobs = ?,
                       closed_jobs = ?, error_count = ?, notes = ?
        WHERE id = ?
        """,
        (now_iso(), new_jobs, updated_jobs, closed_jobs, error_count, notes, run_id),
    )


def purge_old_closed_jobs(conn, days: int = 60) -> int:
    """Delete closed/disappeared jobs whose last_seen is older than `days` days.
    Keeps the DB from growing unboundedly with stale dead listings.
    Returns the number of rows deleted."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    placeholders = ",".join("?" * len(CLOSED_STATUSES))
    cur = conn.execute(
        f"""
        DELETE FROM jobs
        WHERE verification_status IN ({placeholders})
        AND last_seen < ?
        """,
        (*CLOSED_STATUSES, cutoff),
    )
    return cur.rowcount
