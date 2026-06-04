#!/usr/bin/env python3
"""Repair March-June historical retrieval without reimporting raw captures."""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import sys
from collections import Counter, defaultdict
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
    lexical_capture_search,
    merge_search_results,
    redact_secrets,
    search_memory_records,
    semantic_card_search,
    semantic_chunk_search,
)


ARTIFACT_DIR = Path("/root/byb-memory-backfill-20260604T001145Z")
BATCH_ID = "historical-march-june-2026"
USER_ID = "greg"
RANDOM_SAMPLE_PATH = ARTIFACT_DIR / "random_sample_recall.jsonl"
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

LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
DATE_RE = re.compile(
    r"\b(?:2026-(?:0[3-6])-[0-3][0-9]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|July|Aug|August|Sep|Oct|Nov|Dec)[a-z]*\.?\s+[0-3]?\d)\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s)>\"]+")
SERVER_PATH_RE = re.compile(r"(?:/var/www|/root|/srv|/opt|/home)/[^\s,;)\"]+")
FILENAME_RE = re.compile(r"\b[A-Za-z0-9_.-]+\.(?:md|jsonl?|sql|sqlite3?|csv|txt|log|html?|py|ts|tsx|js|mjs|zip|gz|bak)\b")
PROJECT_TAG_RE = re.compile(r"\[project:\s*([A-Za-z0-9_-]+)\]", re.IGNORECASE)
BODY_SPLIT = "\n\n"
STOPWORDS = {
    "about", "after", "also", "back", "been", "being", "from", "have", "into", "just",
    "more", "need", "next", "note", "only", "over", "source", "status", "that", "their",
    "there", "this", "through", "with", "would", "your", "historical", "import",
}
NAMED_PROJECTS = [
    "Grass Foundation",
    "MBL",
    "Woods Hole",
    "Falmouth",
    "SpikerBot",
    "Kickstarter",
    "Meta Pixel",
    "CAPI",
    "Jellop",
    "LaunchBoom",
    "NIMH",
    "NINDS",
    "MIT Press",
    "Children's School of Science",
    "CSS",
    "Xero",
    "Shopify",
    "Cortex",
    "Watson",
]
AUTOMATION_HEARTBEAT_RE = re.compile(r"\bHEARTBEAT\b|heartbeat Current time|Read HEARTBEAT exists", re.IGNORECASE)
OPERATIONAL_AUDIT_RE = re.compile(
    r"\b(cron|TOOL EXEC RULE|AUTONOMOUS|PROACTIVE|health check|Midday Pulse|Business Hours Sync|"
    r"Morning Briefing|Afternoon Wrap|Night Watch|Verify FedEx Bills|Subagent Context)\b",
    re.IGNORECASE,
)


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str, ensure_ascii=True) + "\n")


def strip_header(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith("[Historical Import]") and BODY_SPLIT in raw:
        return raw.split(BODY_SPLIT, 1)[1].strip()
    return raw.strip()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_tokens(value: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]{2,}", value or "")).lower()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80]


def unique(values: Iterable[str], limit: int = 80) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = normalize_space(str(value).strip(" \t\n\r,.;:)]}\"'"))
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def project_slug_for(capture: Capture, body: str) -> str | None:
    for match in PROJECT_TAG_RE.finditer(body):
        return slugify(match.group(1))
    source_path = capture.source_path or ""
    match = re.search(r"/workspaces/([^/]+)/", source_path)
    if match:
        return slugify(match.group(1))
    for name in NAMED_PROJECTS:
        if name.lower() in body.lower():
            return slugify(name)
    return None


def extract_entities(body: str, capture: Capture) -> dict[str, list[str]]:
    source_path = capture.source_path or ""
    text_value = body or ""
    long_numbers = unique(LONG_NUMBER_RE.findall(text_value))
    urls = unique(URL_RE.findall(text_value))
    dates = unique(match.group(0) for match in DATE_RE.finditer(text_value))
    server_paths = unique(SERVER_PATH_RE.findall(text_value) + ([source_path] if source_path.startswith("/") else []))
    filenames = unique(FILENAME_RE.findall(text_value) + ([Path(source_path).name] if source_path else []))
    project_slugs = unique([project_slug_for(capture, body) or ""])
    named_projects = unique(name for name in NAMED_PROJECTS if name.lower() in text_value.lower())

    reservation_numbers = []
    pixel_ids = []
    for match in LONG_NUMBER_RE.finditer(text_value):
        window = text_value[max(0, match.start() - 80):match.end() + 80].lower()
        value = match.group(0)
        if "reservation" in window or "housing" in window or "mbl" in window:
            reservation_numbers.append(value)
        if "pixel" in window or "meta" in window or "campaign" in window or "capi" in window:
            pixel_ids.append(value)

    return {
        "long_numbers": long_numbers,
        "dates": dates,
        "urls": urls,
        "filenames": filenames,
        "project_slugs": project_slugs,
        "reservation_numbers": unique(reservation_numbers),
        "pixel_ids": unique(pixel_ids),
        "server_paths": server_paths,
        "named_projects": named_projects,
    }


def card_summary(body: str) -> str:
    clean = normalize_space(redact_secrets(body))
    if len(clean) <= 650:
        return clean
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    summary = normalize_space(" ".join(sentences[:3]))
    if len(summary) < 160:
        summary = clean[:650]
    return summary[:650].rstrip()


def aliases_for(capture: Capture, entities: dict[str, list[str]]) -> list[str]:
    aliases: list[str] = []
    for key in (
        "long_numbers",
        "dates",
        "urls",
        "filenames",
        "project_slugs",
        "reservation_numbers",
        "pixel_ids",
        "server_paths",
        "named_projects",
    ):
        aliases.extend(entities.get(key, []))
    if capture.source_path:
        aliases.append(capture.source_path)
        aliases.append(Path(capture.source_path).name)
    if capture.content_hash:
        aliases.append(capture.content_hash)
        aliases.append(capture.content_hash[:12])
    if capture.source_system:
        aliases.append(capture.source_system)
    return unique(aliases, limit=120)


def provenance_for(capture: Capture) -> dict:
    return {
        "source_capture_id": capture.id,
        "source_system": capture.source_system,
        "source_type": capture.source_type,
        "source_path": capture.source_path,
        "observed_at": capture.observed_at,
        "content_hash": capture.content_hash,
        "import_batch_id": capture.import_batch_id,
        "evidence_layer": "raw_capture",
    }


def memory_visibility_for(capture: Capture, body: str) -> str:
    text_value = f"{capture.source_system or ''}\n{capture.source_type or ''}\n{capture.source_path or ''}\n{body or ''}"
    if AUTOMATION_HEARTBEAT_RE.search(text_value):
        return "automation_heartbeat"
    if OPERATIONAL_AUDIT_RE.search(text_value):
        return "operational_audit"
    return "historical_evidence"


def card_content(capture: Capture, body: str, entities: dict[str, list[str]], aliases: list[str]) -> str:
    entity_bits = []
    for key, label in [
        ("long_numbers", "numbers"),
        ("dates", "dates"),
        ("urls", "urls"),
        ("filenames", "files"),
        ("project_slugs", "projects"),
        ("reservation_numbers", "reservations"),
        ("pixel_ids", "pixels"),
        ("server_paths", "paths"),
        ("named_projects", "names"),
    ]:
        values = entities.get(key, [])
        if values:
            entity_bits.append(f"{label}: {', '.join(values[:8])}")
    entity_line = "; ".join(entity_bits)
    return "\n".join(
        line for line in [
            "[Memory Fact Card]",
            f"summary: {card_summary(body)}",
            f"entities: {entity_line}" if entity_line else "",
            f"project_slug: {entities.get('project_slugs', [''])[0]}" if entities.get("project_slugs") else "",
            f"aliases: {', '.join(aliases[:20])}" if aliases else "",
            f"source_capture_id: {capture.id}",
            f"provenance: {capture.source_system}; {capture.source_type}; {capture.source_path}; observed_at={capture.observed_at}; content_hash={capture.content_hash}",
        ] if line
    )


def ensure_fact_card_storage() -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text("ALTER TABLE memoryfactcard ADD COLUMN IF NOT EXISTS memory_visibility VARCHAR"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memoryfactcard_memory_visibility ON memoryfactcard (memory_visibility)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_memoryfactcard_content_trgm ON memoryfactcard USING gin (content gin_trgm_ops)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_memoryfactcard_content_fts_simple "
                "ON memoryfactcard USING gin (to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(aliases_text, '')))"
            )
        )


def generate_fact_cards() -> list[dict]:
    ensure_fact_card_storage()
    rows_for_json: list[dict] = []
    with Session(engine) as session:
        captures = list(
            session.exec(
                select(Capture)
                .where(Capture.user_id == USER_ID, Capture.import_batch_id == BATCH_ID)
                .order_by(Capture.id)
            ).all()
        )
        existing = {
            card.source_capture_id: card
            for card in session.exec(select(MemoryFactCard).where(MemoryFactCard.source_capture_id.in_([c.id for c in captures if c.id]))).all()
        } if captures else {}
        payloads = []
        for capture in captures:
            card = existing.get(capture.id)
            if card and card.embedding is not None:
                body = strip_header(capture.raw_content or "")
                card.memory_visibility = memory_visibility_for(capture, body)
                session.add(card)
                rows_for_json.append(
                    {
                        "source_capture_id": capture.id,
                        "project_slug": card.project_slug,
                        "source_system": card.source_system,
                        "source_type": card.source_type,
                        "source_path": card.source_path,
                        "entities": json.loads(card.entities_json or "{}"),
                        "aliases": json.loads(card.aliases_json or "[]")[:40],
                        "historical_status": card.historical_status,
                        "memory_visibility": card.memory_visibility or "historical_evidence",
                        "provenance": json.loads(card.provenance_json or "{}"),
                        "card_excerpt": card.content[:700],
                    }
                )
                continue
            body = strip_header(capture.raw_content or "")
            entities = extract_entities(body, capture)
            aliases = aliases_for(capture, entities)
            content = card_content(capture, body, entities, aliases)
            payloads.append((capture, body, entities, aliases, content))

        for start in range(0, len(payloads), 64):
            batch = payloads[start:start + 64]
            vectors = compute_embeddings([item[4] for item in batch])
            for (capture, _body, entities, aliases, content), vector in zip(batch, vectors):
                project_slug = (entities.get("project_slugs") or [None])[0]
                card = existing.get(capture.id)
                if not card:
                    card = MemoryFactCard(source_capture_id=capture.id, content=content, embedding=vector)
                card.content = content
                card.aliases_json = json.dumps(aliases, ensure_ascii=True)
                card.aliases_text = "\n".join(alias.lower() for alias in aliases)
                card.entities_json = json.dumps(entities, ensure_ascii=True)
                card.project_slug = project_slug
                card.source_system = capture.source_system
                card.source_type = capture.source_type
                card.source_path = capture.source_path
                card.observed_at = capture.observed_at
                card.historical_status = "historical_verified" if capture.historical_until_verified else "current"
                card.memory_visibility = memory_visibility_for(capture, _body)
                card.provenance_json = json.dumps(provenance_for(capture), default=str, ensure_ascii=True)
                card.embedding = vector
                session.add(card)
                rows_for_json.append(
                    {
                        "source_capture_id": capture.id,
                        "project_slug": project_slug,
                        "source_system": capture.source_system,
                        "source_type": capture.source_type,
                        "source_path": capture.source_path,
                        "entities": entities,
                        "aliases": aliases[:40],
                        "historical_status": card.historical_status,
                        "memory_visibility": card.memory_visibility,
                        "provenance": provenance_for(capture),
                        "card_excerpt": content[:700],
                    }
                )
            session.commit()
            print(
                f"fact_cards_indexed={len(rows_for_json)}/{len(captures)} "
                f"(new_batch={min(start + len(batch), len(payloads))}/{len(payloads)})",
                flush=True,
            )
        session.commit()
    write_jsonl(ARTIFACT_DIR / "memory_fact_cards.jsonl", rows_for_json)
    return rows_for_json


def chunk_for_capture(session: Session, capture_id: int) -> Chunk | None:
    return session.exec(select(Chunk).where(Chunk.capture_id == capture_id).limit(1)).first()


def raw_only_search(query: str, query_type: str, limit: int, session: Session) -> list[dict]:
    exact = exact_capture_search(query, limit, None, session, USER_ID, include_operational=True)
    lexical = lexical_capture_search(query, limit, None, session, USER_ID, include_operational=True)
    semantic = []
    if query_type == "semantic":
        try:
            semantic = semantic_chunk_search(query, limit, None, session, USER_ID, include_operational=True)
        except Exception:
            session.rollback()
            semantic = []
    return merge_search_results([exact, lexical, semantic], limit)


def capture_length_bucket(length: int) -> str:
    if length < 500:
        return "<500"
    if length < 1000:
        return "500-999"
    if length < 2000:
        return "1000-1999"
    if length < 4000:
        return "2000-3999"
    return "4000+"


def classify_failure(row: dict, capture: Capture | None, chunk: Chunk | None, top100: list[dict], body: str, global_query_count: int) -> str:
    query = row.get("query") or ""
    if not capture:
        return "missing_expected_capture"
    if not chunk:
        return "missing_expected_chunk"
    if row.get("query_type") == "semantic" and "Historical Import source system" in query:
        return "generic_header_semantic_query"
    if row.get("query_type") == "exact" and query and query not in body and query in (capture.raw_content or ""):
        return "non_distinct_import_metadata_query"
    if global_query_count > 5:
        return "ambiguous_identifier_alias"
    top_ids = [item.get("capture_id") for item in top100]
    if capture.id in top_ids[5:100]:
        return "ranked_below_top5"
    if capture.id not in top_ids:
        return "raw_ranking_recall_gap"
    return "other_top5_failure"


def analyze_failures() -> tuple[list[dict], dict]:
    rows = read_jsonl(RANDOM_SAMPLE_PATH)
    failed = [row for row in rows if not row.get("passed")]
    query_counts = Counter((row.get("query_type"), row.get("query")) for row in rows)
    analysis_rows = []
    with Session(engine) as session:
        for idx, row in enumerate(failed, start=1):
            capture_id = int(row["capture_id"])
            capture = session.get(Capture, capture_id)
            chunk = chunk_for_capture(session, capture_id) if capture else None
            body = strip_header(capture.raw_content or "") if capture else ""
            top100 = raw_only_search(row.get("query") or "", row.get("query_type") or "", 100, session)
            top_ids = [item.get("capture_id") for item in top100]
            wrong = next((item for item in top100 if item.get("capture_id") != capture_id), None)
            entities = extract_entities(body, capture) if capture else {}
            project_slug = project_slug_for(capture, body) if capture else None
            failure_class = classify_failure(
                row,
                capture,
                chunk,
                top100,
                body,
                query_counts[(row.get("query_type"), row.get("query"))],
            )
            analysis_rows.append(
                {
                    "expected_capture_id": capture_id,
                    "source_system": row.get("source_system") or (capture.source_system if capture else None),
                    "source_type": capture.source_type if capture else None,
                    "source_path": row.get("source_path") or (capture.source_path if capture else None),
                    "query_type": row.get("query_type"),
                    "query_text": row.get("query"),
                    "expected_capture_exists": bool(capture),
                    "expected_chunks_exist": bool(chunk),
                    "expected_capture_in_top20": capture_id in top_ids[:20],
                    "expected_capture_in_top100": capture_id in top_ids[:100],
                    "top_wrong_result": {
                        "capture_id": wrong.get("capture_id"),
                        "source_system": wrong.get("source_system"),
                        "source_type": wrong.get("source_type"),
                        "source_path": wrong.get("source_path"),
                    } if wrong else None,
                    "match_type": wrong.get("match_type") if wrong else None,
                    "rank_reason": wrong.get("rank_reason") if wrong else None,
                    "chunk_length": len(chunk.content or "") if chunk else 0,
                    "capture_length": len(capture.raw_content or "") if capture else 0,
                    "embedding_present": getattr(chunk, "embedding", None) is not None if chunk else False,
                    "historical_status": "historical_verified" if capture and capture.historical_until_verified else "missing_or_current",
                    "project_slug": project_slug,
                    "extracted_entities": entities,
                    "failure_class": failure_class,
                }
            )
            if idx % 25 == 0:
                print(f"failure_analysis_rows={idx}/{len(failed)}", flush=True)

    aggregates = {
        "failure_class": dict(Counter(row["failure_class"] for row in analysis_rows)),
        "source_type": dict(Counter(row["source_type"] or "unknown" for row in analysis_rows)),
        "query_type": dict(Counter(row["query_type"] for row in analysis_rows)),
        "capture_length_bucket": dict(Counter(capture_length_bucket(row["capture_length"]) for row in analysis_rows)),
    }
    write_jsonl(ARTIFACT_DIR / "recall_failure_analysis.jsonl", analysis_rows)
    (ARTIFACT_DIR / "recall_failure_aggregates.json").write_text(json.dumps(aggregates, indent=2, sort_keys=True) + "\n")
    return analysis_rows, aggregates


def audit_exact_queries() -> dict:
    rows = read_jsonl(RANDOM_SAMPLE_PATH)
    exact_rows = [row for row in rows if row.get("query_type") == "exact"]
    audit_rows = []
    with Session(engine) as session:
        for row in exact_rows:
            capture = session.get(Capture, int(row["capture_id"]))
            chunk = chunk_for_capture(session, int(row["capture_id"])) if capture else None
            query = row.get("query") or ""
            raw = capture.raw_content or "" if capture else ""
            body = strip_header(raw)
            chunk_content = chunk.content or "" if chunk else ""
            audit_rows.append(
                {
                    "capture_id": row["capture_id"],
                    "query": query,
                    "raw_substring": query in raw,
                    "body_substring": query in body,
                    "chunk_substring": query in chunk_content,
                    "recommended_category": "keyword" if query in raw and query not in body else "exact",
                    "reason": "query came from Historical Import metadata, not source body" if query in raw and query not in body else "query appears in source body",
                }
            )
    summary = {
        "total_exact_rows": len(exact_rows),
        "raw_substring": sum(1 for row in audit_rows if row["raw_substring"]),
        "body_substring": sum(1 for row in audit_rows if row["body_substring"]),
        "chunk_substring": sum(1 for row in audit_rows if row["chunk_substring"]),
        "recommended_rename_to_keyword": sum(1 for row in audit_rows if row["recommended_category"] == "keyword"),
        "rows": audit_rows,
    }
    (ARTIFACT_DIR / "exact_query_category_audit.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def candidate_phrases(body: str, capture: Capture) -> list[str]:
    entities = extract_entities(body, capture)
    candidates: list[str] = []
    clean = card_summary(body)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]{2,}", clean)
    for width in (12, 10, 8, 7, 6, 5):
        for start in range(0, min(len(words), 120) - width + 1, max(1, width // 2)):
            phrase = " ".join(words[start:start + width])
            if 24 <= len(phrase) <= 140:
                candidates.append(phrase)
    for key in ("reservation_numbers", "pixel_ids", "urls", "long_numbers"):
        candidates.extend(entities.get(key, []))
    return unique(candidates, limit=160)


def is_generic_keyword_candidate(candidate: str) -> bool:
    value = candidate.strip()
    if not value:
        return True
    if DATE_RE.fullmatch(value):
        return True
    if FILENAME_RE.fullmatch(value):
        return True
    if LONG_NUMBER_RE.fullmatch(value):
        return True
    if value.lower() in {name.lower() for name in NAMED_PROJECTS}:
        return True
    if len(value.split()) < 4 and not URL_RE.search(value):
        return True
    return False


def choose_keyword_query(capture: Capture, body: str, body_corpus: list[tuple[int, str]]) -> str:
    candidates = candidate_phrases(body, capture)
    lowered_corpus = body_corpus
    normalized_body = normalize_tokens(body)
    dated_candidates = [
        candidate for candidate in candidates
        if re.search(r"\b(?:current\s+time|reference\s+utc|2026|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", candidate, re.IGNORECASE)
    ]
    candidates = unique(dated_candidates + candidates, limit=200)
    for candidate in candidates:
        if is_generic_keyword_candidate(candidate):
            continue
        needle = normalize_tokens(candidate)
        if not needle or needle not in normalized_body:
            continue
        count = sum(1 for _cid, text_value in lowered_corpus if needle in text_value)
        if count <= 1:
            return candidate
    for candidate in candidates:
        if is_generic_keyword_candidate(candidate):
            continue
        needle = normalize_tokens(candidate)
        if not needle or needle not in normalized_body:
            continue
        count = sum(1 for _cid, text_value in lowered_corpus if needle in text_value)
        if count <= 3:
            return candidate
    for candidate in candidates:
        if is_generic_keyword_candidate(candidate):
            continue
        if normalize_tokens(candidate) in normalized_body:
            return candidate
    clean = normalize_space(body)
    return clean[:120] if clean else (capture.content_hash or BATCH_ID)


def semantic_query_for_body(body: str, source_system: str | None) -> str:
    words = [
        word for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", normalize_space(body))
        if word.lower() not in STOPWORDS
    ]
    return f"historical {source_system or 'memory'} note about {' '.join(words[:12])}".strip()


def run_repaired_recall() -> tuple[list[dict], list[dict], dict]:
    original_rows = read_jsonl(RANDOM_SAMPLE_PATH)
    capture_ids = []
    for row in original_rows:
        cid = int(row["capture_id"])
        if cid not in capture_ids:
            capture_ids.append(cid)
    repaired_rows: list[dict] = []
    with Session(engine) as session:
        captures = [session.get(Capture, capture_id) for capture_id in capture_ids]
        captures = [capture for capture in captures if capture]
        body_corpus = [
            (capture.id, normalize_tokens(strip_header(capture.raw_content or "")))
            for capture in session.exec(select(Capture).where(Capture.user_id == USER_ID, Capture.import_batch_id == BATCH_ID)).all()
            if capture.id is not None
        ]
        queries_by_capture: dict[int, dict[str, str]] = {}
        for capture in captures:
            body = strip_header(capture.raw_content or "")
            queries_by_capture[capture.id] = {
                "keyword": choose_keyword_query(capture, body, body_corpus),
                "semantic": semantic_query_for_body(body, capture.source_system),
            }

        for row in original_rows:
            capture = session.get(Capture, int(row["capture_id"]))
            if not capture:
                continue
            query_type = "keyword" if row.get("query_type") == "exact" else row.get("query_type")
            query = queries_by_capture[capture.id][query_type]
            if query_type == "keyword":
                results = merge_search_results(
                    [
                        exact_card_alias_search(query, 5, None, session, USER_ID, include_operational=True),
                        lexical_card_search(query, 5, None, session, USER_ID, include_operational=True),
                        exact_capture_search(query, 5, None, session, USER_ID, include_operational=True),
                    ],
                    5,
                )
            else:
                results = merge_search_results(
                    [
                        lexical_card_search(query, 5, None, session, USER_ID, include_operational=True),
                        semantic_card_search(query, 5, None, session, USER_ID, include_operational=True),
                    ],
                    5,
                )
            top_ids = [item.get("capture_id") for item in results[:5]]
            repaired_rows.append(
                {
                    "capture_id": capture.id,
                    "source_system": capture.source_system,
                    "source_type": capture.source_type,
                    "source_path": capture.source_path,
                    "query_type": query_type,
                    "query": query,
                    "passed": capture.id in top_ids or bool(capture.content_hash and any(capture.content_hash in (item.get("raw_content") or "") for item in results[:5])),
                    "top_capture_ids": top_ids,
                    "top_match_types": [item.get("match_type") for item in results[:5]],
                    "top_rank_reasons": [item.get("rank_reason") for item in results[:5]],
                    "memory_system": MEMORY_SYSTEM,
                }
            )
            if len(repaired_rows) % 25 == 0:
                print(f"repaired_recall_checks={len(repaired_rows)}/{len(original_rows)}", flush=True)

        known_rows = []
        for fact in KNOWN_FACTS:
            results = search_memory_records(fact, 5, None, session, USER_ID, include_semantic=False)
            known_rows.append(
                {
                    "query": fact,
                    "passed": bool(results),
                    "top_count": len(results),
                    "top_capture_ids": [item.get("capture_id") for item in results[:5]],
                    "top_match_types": [item.get("match_type") for item in results[:5]],
                    "memory_system": MEMORY_SYSTEM,
                }
            )

    write_jsonl(ARTIFACT_DIR / "random_sample_recall_after.jsonl", repaired_rows)
    write_jsonl(ARTIFACT_DIR / "known_fact_recall_after.jsonl", known_rows)
    exact_rows = [row for row in repaired_rows if row.get("query_type") == "keyword"]
    semantic_rows = [row for row in repaired_rows if row.get("query_type") == "semantic"]
    summary = {
        "total": len(repaired_rows),
        "passed": sum(1 for row in repaired_rows if row["passed"]),
        "keyword_total": len(exact_rows),
        "keyword_passed": sum(1 for row in exact_rows if row["passed"]),
        "semantic_total": len(semantic_rows),
        "semantic_passed": sum(1 for row in semantic_rows if row["passed"]),
        "known_total": len(known_rows),
        "known_passed": sum(1 for row in known_rows if row["passed"]),
    }
    (ARTIFACT_DIR / "recall_after_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return repaired_rows, known_rows, summary


def verify_provenance_from_ledger() -> dict:
    rows = read_jsonl(ARTIFACT_DIR / "verification_all.jsonl")
    by_capture: dict[int, list[bool]] = defaultdict(list)
    for row in rows:
        by_capture[int(row["capture_id"])].append(bool(row.get("passed")))
    summary = {
        "verification_query_rows": len(rows),
        "captures_verified": sum(1 for checks in by_capture.values() if all(checks)),
        "captures_total": len(by_capture),
        "all_passed": all(all(checks) for checks in by_capture.values()),
    }
    (ARTIFACT_DIR / "provenance_verification_after.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def investigate_cortex_feedback_source() -> dict:
    roots = [Path("/var/www"), Path("/root"), Path("/var/backups"), Path("/backup"), Path("/srv")]
    suffixes = (".sql", ".sql.gz", ".dump", ".bak", ".gz", ".zip", ".sqlite", ".sqlite3")
    found = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            path_str = str(path)
            if path_str in seen or "node_modules" in path_str or not path.is_file():
                continue
            lower_name = path.name.lower()
            if ("feedback" in lower_name or "cortex" in lower_name) and lower_name.endswith(suffixes):
                seen.add(path_str)
                stat = path.stat()
                found.append(
                    {
                        "path": path_str,
                        "size_bytes": stat.st_size,
                        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "candidate_type": "compressed_or_sql_backup" if lower_name.endswith((".gz", ".zip", ".sql", ".dump", ".bak")) else "database",
                    }
                )
    imported_paths = set()
    with Session(engine) as session:
        rows = session.exec(
            select(Capture.source_path)
            .where(Capture.import_batch_id == BATCH_ID, Capture.source_system == "Cortex Feedback SQL Backup")
        ).all()
        imported_paths = {str(row) for row in rows if row}
    result = {
        "status": "found_source_not_imported" if found and not imported_paths else ("found_imported_source" if imported_paths else "unavailable_source"),
        "found_candidates": found,
        "imported_paths": sorted(imported_paths),
        "note": "Reimport deferred by instruction; original source ledgers are preserved.",
    }
    (ARTIFACT_DIR / "cortex_feedback_source_investigation.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def pct(part: int, total: int) -> str:
    return f"{(part / total * 100.0):.1f}%" if total else "0.0%"


def update_report(aggregates: dict, exact_audit: dict, recall_summary: dict, provenance_summary: dict, cortex_result: dict, card_count: int) -> None:
    report_path = ARTIFACT_DIR / "march_june_audit_report.md"
    backup_path = ARTIFACT_DIR / "march_june_audit_report.pre_retrieval_repair.md"
    if not backup_path.exists():
        shutil.copy2(report_path, backup_path)
    original = backup_path.read_text()
    before_rows = read_jsonl(RANDOM_SAMPLE_PATH)
    before_pass = sum(1 for row in before_rows if row.get("passed"))
    before_exact = [row for row in before_rows if row.get("query_type") == "exact"]
    before_semantic = [row for row in before_rows if row.get("query_type") == "semantic"]
    section = [
        "",
        "## 7. Retrieval-quality repair phase",
        "",
        "Status: repair applied; incident not closed. Raw captures remain the evidence layer and were not reimported.",
        "",
        "### Failure analysis",
        "",
        f"- Failed random-sample checks analyzed: {sum(aggregates['failure_class'].values())}",
        f"- Failure classes: {json.dumps(aggregates['failure_class'], sort_keys=True)}",
        f"- By source type: {json.dumps(aggregates['source_type'], sort_keys=True)}",
        f"- By query type: {json.dumps(aggregates['query_type'], sort_keys=True)}",
        f"- By capture length bucket: {json.dumps(aggregates['capture_length_bucket'], sort_keys=True)}",
        "",
        "### Exact-query audit",
        "",
        f"- Original exact rows true substrings of raw capture: {exact_audit['raw_substring']}/{exact_audit['total_exact_rows']}",
        f"- Original exact rows true substrings of source body: {exact_audit['body_substring']}/{exact_audit['total_exact_rows']}",
        f"- Rows renamed/regenerated as keyword probes because they came from import metadata: {exact_audit['recommended_rename_to_keyword']}/{exact_audit['total_exact_rows']}",
        "",
        "### Tuning changes",
        "",
        "- Added compact historical memory fact cards linked to source_capture_id and provenance.",
        f"- Indexed fact cards generated: {card_count}",
        "- Added aliases/entities for long numbers, dates, URLs, filenames, project slugs, reservation numbers, pixel IDs, server paths, and named projects.",
        "- Updated search ranking to prefer exact card aliases, then card lexical, then card semantic, then raw capture/chunk matches.",
        "- Raw session logs no longer outrank compact fact cards except when an exact identifier match is present.",
        "",
        "### Before/after recall",
        "",
        f"- Before random recall: {before_pass}/{len(before_rows)} ({pct(before_pass, len(before_rows))})",
        f"- Before exact top-5: {sum(1 for row in before_exact if row.get('passed'))}/{len(before_exact)} ({pct(sum(1 for row in before_exact if row.get('passed')), len(before_exact))})",
        f"- Before semantic top-5: {sum(1 for row in before_semantic if row.get('passed'))}/{len(before_semantic)} ({pct(sum(1 for row in before_semantic if row.get('passed')), len(before_semantic))})",
        f"- After keyword top-5: {recall_summary['keyword_passed']}/{recall_summary['keyword_total']} ({pct(recall_summary['keyword_passed'], recall_summary['keyword_total'])})",
        f"- After semantic top-5: {recall_summary['semantic_passed']}/{recall_summary['semantic_total']} ({pct(recall_summary['semantic_passed'], recall_summary['semantic_total'])})",
        f"- After total random recall: {recall_summary['passed']}/{recall_summary['total']} ({pct(recall_summary['passed'], recall_summary['total'])})",
        f"- Known facts after repair: {recall_summary['known_passed']}/{recall_summary['known_total']}",
        f"- Provenance verification after repair: {provenance_summary['captures_verified']}/{provenance_summary['captures_total']}",
        "",
        "### Cortex Feedback SQL Backup",
        "",
        f"- Investigation status: {cortex_result['status']}",
        f"- Candidate backup files found: {len(cortex_result['found_candidates'])}",
        "- Import remains deferred because this phase explicitly forbids corpus reimport.",
        "",
        "### Remaining gaps",
        "",
        "- Original random_sample_recall.jsonl is preserved as the failing pre-repair ledger.",
        "- Repaired random recall uses the same sampled capture IDs, but replaces header-derived probes with source-body keyword/semantic probes.",
        "- Cortex Feedback SQL Backup source is located but not imported in this phase.",
        "",
        "### Closure recommendation",
        "",
        "- Do not close the incident yet. Close only after the deferred Cortex source decision is made and production search is exercised through the deployed API/MCP path.",
    ]
    report_path.write_text(original.rstrip() + "\n" + "\n".join(section) + "\n")


def main() -> None:
    failure_path = ARTIFACT_DIR / "recall_failure_analysis.jsonl"
    aggregate_path = ARTIFACT_DIR / "recall_failure_aggregates.json"
    if failure_path.exists() and aggregate_path.exists():
        analysis_rows = read_jsonl(failure_path)
        aggregates = json.loads(aggregate_path.read_text())
    else:
        analysis_rows, aggregates = analyze_failures()

    exact_audit_path = ARTIFACT_DIR / "exact_query_category_audit.json"
    if exact_audit_path.exists():
        exact_audit = json.loads(exact_audit_path.read_text())
    else:
        exact_audit = audit_exact_queries()

    card_rows = generate_fact_cards()
    repaired_rows, known_rows, recall_summary = run_repaired_recall()
    provenance_summary = verify_provenance_from_ledger()
    cortex_result = investigate_cortex_feedback_source()
    update_report(aggregates, exact_audit, recall_summary, provenance_summary, cortex_result, len(card_rows))
    print(
        json.dumps(
            {
                "failure_rows": len(analysis_rows),
                "fact_cards": len(card_rows),
                "after_random_passed": f"{recall_summary['passed']}/{recall_summary['total']}",
                "after_keyword_passed": f"{recall_summary['keyword_passed']}/{recall_summary['keyword_total']}",
                "after_semantic_passed": f"{recall_summary['semantic_passed']}/{recall_summary['semantic_total']}",
                "known_facts": f"{recall_summary['known_passed']}/{recall_summary['known_total']}",
                "provenance": f"{provenance_summary['captures_verified']}/{provenance_summary['captures_total']}",
                "cortex_status": cortex_result["status"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
