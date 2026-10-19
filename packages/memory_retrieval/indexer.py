"""Retryable indexing of source sections and extractive evidence cards."""
import json
from datetime import datetime
from sqlalchemy import delete, text
from sqlmodel import Session, select
from db.engine import engine
from db.schema import Capture, Chunk, MemoryFactCard
from .embeddings import compute_embeddings, get_model

CARD_SOURCE = "extractive-card-v1"

def source_sections(content: str, token_limit: int = 360, overlap: int = 40) -> list[str]:
    if not content.strip():
        return []
    tokenizer = get_model().tokenizer
    offsets = tokenizer(content, add_special_tokens=False, return_offsets_mapping=True,
                        truncation=False)["offset_mapping"]
    if not offsets:
        return [content]
    sections = []
    for start in range(0, len(offsets), token_limit - overlap):
        end = min(start + token_limit, len(offsets))
        sections.append(content[offsets[start][0]:offsets[end - 1][1]])
        if end == len(offsets):
            break
    return sections

def compact_excerpt(content: str) -> str:
    if len(content) <= 900:
        return content
    boundary = max(content.rfind(". ", 450, 900), content.rfind("\n", 450, 900))
    return content[:boundary + 1 if boundary >= 450 else 900].rstrip() + " … [source excerpt]"

def upsert_capture_chunk(session: Session, capture: Capture) -> Chunk | None:
    if not capture.id or capture.memory_status != "active":
        return None
    sections = source_sections(capture.raw_content)
    old = list(session.exec(select(Chunk).where(Chunk.capture_id == capture.id, Chunk.user_id == capture.user_id).order_by(Chunk.id)).all())
    # Reuse a vector only when its stored source text is byte-for-byte identical.
    # Most old short captures already have a complete vector; backfill their cards cheaply.
    cached = {c.content: c.embedding for c in old if c.embedding is not None}
    missing = list(dict.fromkeys(s for s in sections if s not in cached))
    cached.update(zip(missing, compute_embeddings(missing)))
    vectors = [cached[s] for s in sections]
    first = None
    for i, (content, vector) in enumerate(zip(sections, vectors)):
        chunk = old[i] if i < len(old) else Chunk(user_id=capture.user_id, capture_id=capture.id, content=content, embedding=vector)
        chunk.content, chunk.embedding = content, vector
        session.add(chunk)
        first = first or chunk
    for stale in old[len(sections):]:
        session.delete(stale)
    session.exec(delete(MemoryFactCard).where(MemoryFactCard.source_capture_id == capture.id, MemoryFactCard.source_system == CARD_SOURCE, MemoryFactCard.revision == 1, MemoryFactCard.revision_actor.is_(None)))
    if capture.memory_visibility in {"knowledge", "policy"}:
        excerpts = sections  # each source section is already bounded to 360 tokens
        card_vectors = vectors
        for i, (excerpt, vector) in enumerate(zip(excerpts, card_vectors)):
            session.add(MemoryFactCard(
                source_capture_id=capture.id, content=excerpt, embedding=vector,
                project_slug=capture.project_slug, source_system=CARD_SOURCE,
                source_type="extractive", source_path=capture.source_path,
                observed_at=capture.observed_at or capture.created_at,
                memory_visibility=capture.memory_visibility,
                historical_status="historical" if capture.historical_until_verified else "current",
                provenance_json=json.dumps({"capture_id": capture.id, "capture_revision": capture.revision, "section": i, "extraction": "verbatim excerpt"}),
            ))
    capture.index_state = "ready"
    capture.index_error = None
    capture.index_updated_at = datetime.utcnow()
    session.add(capture)
    return first

def process_capture(capture_id: int):
    try:
        with Session(engine) as session:
            session.execute(text("SELECT pg_advisory_xact_lock(61015, :id)"), {"id": capture_id})
            capture = session.get(Capture, capture_id)
            if not capture or capture.memory_status != "active":
                return
            upsert_capture_chunk(session, capture)
            session.commit()
    except Exception as exc:
        with Session(engine) as session:
            capture = session.get(Capture, capture_id)
            if capture:
                capture.index_state = "error"
                capture.index_error = type(exc).__name__
                capture.index_updated_at = datetime.utcnow()
                session.add(capture)
                session.commit()
        raise
