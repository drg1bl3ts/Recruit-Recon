"""
Write the jobs.json snapshot the React frontend consumes.

The frontend (PriorityBoard.jsx) does:
    const r = await fetch('./jobs.json');
    const data = await r.json();

So this file IS the contract. Don't change the schema without updating both ends.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import filters
from db import CLOSED_STATUSES


def write_snapshot(
    path: str,
    *,
    jobs: list[dict],
    run_meta: dict,
    run_started_at: Optional[str] = None,
    gpen_marker: Union[str, list, None] = None,
) -> None:
    """
    Write jobs.json. Schema:

    {
      "generated_at": ISO8601,
      "stats": {
        "total": int,
        "verified_live": int,
        "gpen_match": int,
        "closed": int,
        "static": int,
        "new_jobs_this_run": int,
        "closed_statuses": [str]
      },
      "run": {
        "started_at": ISO8601,
        "finished_at": ISO8601,
        "errors": int
      },
      "jobs": [Job {..., "first_seen_at_run": bool}]
    }

    If run_started_at is provided, each job gets a `first_seen_at_run` boolean
    indicating whether its first_seen >= run_started_at (i.e. "new this run").
    ISO8601 timestamps sort lexicographically, so string comparison is safe.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    # Build annotated copies — don't mutate the input list so callers can safely
    # reuse their job dicts (e.g. for logging or a second snapshot write).
    # Tally the summary stats in the same pass instead of five separate O(n)
    # scans over the annotated list afterward.
    annotated: list[dict] = []
    verified_live = closed = gpen = static_count = new_this_run = 0
    for j in jobs:
        aj = {**j}  # shallow copy; nested dicts (verification, etc.) are read-only here

        aj["first_seen_at_run"] = bool(
            run_started_at and aj.get("first_seen") and aj["first_seen"] >= run_started_at
        )
        aj["tier_a_certs"] = (
            filters.matched_tier_a_certs(aj.get("certs_mentioned") or [], gpen_marker)
            if gpen_marker is not None
            else []
        )
        annotated.append(aj)

        status = aj["verification"]["status"]
        if status == "verified_live":
            verified_live += 1
        elif status in CLOSED_STATUSES:
            closed += 1
        if aj["gpen_explicit"]:
            gpen += 1
        if aj["is_static"]:
            static_count += 1
        if aj["first_seen_at_run"]:
            new_this_run += 1

    jobs = annotated  # shadow the parameter — only the local snapshot uses this

    # Include the threshold in run_meta so the frontend can show it if desired
    run_meta = {**run_meta, "threshold_started_at": run_started_at}

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            "total": len(jobs),
            "verified_live": verified_live,
            "gpen_match": gpen,
            "closed": closed,
            "static": static_count,
            # Use the per-job computation rather than run_meta["new_jobs"]
            # because it's a richer signal — it counts DB inserts in this run,
            # which is what the frontend actually needs to badge.
            "new_jobs_this_run": new_this_run,
            # Shipped so the frontend buckets "closed / disappeared" using the
            # same status set as this stat, instead of keeping its own copy
            # that can silently drift out of sync.
            "closed_statuses": sorted(CLOSED_STATUSES),
        },
        "run": run_meta,
        "jobs": jobs,
    }

    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
