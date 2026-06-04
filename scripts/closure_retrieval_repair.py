#!/usr/bin/env python3
"""Closure-phase retrieval repair, holdout evaluation, and Cortex SQL import."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlmodel import Session, select

from db.engine import engine, init_db
from db.schema import Capture, Chunk, MemoryFactCard
from packages.memory_retrieval.embeddings import compute_embeddings
from services.memory_api.main import (
    MEMORY_SYSTEM,
    exact_card_alias_search,
    exact_capture_search,
    lexical_card_search,
    merge_search_results,
    redact_secrets,
    semantic_card_search,
)
from scripts.repair_march_june_retrieval import (
    ARTIFACT_DIR,
    BATCH_ID,
    KNOWN_FACTS,
    USER_ID,
    aliases_for,
    card_content,
    capture_length_bucket,
    choose_keyword_query,
    extract_entities,
    memory_visibility_for,
    provenance_for,
    semantic_query_for_body,
    strip_header,
    write_jsonl,
)


CORTEX_SQL = Path("/var/www/openclaw.backyardbrains.com/memory/cortex-feedback-loop-backup-20260603.sql")
CORTEX_BATCH_ID = "historical-cortex-feedback-sql-20260603"
CORTEX_SOURCE_SYSTEM = "Backup SQL"
HASH_ALGORITHM = "sha256"
SECRET_WORD_RE = re.compile(r"\b(token|secret|password|passwd|pwd|api[_-]?key|bearer)\b", re.IGNORECASE)
COPY_CAPTURE_RE = re.compile(r"^COPY public\.capture \(id, raw_content, source, user_id, created_at\) FROM stdin;$")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def copy_unescape(value: str) -> str | None:
    if value == r"\N":
        return None
    replacements = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\\": "\\",
    }
    out = value
    for old, new in replacements.items():
        out = out.replace(old, new)
    return out


def iter_cortex_capture_rows(path: Path) -> Iterable[dict]:
    in_copy = False
    with path.open(errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not in_copy:
                in_copy = bool(COPY_CAPTURE_RE.match(line))
                continue
            if line == r"\.":
                break
            cells = next(csv.reader([line], delimiter="\t", quotechar="\0"))
            if len(cells) != 5:
                continue
            raw_content = copy_unescape(cells[1]) or ""
            yield {
                "source_row_id": cells[0],
                "raw_content": raw_content,
                "source": copy_unescape(cells[2]) or "backup-sql",
                "user_id": copy_unescape(cells[3]) or USER_ID,
                "created_at": copy_unescape(cells[4]) or "",
            }


def parse_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def in_march_june(value: str) -> bool:
    dt = parse_dt(value)
    if not dt:
        return False
    return datetime(2026, 3, 1, tzinfo=timezone.utc) <= dt <= datetime(2026, 6, 3, 23, 59, 59, tzinfo=timezone.utc)


def existing_body_hashes(session: Session) -> set[str]:
    hashes = set()
    for raw in session.exec(select(Capture.raw_content).where(Capture.user_id == USER_ID)).all():
        hashes.add(stable_hash(normalize_text(strip_header(raw or ""))))
    return hashes


def cortex_inventory() -> dict:
    rows = list(iter_cortex_capture_rows(CORTEX_SQL))
    window_rows = [row for row in rows if in_march_june(row["created_at"])]
    secret_rows = [row for row in window_rows if SECRET_WORD_RE.search(row["raw_content"])]
    redaction_changed = sum(1 for row in window_rows if redact_secrets(row["raw_content"]) != row["raw_content"])
    row_hashes = [stable_hash(normalize_text(row["raw_content"])) for row in window_rows]
    with Session(engine) as session:
        existing = existing_body_hashes(session)
        duplicates = sum(1 for digest in row_hashes if digest in existing)
        already_imported = session.exec(
            select(Capture.id).where(Capture.import_batch_id == CORTEX_BATCH_ID, Capture.user_id == USER_ID).limit(1)
        ).first() is not None
    inventory = {
        "source_path": str(CORTEX_SQL),
        "source_system": CORTEX_SOURCE_SYSTEM,
        "import_batch_id": CORTEX_BATCH_ID,
        "sql_capture_rows": len(rows),
        "window_candidate_rows": len(window_rows),
        "secret_scan_rows_with_secret_terms": len(secret_rows),
        "redaction_changed_rows": redaction_changed,
        "dedupe_existing_rows": duplicates,
        "dedupe_new_rows": max(0, len(window_rows) - duplicates),
        "already_imported": already_imported,
        "sample_hashes": row_hashes[:10],
        "status": "inventory_redaction_dedupe_dry_run",
    }
    (ARTIFACT_DIR / "cortex_sql_inventory_redaction_dedupe.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    return inventory


def ensure_fact_card_storage() -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text("ALTER TABLE memoryfactcard ADD COLUMN IF NOT EXISTS memory_visibility VARCHAR"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memoryfactcard_memory_visibility ON memoryfactcard (memory_visibility)"))


def make_capture_body(row: dict, imported_at: datetime, content_hash: str) -> str:
    observed = parse_dt(row["created_at"]) or imported_at
    return (
        "[Historical Import]\n"
        f"source_system: {CORTEX_SOURCE_SYSTEM}\n"
        f"source_path: {CORTEX_SQL}#capture:{row['source_row_id']}\n"
        "source_type: sql_backup_capture\n"
        f"observed_at: {observed.isoformat()}\n"
        f"imported_at: {imported_at.isoformat()}\n"
        f"content_hash_algorithm: {HASH_ALGORITHM}\n"
        f"content_hash: {content_hash}\n"
        f"import_batch_id: {CORTEX_BATCH_ID}\n"
        "status: historical_until_verified\n\n"
        f"{redact_secrets(row['raw_content'])}"
    )


def index_chunks(captures: list[Capture]) -> None:
    texts = [capture.raw_content for capture in captures]
    vectors = compute_embeddings(texts)
    with Session(engine) as session:
        for capture, vector in zip(captures, vectors):
            exists = session.exec(select(Chunk.id).where(Chunk.capture_id == capture.id).limit(1)).first()
            if exists:
                continue
            session.add(Chunk(content=capture.raw_content, embedding=vector, user_id=capture.user_id, capture_id=capture.id))
        session.commit()


def upsert_fact_cards_for_batch(batch_id: str) -> int:
    ensure_fact_card_storage()
    with Session(engine) as session:
        captures = list(session.exec(select(Capture).where(Capture.user_id == USER_ID, Capture.import_batch_id == batch_id).order_by(Capture.id)).all())
        existing = {
            card.source_capture_id: card
            for card in session.exec(select(MemoryFactCard).where(MemoryFactCard.source_capture_id.in_([c.id for c in captures if c.id]))).all()
        } if captures else {}
        payloads = []
        for capture in captures:
            body = strip_header(capture.raw_content or "")
            entities = extract_entities(body, capture)
            aliases = aliases_for(capture, entities)
            content = card_content(capture, body, entities, aliases)
            payloads.append((capture, entities, aliases, content))
        count = 0
        for start in range(0, len(payloads), 64):
            batch = payloads[start:start + 64]
            vectors = compute_embeddings([item[3] for item in batch])
            for (capture, entities, aliases, content), vector in zip(batch, vectors):
                card = existing.get(capture.id) or MemoryFactCard(source_capture_id=capture.id, content=content, embedding=vector)
                card.content = content
                card.aliases_json = json.dumps(aliases, ensure_ascii=True)
                card.aliases_text = "\n".join(alias.lower() for alias in aliases)
                card.entities_json = json.dumps(entities, ensure_ascii=True)
                card.project_slug = (entities.get("project_slugs") or [None])[0]
                card.source_system = capture.source_system
                card.source_type = capture.source_type
                card.source_path = capture.source_path
                card.observed_at = capture.observed_at
                card.historical_status = "historical_verified" if capture.historical_until_verified else "current"
                card.memory_visibility = memory_visibility_for(capture, strip_header(capture.raw_content or ""))
                card.provenance_json = json.dumps(provenance_for(capture), default=str, ensure_ascii=True)
                card.embedding = vector
                session.add(card)
                count += 1
            session.commit()
            print(f"fact_cards_batch={batch_id} indexed={min(start + len(batch), len(payloads))}/{len(payloads)}", flush=True)
    return count


def import_cortex() -> dict:
    inventory = cortex_inventory()
    if inventory["already_imported"]:
        return {"status": "already_imported", **inventory}
    imported_at = datetime.now(timezone.utc)
    imported: list[Capture] = []
    skipped_duplicates = 0
    with Session(engine) as session:
        existing = existing_body_hashes(session)
        seen_new: set[str] = set()
        for row in iter_cortex_capture_rows(CORTEX_SQL):
            if not in_march_june(row["created_at"]):
                continue
            body_hash = stable_hash(normalize_text(row["raw_content"]))
            if body_hash in existing or body_hash in seen_new:
                skipped_duplicates += 1
                continue
            seen_new.add(body_hash)
            observed = parse_dt(row["created_at"]) or imported_at
            source_path = f"{CORTEX_SQL}#capture:{row['source_row_id']}"
            content_hash = stable_hash(CORTEX_SOURCE_SYSTEM, source_path, normalize_text(row["raw_content"]))
            capture = Capture(
                raw_content=make_capture_body(row, imported_at, content_hash),
                source="historical-import:Backup SQL",
                user_id=USER_ID,
                source_system=CORTEX_SOURCE_SYSTEM,
                source_path=source_path,
                source_type="sql_backup_capture",
                observed_at=observed.replace(tzinfo=None),
                imported_at=imported_at.replace(tzinfo=None),
                content_hash=content_hash,
                import_batch_id=CORTEX_BATCH_ID,
                historical_until_verified=True,
            )
            session.add(capture)
            imported.append(capture)
        session.commit()
        for capture in imported:
            session.refresh(capture)
    for start in range(0, len(imported), 64):
        index_chunks(imported[start:start + 64])
        print(f"cortex_chunks_indexed={min(start + 64, len(imported))}/{len(imported)}", flush=True)
    card_count = upsert_fact_cards_for_batch(CORTEX_BATCH_ID)
    result = {
        **inventory,
        "status": "imported",
        "imported": len(imported),
        "skipped_duplicates": skipped_duplicates,
        "fact_cards": card_count,
    }
    (ARTIFACT_DIR / "cortex_sql_import_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    return result


def top5_contains(results: list[dict], capture_id: int) -> bool:
    return any(row.get("capture_id") == capture_id for row in results[:5])


def keyword_results(query: str, limit: int, session: Session) -> list[dict]:
    return merge_search_results(
        [
            exact_card_alias_search(query, limit, None, session, USER_ID, include_operational=True),
            lexical_card_search(query, limit, None, session, USER_ID, include_operational=True),
            exact_capture_search(query, limit, None, session, USER_ID, include_operational=True),
        ],
        limit,
    )


def semantic_results(query: str, limit: int, session: Session) -> list[dict]:
    return merge_search_results(
        [
            lexical_card_search(query, limit, None, session, USER_ID, include_operational=True),
            semantic_card_search(query, limit, None, session, USER_ID, include_operational=True),
        ],
        limit,
    )


def evaluate_sample(batch_id: str, out_prefix: str, seed: int, exclude_ids: set[int] | None = None) -> dict:
    exclude_ids = exclude_ids or set()
    with Session(engine) as session:
        captures = [
            capture for capture in session.exec(select(Capture).where(Capture.user_id == USER_ID, Capture.import_batch_id == batch_id).order_by(Capture.id)).all()
            if capture.id not in exclude_ids
        ]
        sample_size = min(100, len(captures))
        sample = random.Random(seed).sample(captures, sample_size) if sample_size else []
        corpus = [
            (capture.id, re.sub(r"\s+", " ", strip_header(capture.raw_content or "")).lower())
            for capture in captures
            if capture.id is not None
        ]
        rows = []
        for capture in sample:
            body = strip_header(capture.raw_content or "")
            keyword_q = choose_keyword_query(capture, body, corpus)
            semantic_q = semantic_query_for_body(body, capture.source_system)
            for query_type, query in [("keyword", keyword_q), ("semantic", semantic_q)]:
                results = keyword_results(query, 5, session) if query_type == "keyword" else semantic_results(query, 5, session)
                rows.append(
                    {
                        "capture_id": capture.id,
                        "source_system": capture.source_system,
                        "source_type": capture.source_type,
                        "source_path": capture.source_path,
                        "query_type": query_type,
                        "query": query,
                        "passed": top5_contains(results, capture.id),
                        "top_capture_ids": [row.get("capture_id") for row in results[:5]],
                        "top_match_types": [row.get("match_type") for row in results[:5]],
                        "memory_system": MEMORY_SYSTEM,
                    }
                )
                if len(rows) % 50 == 0:
                    print(f"{out_prefix}_checks={len(rows)}/{sample_size * 2}", flush=True)
    write_jsonl(ARTIFACT_DIR / f"{out_prefix}.jsonl", rows)
    keyword = [row for row in rows if row["query_type"] == "keyword"]
    semantic = [row for row in rows if row["query_type"] == "semantic"]
    summary = {
        "batch_id": batch_id,
        "sampled_captures": sample_size,
        "total": len(rows),
        "passed": sum(1 for row in rows if row["passed"]),
        "keyword_total": len(keyword),
        "keyword_passed": sum(1 for row in keyword if row["passed"]),
        "semantic_total": len(semantic),
        "semantic_passed": sum(1 for row in semantic if row["passed"]),
    }
    (ARTIFACT_DIR / f"{out_prefix}_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def known_fact_summary(out_name: str) -> dict:
    rows = []
    with Session(engine) as session:
        for fact in KNOWN_FACTS:
            results = keyword_results(fact, 5, session)
            rows.append(
                {
                    "query": fact,
                    "passed": bool(results),
                    "top_capture_ids": [row.get("capture_id") for row in results[:5]],
                    "top_match_types": [row.get("match_type") for row in results[:5]],
                }
            )
    write_jsonl(ARTIFACT_DIR / out_name, rows)
    return {"known_total": len(rows), "known_passed": sum(1 for row in rows if row["passed"])}


def provenance_summary(batch_id: str, out_name: str) -> dict:
    with Session(engine) as session:
        captures = list(session.exec(select(Capture).where(Capture.user_id == USER_ID, Capture.import_batch_id == batch_id)).all())
        capture_ids = [capture.id for capture in captures if capture.id]
        chunk_count = session.exec(select(Chunk.id).where(Chunk.capture_id.in_(capture_ids))).all() if capture_ids else []
        card_count = session.exec(select(MemoryFactCard.id).where(MemoryFactCard.source_capture_id.in_(capture_ids))).all() if capture_ids else []
    summary = {
        "batch_id": batch_id,
        "captures_total": len(captures),
        "chunks_total": len(chunk_count),
        "fact_cards_total": len(card_count),
        "provenance_verified": len(captures) == len(chunk_count) == len(card_count),
    }
    (ARTIFACT_DIR / out_name).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def update_report(extra: dict) -> None:
    report_path = ARTIFACT_DIR / "march_june_audit_report.md"
    text_value = report_path.read_text()
    marker = "\n## 8. Retrieval repair closure phase\n"
    if marker in text_value:
        text_value = text_value.split(marker, 1)[0].rstrip() + "\n"
    lines = [
        "",
        "## 8. Retrieval repair closure phase",
        "",
        "Status: closure repair applied; incident remains open pending final keyword target and Cortex review.",
        "",
        "### Keyword failure analysis",
        "",
        f"- Current failed keyword probes analyzed: {extra['keyword_failed']}",
        f"- Aggregates: {json.dumps(extra['keyword_aggregates'], sort_keys=True)}",
        "",
        "### Search tuning",
        "",
        "- Strengthened compact-card lexical ranking with wider candidate retrieval, exact phrase boosts, rare-token overlap, token coverage, and exact alias precedence.",
        "- Added closure failure artifacts without modifying original ledgers.",
        "",
        "### Metrics",
        "",
        f"- Original repaired sample keyword: {extra['original_summary']['keyword_passed']}/{extra['original_summary']['keyword_total']}",
        f"- Original repaired sample semantic: {extra['original_summary']['semantic_passed']}/{extra['original_summary']['semantic_total']}",
        f"- Holdout keyword: {extra['holdout_summary']['keyword_passed']}/{extra['holdout_summary']['keyword_total']}",
        f"- Holdout semantic: {extra['holdout_summary']['semantic_passed']}/{extra['holdout_summary']['semantic_total']}",
        f"- Known facts: {extra['known_summary']['known_passed']}/{extra['known_summary']['known_total']}",
        f"- March-June provenance: {extra['march_provenance']['captures_total']}/{extra['march_provenance']['captures_total']} captures with chunks/cards={extra['march_provenance']['provenance_verified']}",
        "",
        "### Cortex SQL",
        "",
        f"- Inventory candidates: {extra['cortex_inventory']['window_candidate_rows']}",
        f"- Redaction changed rows: {extra['cortex_inventory']['redaction_changed_rows']}",
        f"- Dedupe new rows: {extra['cortex_inventory']['dedupe_new_rows']}",
        f"- Import result: {extra['cortex_import']['status']} imported={extra['cortex_import'].get('imported', 0)} duplicates={extra['cortex_import'].get('skipped_duplicates', 0)}",
        f"- Cortex provenance: captures={extra['cortex_provenance']['captures_total']} chunks={extra['cortex_provenance']['chunks_total']} cards={extra['cortex_provenance']['fact_cards_total']} verified={extra['cortex_provenance']['provenance_verified']}",
        f"- Cortex recall: keyword={extra['cortex_recall']['keyword_passed']}/{extra['cortex_recall']['keyword_total']} semantic={extra['cortex_recall']['semantic_passed']}/{extra['cortex_recall']['semantic_total']}",
        "",
        "### Remaining gaps",
        "",
        "- Keyword target is not declared met unless the latest summary reaches the requested threshold.",
        "- Remaining misses are repeated scheduled prompt/log templates where the unchanged probe text does not uniquely identify one capture.",
        "",
        "### Closure recommendation",
        "",
        "- Do not close the incident yet. Continue with duplicate-template disambiguation or revise the keyword probe generator to include source-body unique substrings for repeated automation records.",
    ]
    report_path.write_text(text_value.rstrip() + "\n" + "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cortex-inventory", action="store_true")
    parser.add_argument("--cortex-import", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    args = parser.parse_args()

    if args.cortex_inventory:
        print(json.dumps(cortex_inventory(), indent=2, sort_keys=True))
        return
    if args.cortex_import:
        print(json.dumps(import_cortex(), indent=2, sort_keys=True, default=str))
        return
    if args.evaluate:
        original_ids = {
            int(row["capture_id"])
            for row in (json.loads(line) for line in (ARTIFACT_DIR / "random_sample_recall_after.jsonl").open())
            if row.get("query_type") == "keyword"
        }
        original_summary = json.loads((ARTIFACT_DIR / "recall_after_summary.json").read_text())
        holdout_summary = evaluate_sample(BATCH_ID, "random_sample_recall_holdout", 20260605, original_ids)
        cortex_recall = evaluate_sample(CORTEX_BATCH_ID, "cortex_sql_random_recall", 20260606)
        known = known_fact_summary("known_fact_recall_closure.jsonl")
        march_prov = provenance_summary(BATCH_ID, "provenance_verification_closure.json")
        cortex_prov = provenance_summary(CORTEX_BATCH_ID, "cortex_sql_provenance_verification.json")
        keyword_aggs = json.loads((ARTIFACT_DIR / "keyword_failure_aggregates_closure.json").read_text())
        cortex_inventory_data = json.loads((ARTIFACT_DIR / "cortex_sql_inventory_redaction_dedupe.json").read_text())
        cortex_import_data = json.loads((ARTIFACT_DIR / "cortex_sql_import_result.json").read_text())
        extra = {
            "keyword_failed": sum(keyword_aggs["query_type"].values()),
            "keyword_aggregates": keyword_aggs,
            "original_summary": original_summary,
            "holdout_summary": holdout_summary,
            "cortex_recall": cortex_recall,
            "known_summary": known,
            "march_provenance": march_prov,
            "cortex_provenance": cortex_prov,
            "cortex_inventory": cortex_inventory_data,
            "cortex_import": cortex_import_data,
        }
        (ARTIFACT_DIR / "closure_phase_summary.json").write_text(json.dumps(extra, indent=2, sort_keys=True) + "\n")
        update_report(extra)
        print(json.dumps(extra, indent=2, sort_keys=True))
        return
    parser.error("choose --cortex-inventory, --cortex-import, or --evaluate")


if __name__ == "__main__":
    main()
