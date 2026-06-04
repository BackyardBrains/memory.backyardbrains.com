#!/usr/bin/env python3
"""Daily BYB Shared Memory monitoring canary.

Runs the repeatable local checks for the 7-day monitoring window. The script
does not print raw captures or historical evidence content.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from db.engine import engine  # noqa: E402
from db.schema import Capture, Chunk, MemoryFactCard  # noqa: E402
from services.memory_api.main import MEMORY_SYSTEM, app, search_memory_records, service_metadata  # noqa: E402


DEFAULT_ARTIFACT_DIR = Path("/root/byb-memory-backfill-20260604T001145Z")
DEFAULT_USER_ID = "greg"
KNOWN_FACTS = [
    "454302771395070",
    "1537689776542289",
    "623819650",
    "943502520",
    "Woods Hole",
    "Falmouth",
    "July 20",
    "August 10",
]
DEFAULT_EXCLUDED_VISIBILITIES = {"automation_heartbeat", "operational_audit"}


def git_value(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def service_health() -> dict[str, Any]:
    metadata = service_metadata()
    return {
        "status": metadata.get("status"),
        "service": metadata.get("service"),
        "canonical_memory": metadata.get("canonical_memory"),
        "hybrid_search_enabled": metadata.get("hybrid_search_enabled"),
        "sync_capture_enabled": metadata.get("sync_capture_enabled"),
        "write_verify_enabled": metadata.get("write_verify_enabled"),
        "git_sha": metadata.get("git_sha"),
        "git_branch": metadata.get("git_branch"),
        "dirty_worktree": metadata.get("dirty_worktree"),
    }


def visibility_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        text(
            """
            SELECT coalesce(memory_visibility, 'historical_evidence') AS visibility, count(*)
            FROM memoryfactcard
            GROUP BY visibility
            ORDER BY visibility
            """
        )
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def unindexed_capture_count(session: Session, user_id: str) -> int:
    rows = session.execute(
        text(
            """
            SELECT count(*)
            FROM capture c
            WHERE c.user_id = :user_id
              AND NOT EXISTS (
                SELECT 1 FROM chunk ch
                WHERE ch.capture_id = c.id
              )
            """
        ),
        {"user_id": user_id},
    ).fetchone()
    return int(rows[0]) if rows else 0


def rejected_count_from_artifacts(artifact_dir: Path) -> int | None:
    inventory = artifact_dir / "source_inventory.csv"
    if not inventory.exists():
        return None
    with inventory.open(newline="") as handle:
        reader = csv.DictReader(handle)
        total = 0
        for row in reader:
            try:
                total += int(row.get("records_rejected") or 0)
            except ValueError:
                continue
    return total


def top_capture_ids(rows: list[dict[str, Any]], limit: int = 5) -> list[int | None]:
    return [row.get("capture_id") for row in rows[:limit]]


def run_sync_canary(user_id: str, marker: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    embedded_number = now.strftime("%Y%m%d%H%M%S")
    marker_text = marker or (
        "remember status: BYB Shared Memory daily canary "
        f"exact marker byb-canary-{embedded_number} "
        f"embedded number {embedded_number}"
    )
    verify_q = f"byb-canary-{embedded_number}"
    client = TestClient(app)
    response = client.post(
        "/v1/captures",
        params={"sync": "true", "verify_q": verify_q},
        json={"raw_content": marker_text, "source": "daily-memory-canary"},
        headers={"X-API-Key": f"sk_byb_{user_id}_daily_canary"},
    )
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    capture_id = data.get("capture_id") or data.get("id")

    with Session(engine) as session:
        exact_rows = search_memory_records(verify_q, 5, None, session, user_id, include_semantic=False)
        number_rows = search_memory_records(embedded_number, 5, None, session, user_id, include_semantic=False)

    exact_ok = bool(capture_id and any(row.get("capture_id") == capture_id for row in exact_rows[:5]))
    number_ok = bool(capture_id and any(row.get("capture_id") == capture_id for row in number_rows[:5]))
    return {
        "status_code": response.status_code,
        "capture_id": capture_id,
        "api_verified": bool(data.get("verified")),
        "verification_query": verify_q,
        "embedded_number": embedded_number,
        "exact_search_top_capture_ids": top_capture_ids(exact_rows),
        "number_search_top_capture_ids": top_capture_ids(number_rows),
        "exact_search_verified": exact_ok,
        "number_search_verified": number_ok,
        "passed": response.status_code == 200 and bool(data.get("verified")) and exact_ok and number_ok,
    }


def run_known_fact_checks(user_id: str) -> dict[str, Any]:
    checks = {}
    with Session(engine) as session:
        for fact in KNOWN_FACTS:
            rows = search_memory_records(fact, 5, None, session, user_id, include_semantic=False)
            checks[fact] = {
                "passed": bool(rows) and all(row.get("memory_system") == MEMORY_SYSTEM for row in rows[:5]),
                "top_capture_ids": top_capture_ids(rows),
                "match_types": [row.get("match_type") for row in rows[:5]],
            }
    return {
        "passed": all(item["passed"] for item in checks.values()),
        "checks": checks,
    }


def run_visibility_checks(user_id: str) -> dict[str, Any]:
    with Session(engine) as session:
        default_rows = search_memory_records("current time april", 10, None, session, user_id, include_semantic=False)
        heartbeat_rows = search_memory_records("HEARTBEAT current time April", 10, None, session, user_id, include_semantic=False)
        operational_rows = search_memory_records("Business Hours Sync cron", 10, None, session, user_id, include_semantic=False)
        counts = visibility_counts(session)
    default_visibilities = {row.get("memory_visibility") for row in default_rows if row.get("memory_visibility")}
    heartbeat_visibilities = {row.get("memory_visibility") for row in heartbeat_rows if row.get("memory_visibility")}
    operational_visibilities = {row.get("memory_visibility") for row in operational_rows if row.get("memory_visibility")}
    return {
        "counts": counts,
        "default_search_excludes_operational": not (default_visibilities & DEFAULT_EXCLUDED_VISIBILITIES),
        "default_search_visibilities": sorted(default_visibilities),
        "explicit_heartbeat_retrieves_automation": "automation_heartbeat" in heartbeat_visibilities,
        "explicit_operational_retrieves_audit": bool(operational_visibilities & DEFAULT_EXCLUDED_VISIBILITIES),
        "explicit_heartbeat_visibilities": sorted(heartbeat_visibilities),
        "explicit_operational_visibilities": sorted(operational_visibilities),
    }


def run_index_health(user_id: str, artifact_dir: Path) -> dict[str, Any]:
    with Session(engine) as session:
        unindexed = unindexed_capture_count(session, user_id)
        capture_total = session.exec(select(Capture.id).where(Capture.user_id == user_id)).all()
        chunk_total = session.exec(select(Chunk.id).where(Chunk.user_id == user_id)).all()
        card_total = session.exec(select(MemoryFactCard.id)).all()
    rejected_count = rejected_count_from_artifacts(artifact_dir)
    return {
        "capture_total": len(capture_total),
        "chunk_total": len(chunk_total),
        "fact_card_total": len(card_total),
        "unindexed_captures": unindexed,
        "historical_rejected_captures": rejected_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--marker", default=None)
    args = parser.parse_args()

    sync = run_sync_canary(args.user_id, args.marker)
    known = run_known_fact_checks(args.user_id)
    visibility = run_visibility_checks(args.user_id)
    index_health = run_index_health(args.user_id, args.artifact_dir)

    external_gates = {
        "spike_trace_result": "manual_required",
        "cortex_ui_confirmation": "manual_required",
    }
    health = service_health()
    commit_sha = git_value("rev-parse", "HEAD")
    passed = (
        sync["passed"]
        and known["passed"]
        and visibility["default_search_excludes_operational"]
        and visibility["explicit_heartbeat_retrieves_automation"]
        and visibility["explicit_operational_retrieves_audit"]
        and index_health["unindexed_captures"] == 0
        and health["status"] == "ok"
    )
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "memory_system": MEMORY_SYSTEM,
        "service_health": health,
        "status": "pass" if passed else "needs_review",
        "sync_canary": sync,
        "known_facts": known,
        "memory_visibility": visibility,
        "index_health": index_health,
        "write_verify_failures": 0 if sync["passed"] else 1,
        "external_gates": external_gates,
        "closure_note": "Do not close parent incident until 7 consecutive clean days and external gates pass.",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
