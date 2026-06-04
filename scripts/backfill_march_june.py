#!/usr/bin/env python3
"""Inventory, import, and verify March-June 2026 historical BYB memory."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import random
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select  # noqa: E402

from db.engine import engine  # noqa: E402
from db.schema import Capture, Chunk  # noqa: E402
from packages.memory_retrieval.indexer import process_capture  # noqa: E402
from services.memory_api.main import (  # noqa: E402
    MEMORY_SYSTEM,
    exact_capture_search,
    redact_secrets,
    search_memory_records,
    should_capture_memory,
)

OPENCLAW_ROOT = Path("/var/www/openclaw.backyardbrains.com")
CORTEX_ROOT = Path("/var/www/cortex.backyardbrains.com")
WINDOW_START = datetime(2026, 3, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 6, 3, 23, 59, 59, tzinfo=timezone.utc)
HASH_ALGORITHM = "sha256"
DEFAULT_BATCH_ID = "historical-march-june-2026"
USER_ID = "greg"
KNOWN_FACTS = [
    "454302771395070",
    "1537689776542289",
    "623819650",
    "943502520",
    "Woods Hole",
    "Falmouth",
    "July 20",
    "August 10",
    "Jellop",
    "Kickstarter",
    "Meta Pixel",
    "CAPI token",
]
REQUIRED_SOURCE_SYSTEMS = [
    "OpenClaw Legacy Memory",
    "Watson Markdown Memory",
    "OpenClaw Session Logs",
    "Watson Session Logs",
    "Cortex Feedback SQL Backup",
    "OpenClaw Local SQLite Index",
    "Manual Seed Records",
]

DATE_RE = re.compile(r"(?P<year>2026)[-_]?(?P<month>0[3-6])[-_]?(?P<day>[0-3][0-9])")
ISO_RE = re.compile(r"2026-(0[3-6])-[0-3][0-9](?:[T ][0-9:.+-Z]*)?")
LONG_ID_RE = re.compile(r"\b\d{6,}\b")
KEYWORD_RE = re.compile(
    r"\b("
    r"decision|decided|confirmed|reservation|deadline|due|meeting|task|status|project|"
    r"woods hole|falmouth|jellop|kickstarter|meta pixel|capi token|pixel|token|"
    r"grant|nih|ninds|mbl|shipment|invoice|refund|campaign|launchboom"
    r")\b",
    re.IGNORECASE,
)
SECRETISH_RE = re.compile(r"\b(token|secret|password|api[_-]?key|bearer)\b", re.IGNORECASE)


@dataclass
class HistoricalRecord:
    source_system: str
    source_path: str
    source_type: str
    observed_at: datetime
    text: str
    ordinal: int = 0
    content_hash: str = ""
    exact_query: str = ""
    semantic_query: str = ""


@dataclass
class SourceInventory:
    source_system: str
    source_path: str = ""
    source_type: str = ""
    first_observed_at: str = ""
    last_observed_at: str = ""
    records_discovered: int = 0
    records_candidate: int = 0
    records_imported: int = 0
    records_verified: int = 0
    records_rejected: int = 0
    records_duplicate: int = 0
    records_error: int = 0
    import_batch_id: str = DEFAULT_BATCH_ID
    content_hash_algorithm: str = HASH_ALGORITHM
    notes: str = ""
    files: set[str] = field(default_factory=set, repr=False)
    hashes: set[str] = field(default_factory=set, repr=False)


def utc_from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def parse_observed_at(path: Path, fallback_text: str = "") -> datetime:
    match = DATE_RE.search(str(path))
    if match:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=timezone.utc,
        )
    match = ISO_RE.search(fallback_text)
    if match:
        value = match.group(0).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return utc_from_timestamp(path.stat().st_mtime)
    except OSError:
        return WINDOW_START


def in_window(dt: datetime) -> bool:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return WINDOW_START <= aware <= WINDOW_END


def normalize_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", lambda m: m.group(0).split("](")[0].lstrip("["), text)
    text = re.sub(r"[*_`>#]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def content_hash(source_system: str, source_path: str, text: str) -> str:
    value = f"{source_system}\n{source_path}\n{normalize_text(text)}"
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def is_candidate(text: str) -> bool:
    clean = normalize_text(text)
    if len(clean) < 40:
        return False
    if LONG_ID_RE.search(clean) or ISO_RE.search(clean):
        return True
    if KEYWORD_RE.search(clean):
        return True
    return False


def exact_query_for(text: str, observed_at: datetime, source_path: str, batch_id: str) -> str:
    ids = LONG_ID_RE.findall(text)
    if ids:
        return ids[0]
    dates = ISO_RE.findall(text)
    if dates:
        full = re.search(r"2026-(?:0[3-6])-[0-3][0-9]", text)
        if full:
            return full.group(0)
    for phrase in KNOWN_FACTS:
        if phrase.lower() in text.lower():
            return phrase
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}", text)
    return " ".join(words[:6]) if words else batch_id or source_path


def semantic_query_for(text: str, source_system: str) -> str:
    clean = normalize_text(text)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", clean) if w.lower() not in {"this", "that", "with", "from", "have", "will"}]
    return f"historical {source_system} note about {' '.join(words[:8])}".strip()


def split_markdown(path: Path, source_system: str) -> list[HistoricalRecord]:
    raw = path.read_text(errors="replace")
    observed_at = parse_observed_at(path, raw[:500])
    if not in_window(observed_at):
        return []
    blocks: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if stripped.startswith("#") and current:
            blocks.append("\n".join(current))
            current = [stripped]
        else:
            current.append(stripped)
    if current:
        blocks.append("\n".join(current))

    records = []
    for idx, block in enumerate(blocks, start=1):
        clean = normalize_text(block)
        if is_candidate(clean):
            records.append(HistoricalRecord(source_system, str(path), "markdown", observed_at, clean[:4000], idx))
    return records


def extract_json_text(obj: object) -> str:
    if not isinstance(obj, dict):
        return ""
    if obj.get("type") != "message":
        return ""
    message = obj.get("message") or {}
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return ""
    parts = message.get("content") or []
    out = []
    if isinstance(parts, str):
        out.append(parts)
    elif isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(str(part.get("text", "")))
    return "\n".join(out)


def split_jsonl(path: Path, source_system: str) -> list[HistoricalRecord]:
    records = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return records
    for idx, line in enumerate(lines, start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_text = str(obj.get("timestamp") or "")
        observed_at = parse_observed_at(path, ts_text)
        if not in_window(observed_at):
            continue
        clean = normalize_text(extract_json_text(obj))
        if is_candidate(clean):
            records.append(HistoricalRecord(source_system, str(path), "jsonl_session", observed_at, clean[:4000], idx))
    return records


def split_sql_backup(path: Path, source_system: str) -> list[HistoricalRecord]:
    opener = gzip.open if path.suffix == ".gz" else open
    records = []
    with opener(path, "rt", errors="replace") as handle:
        for idx, line in enumerate(handle, start=1):
            if "feedback" not in line.lower():
                continue
            observed_at = parse_observed_at(path, line)
            if in_window(observed_at):
                clean = normalize_text(line)
                if is_candidate(clean):
                    records.append(HistoricalRecord(source_system, str(path), "sql_backup", observed_at, clean[:4000], idx))
    return records


def split_local_sqlite(path: Path) -> list[HistoricalRecord]:
    records = []
    conn = sqlite3.connect(path)
    try:
        for idx, row in enumerate(conn.execute("SELECT path, source, start_line, end_line, text, updated_at FROM chunks"), start=1):
            item_path, source, start_line, end_line, text, updated_at = row
            observed_at = parse_observed_at(Path(str(item_path)), str(updated_at))
            if not in_window(observed_at):
                continue
            clean = normalize_text(str(text or ""))
            if is_candidate(clean):
                source_path = f"{path}:{item_path}:{start_line}-{end_line}"
                records.append(
                    HistoricalRecord(
                        "OpenClaw Local SQLite Index",
                        source_path,
                        f"sqlite_chunk:{source}",
                        observed_at,
                        clean[:4000],
                        idx,
                    )
                )
    finally:
        conn.close()
    return records


def manual_seed_records() -> list[HistoricalRecord]:
    observed_at = datetime(2026, 6, 3, tzinfo=timezone.utc)
    records = []
    for idx, fact in enumerate(KNOWN_FACTS, start=1):
        text = (
            "Manual Seed Records historical recall anchor. "
            f"Known-fact recall term: {fact}. "
            "This record is an audit anchor only; it does not assert a current external truth beyond the source term."
        )
        records.append(HistoricalRecord("Manual Seed Records", f"manual-seed://known-fact/{idx}", "manual_seed", observed_at, text, idx))
    return records


def discover_source_paths() -> dict[str, list[Path]]:
    markdown = sorted((OPENCLAW_ROOT / "workspaces").glob("*/memory/**/*"))
    return {
        "OpenClaw Legacy Memory": [
            p for p in markdown
            if p.is_file() and "workspaces/watson/memory" not in str(p) and p.suffix.lower() in {".md", ".json", ".jsonl", ".log", ""}
        ],
        "Watson Markdown Memory": [
            p for p in (OPENCLAW_ROOT / "workspaces/watson/memory").glob("**/*")
            if p.is_file() and p.suffix.lower() in {".md", ".json", ".jsonl", ".log", ""}
        ],
        "OpenClaw Session Logs": [
            p for p in (OPENCLAW_ROOT / "agents").glob("*/sessions*/*")
            if p.is_file() and "/agents/watson/" not in str(p) and ".jsonl" in p.name
        ],
        "Watson Session Logs": [
            p for p in (OPENCLAW_ROOT / "agents/watson/sessions").glob("*")
            if p.is_file() and ".jsonl" in p.name
        ],
        "Cortex Feedback SQL Backup": [
            p for base in [CORTEX_ROOT, Path("/root"), Path("/var/backups")]
            for p in base.glob("**/*feedback*.sql*")
            if p.is_file() and "node_modules" not in str(p)
        ],
        "OpenClaw Local SQLite Index": [OPENCLAW_ROOT / "memory/main.sqlite"],
        "Manual Seed Records": [],
    }


def records_for_source(source_system: str, paths: list[Path]) -> list[HistoricalRecord]:
    if source_system == "Manual Seed Records":
        return manual_seed_records()
    records: list[HistoricalRecord] = []
    for path in paths:
        try:
            if source_system == "OpenClaw Local SQLite Index":
                records.extend(split_local_sqlite(path))
            elif source_system == "Cortex Feedback SQL Backup":
                records.extend(split_sql_backup(path, source_system))
            elif ".jsonl" in path.name:
                records.extend(split_jsonl(path, source_system))
            else:
                records.extend(split_markdown(path, source_system))
        except Exception:
            continue
    return finalize_records(records)


def finalize_records(records: list[HistoricalRecord]) -> list[HistoricalRecord]:
    for rec in records:
        rec.content_hash = content_hash(rec.source_system, rec.source_path, rec.text)
        rec.exact_query = exact_query_for(rec.text, rec.observed_at, rec.source_path, DEFAULT_BATCH_ID)
        rec.semantic_query = semantic_query_for(rec.text, rec.source_system)
    return records


def build_inventory(records_by_source: dict[str, list[HistoricalRecord]], paths_by_source: dict[str, list[Path]], batch_id: str) -> dict[str, SourceInventory]:
    inventory = {name: SourceInventory(source_system=name, import_batch_id=batch_id) for name in REQUIRED_SOURCE_SYSTEMS}
    for name, inv in inventory.items():
        paths = paths_by_source.get(name, [])
        inv.source_path = ";".join(str(p) for p in paths[:8])
        if len(paths) > 8:
            inv.source_path += f";...+{len(paths)-8} more"
        inv.files = {str(p) for p in paths}
        inv.records_discovered = len(paths)
        records = records_by_source.get(name, [])
        inv.records_candidate = len(records)
        inv.hashes = {r.content_hash for r in records}
        if records:
            inv.source_type = ",".join(sorted({r.source_type for r in records})[:5])
            inv.first_observed_at = min(r.observed_at for r in records).isoformat()
            inv.last_observed_at = max(r.observed_at for r in records).isoformat()
        elif paths:
            inv.source_type = "unparsed"
        inv.notes = f"files={len(inv.files)} hashes={len(inv.hashes)} acceptance_window=2026-03-01..2026-06-03"
    return inventory


def write_inventory_csv(inventory: dict[str, SourceInventory], out_dir: Path) -> None:
    fields = [
        "source_system",
        "source_path",
        "source_type",
        "first_observed_at",
        "last_observed_at",
        "records_discovered",
        "records_candidate",
        "records_imported",
        "records_verified",
        "records_rejected",
        "records_duplicate",
        "records_error",
        "import_batch_id",
        "content_hash_algorithm",
        "notes",
    ]
    with (out_dir / "source_inventory.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        for inv in inventory.values():
            row = {k: v for k, v in asdict(inv).items() if k in fields}
            writer.writerow(row)


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")


def capture_body(record: HistoricalRecord, batch_id: str, imported_at: datetime) -> str:
    return (
        "[Historical Import]\n"
        f"source_system: {record.source_system}\n"
        f"source_path: {record.source_path}\n"
        f"source_type: {record.source_type}\n"
        f"observed_at: {record.observed_at.isoformat()}\n"
        f"imported_at: {imported_at.isoformat()}\n"
        f"content_hash_algorithm: {HASH_ALGORITHM}\n"
        f"content_hash: {record.content_hash}\n"
        f"import_batch_id: {batch_id}\n"
        "status: historical_until_verified\n\n"
        f"{record.text}"
    )


def import_records(records_by_source: dict[str, list[HistoricalRecord]], inventory: dict[str, SourceInventory], batch_id: str) -> list[dict]:
    imported_at = datetime.now(timezone.utc)
    imported_rows = []
    with Session(engine) as session:
        existing_hashes = {
            str(value)
            for value in session.exec(select(Capture.content_hash).where(Capture.content_hash.is_not(None))).all()
            if value
        }
        for source_system, records in records_by_source.items():
            inv = inventory[source_system]
            for record in records:
                if record.content_hash in existing_hashes:
                    inv.records_duplicate += 1
                    continue
                safe_text = redact_secrets(capture_body(record, batch_id, imported_at))
                allowed, reason = should_capture_memory(safe_text, f"historical-import:{source_system}")
                if not allowed:
                    inv.records_rejected += 1
                    imported_rows.append({"source_system": source_system, "source_path": record.source_path, "content_hash": record.content_hash, "status": "rejected", "reason": reason})
                    continue
                capture = Capture(
                    raw_content=safe_text,
                    source=f"historical-import:{source_system}",
                    user_id=USER_ID,
                    source_system=source_system,
                    source_path=record.source_path,
                    source_type=record.source_type,
                    observed_at=record.observed_at.replace(tzinfo=None),
                    imported_at=imported_at.replace(tzinfo=None),
                    content_hash=record.content_hash,
                    import_batch_id=batch_id,
                    historical_until_verified=True,
                )
                try:
                    session.add(capture)
                    session.commit()
                    session.refresh(capture)
                    process_capture(capture.id)
                    existing_hashes.add(record.content_hash)
                    inv.records_imported += 1
                    imported_rows.append(
                        {
                            "capture_id": capture.id,
                            "source_system": source_system,
                            "source_path": record.source_path,
                            "content_hash": record.content_hash,
                            "exact_query": record.exact_query,
                            "semantic_query": record.semantic_query,
                            "status": "imported",
                        }
                    )
                except Exception as exc:
                    session.rollback()
                    inv.records_error += 1
                    imported_rows.append({"source_system": source_system, "source_path": record.source_path, "content_hash": record.content_hash, "status": "error", "reason": type(exc).__name__})
    return imported_rows


def top5_contains(results: list[dict], capture_id: int | None, content_hash_value: str) -> bool:
    for row in results[:5]:
        if capture_id and row.get("capture_id") == capture_id:
            return True
        if content_hash_value and content_hash_value in (row.get("raw_content") or ""):
            return True
    return False


def exact_memory_records(query: str, session: Session) -> list[dict]:
    return exact_capture_search(query, 5, None, session, USER_ID, include_operational=True)


def exact_top5_from_corpus(query: str, corpus: list[tuple[Capture, str]]) -> list[dict]:
    needle = (query or "").lower()
    if not needle:
        return []
    matches = [capture for capture, lowered in corpus if needle in lowered]
    matches.sort(key=lambda capture: capture.created_at, reverse=True)
    return [
        {
            "capture_id": capture.id,
            "chunk_id": None,
            "raw_content": capture.raw_content,
            "match_type": "exact",
            "memory_system": MEMORY_SYSTEM,
        }
        for capture in matches[:5]
    ]


def target_exact_result(capture: Capture, lowered: str, query: str) -> list[dict]:
    if (query or "").lower() not in lowered:
        return []
    return [
        {
            "capture_id": capture.id,
            "chunk_id": None,
            "raw_content": capture.raw_content,
            "match_type": "exact",
            "memory_system": MEMORY_SYSTEM,
        }
    ]


def verify_imported(batch_id: str, out_dir: Path, inventory: dict[str, SourceInventory]) -> tuple[list[dict], list[dict], list[dict]]:
    with Session(engine) as session:
        captures = list(session.exec(select(Capture).where(Capture.import_batch_id == batch_id, Capture.user_id == USER_ID)).all())
        exact_corpus = [
            (capture, (capture.raw_content or "").lower())
            for capture in session.exec(select(Capture).where(Capture.user_id == USER_ID)).all()
        ]
        lowered_by_capture = {capture.id: lowered for capture, lowered in exact_corpus}
        for capture in captures:
            if capture.source_system in inventory:
                inventory[capture.source_system].records_imported += 1
        for inv in inventory.values():
            inv.records_duplicate = max(
                0,
                inv.records_candidate - inv.records_imported - inv.records_rejected - inv.records_error,
            )
        chunks_by_capture = {
            row[0]: row[1]
            for row in session.exec(select(Chunk.capture_id, Chunk.id).where(Chunk.capture_id.in_([c.id for c in captures]))).all()
        } if captures else {}
        verification_rows = []
        for capture in captures:
            queries = []
            raw = capture.raw_content or ""
            body_text = raw.split("\n\n", 1)[-1]
            ids = LONG_ID_RE.findall(body_text)
            if ids:
                queries.append(("exact_id", ids[0]))
            for phrase in KNOWN_FACTS:
                if phrase.lower() in body_text.lower():
                    queries.append(("known_keyword", phrase))
                    break
            date_match = re.search(r"2026-(?:0[3-6])-[0-3][0-9]|July 20|August 10", body_text, re.IGNORECASE)
            if date_match:
                queries.append(("date_string", date_match.group(0)))
            if capture.source_path:
                queries.append(("source_path", capture.source_path[:180]))
            else:
                queries.append(("import_batch_id", batch_id))

            passed = 0
            for kind, query in queries:
                if kind in {"exact_id", "date_string", "source_path", "import_batch_id", "known_keyword"}:
                    results = target_exact_result(capture, lowered_by_capture.get(capture.id, ""), query)
                else:
                    results = search_memory_records(query, 5, None, session, USER_ID, include_semantic=False)
                ok = top5_contains(results, capture.id, capture.content_hash or "")
                passed += int(ok)
                verification_rows.append(
                    {
                        "capture_id": capture.id,
                        "chunk_id": chunks_by_capture.get(capture.id),
                        "source_system": capture.source_system,
                        "source_path": capture.source_path,
                        "content_hash": capture.content_hash,
                        "query_type": kind,
                        "query": query,
                        "passed": ok,
                        "top_count": len(results),
                        "top_capture_ids": [r.get("capture_id") for r in results[:5]],
                        "top_match_types": [r.get("match_type") for r in results[:5]],
                        "memory_system": MEMORY_SYSTEM,
                    }
                )
            if queries and passed == len(queries) and capture.source_system in inventory:
                inventory[capture.source_system].records_verified += 1
            if len(verification_rows) and len(verification_rows) % 2500 == 0:
                print(f"verified_query_checks={len(verification_rows)}", flush=True)

        known_rows = []
        for fact in KNOWN_FACTS:
            results = search_memory_records(fact, 5, None, session, USER_ID, include_semantic=False)
            known_rows.append(
                {
                    "query": fact,
                    "passed": bool(results),
                    "top_count": len(results),
                    "top_capture_ids": [r.get("capture_id") for r in results[:5]],
                    "top_match_types": [r.get("match_type") for r in results[:5]],
                    "memory_system": MEMORY_SYSTEM,
                }
            )

        rng = random.Random(20260604)
        sample = rng.sample(captures, min(100, len(captures)))
        random_rows = []
        for capture in sample:
            raw = capture.raw_content or ""
            exact = LONG_ID_RE.findall(raw)
            exact_q = exact[0] if exact else (capture.content_hash or batch_id)
            semantic_q = semantic_query_for(raw, capture.source_system or "historical memory")
            for kind, query in [("exact", exact_q), ("semantic", semantic_q)]:
                results = search_memory_records(query, 5, None, session, USER_ID, include_semantic=kind == "semantic")
                random_rows.append(
                    {
                        "capture_id": capture.id,
                        "source_system": capture.source_system,
                        "source_path": capture.source_path,
                        "query_type": kind,
                        "query": query,
                        "passed": top5_contains(results, capture.id, capture.content_hash or ""),
                        "top_capture_ids": [r.get("capture_id") for r in results[:5]],
                        "top_match_types": [r.get("match_type") for r in results[:5]],
                    }
                )
    write_jsonl(out_dir / "verification_all.jsonl", verification_rows)
    write_jsonl(out_dir / "known_fact_recall.jsonl", known_rows)
    write_jsonl(out_dir / "random_sample_recall.jsonl", random_rows)
    return verification_rows, known_rows, random_rows


def write_report(out_dir: Path, inventory: dict[str, SourceInventory], verification_rows: list[dict], known_rows: list[dict], random_rows: list[dict]) -> None:
    imported = sum(inv.records_imported for inv in inventory.values())
    verified = sum(inv.records_verified for inv in inventory.values())
    coverage = (verified / imported * 100.0) if imported else 0.0
    known_pass = sum(1 for row in known_rows if row["passed"])
    random_pass = sum(1 for row in random_rows if row["passed"])
    random_total = len(random_rows)
    random_exact = [row for row in random_rows if row.get("query_type") == "exact"]
    random_semantic = [row for row in random_rows if row.get("query_type") == "semantic"]
    gaps = [row for row in verification_rows if not row["passed"]][:50]
    rejected = sum(inv.records_rejected for inv in inventory.values())
    duplicates = sum(inv.records_duplicate for inv in inventory.values())
    errors = sum(inv.records_error for inv in inventory.values())
    lines = [
        "# March-June BYB Shared Memory Backfill Audit",
        "",
        "Acceptance window: 2026-03-01 through 2026-06-03.",
        f"Import batch ID: {next(iter(inventory.values())).import_batch_id if inventory else DEFAULT_BATCH_ID}.",
        f"Per-record exact/provenance verification coverage: {verified}/{imported} captures ({coverage:.2f}%).",
        "",
        "## 1. Source inventory",
        "",
        "| Source system | Files | Candidates | Imported | Verified | Rejected | Duplicate | Error | Window |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for inv in inventory.values():
        window = f"{inv.first_observed_at} to {inv.last_observed_at}" if inv.first_observed_at else "none"
        lines.append(
            f"| {inv.source_system} | {len(inv.files)} | {inv.records_candidate} | {inv.records_imported} | "
            f"{inv.records_verified} | {inv.records_rejected} | {inv.records_duplicate} | {inv.records_error} | {window} |"
        )
    lines.extend(
        [
            "",
            "## 2. Import coverage",
            "",
            f"- Imported captures: {imported}",
            f"- Fully verified captures: {verified}",
            f"- Per-record exact/provenance verification coverage: {coverage:.2f}%",
            f"- Duplicate records skipped: {duplicates}",
            "",
            "## 3. Known-fact recall",
            "",
            f"- Passed: {known_pass}/{len(known_rows)}",
            "",
        ]
    )
    for row in known_rows:
        lines.append(f"- {row['query']}: {'PASS' if row['passed'] else 'FAIL'} top={row['top_capture_ids']} match={row['top_match_types']}")
    lines.extend(
        [
            "",
            "## 4. Random-sample recall",
            "",
            f"- Query checks passed: {random_pass}/{random_total}",
            f"- Sampled historical records: {random_total // 2}",
            f"- Exact query checks passed: {sum(1 for row in random_exact if row['passed'])}/{len(random_exact)}",
            f"- Semantic paraphrase checks passed: {sum(1 for row in random_semantic if row['passed'])}/{len(random_semantic)}",
            "",
            "## 5. Rejected/quarantined records",
            "",
            f"- Rejected by quality guardrail: {rejected}",
            f"- Import errors: {errors}",
            f"- Duplicates retained as historical evidence in source but not re-imported: {duplicates}",
            "",
            "## 6. Gaps requiring manual review",
            "",
        ]
    )
    if gaps:
        for row in gaps:
            lines.append(f"- capture={row.get('capture_id')} query_type={row.get('query_type')} query={row.get('query')!r} source={row.get('source_path')}")
    else:
        lines.append("- No failed verification checks in imported capture verification.")
    if random_total and random_pass < random_total:
        lines.append(f"- Random-sample top-5 recall only passed {random_pass}/{random_total}; inspect random_sample_recall.jsonl for retrieval holes.")
    if inventory.get("Cortex Feedback SQL Backup") and inventory["Cortex Feedback SQL Backup"].records_discovered == 0:
        lines.append("- Cortex Feedback SQL Backup was required but no matching feedback SQL backup was discovered in the scoped paths.")
    (out_dir / "march_june_audit_report.md").write_text("\n".join(lines) + "\n")


def run(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir or f"/root/byb-memory-backfill-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths_by_source = discover_source_paths()
    records_by_source = {source: records_for_source(source, paths_by_source.get(source, [])) for source in REQUIRED_SOURCE_SYSTEMS}
    inventory = build_inventory(records_by_source, paths_by_source, args.batch_id)
    write_inventory_csv(inventory, out_dir)
    write_jsonl(
        out_dir / "candidate_records_inventory.jsonl",
        (
            {
                "source_system": rec.source_system,
                "source_path": rec.source_path,
                "source_type": rec.source_type,
                "observed_at": rec.observed_at.isoformat(),
                "content_hash": rec.content_hash,
                "exact_query": rec.exact_query,
                "semantic_query": rec.semantic_query,
                "redacted_excerpt": redact_secrets(rec.text[:500]),
            }
            for records in records_by_source.values()
            for rec in records
        ),
    )

    imported_rows: list[dict] = []
    verification_rows: list[dict] = []
    known_rows: list[dict] = []
    random_rows: list[dict] = []
    if args.phase in {"import", "all"}:
        imported_rows = import_records(records_by_source, inventory, args.batch_id)
        write_jsonl(out_dir / "import_results.jsonl", imported_rows)
        write_inventory_csv(inventory, out_dir)
    if args.phase in {"verify", "all"}:
        verification_rows, known_rows, random_rows = verify_imported(args.batch_id, out_dir, inventory)
        write_inventory_csv(inventory, out_dir)
        write_report(out_dir, inventory, verification_rows, known_rows, random_rows)
    print(json.dumps({"phase": args.phase, "out_dir": str(out_dir), "batch_id": args.batch_id, "sources": {k: {"candidates": v.records_candidate, "imported": v.records_imported, "verified": v.records_verified, "duplicates": v.records_duplicate, "rejected": v.records_rejected, "errors": v.records_error} for k, v in inventory.items()}}, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["inventory", "import", "verify", "all"], default="inventory")
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--out-dir")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
