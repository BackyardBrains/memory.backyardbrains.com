import sys
from pathlib import Path

# Provide access to the rest of the application
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlmodel import Session, select
from db.engine import engine
from db.schema import Capture, Chunk
from .embeddings import compute_embedding


def upsert_capture_chunk(session: Session, capture: Capture) -> Chunk | None:
    """Create or refresh the one semantic chunk for an active capture."""
    if not capture.id or getattr(capture, "memory_status", "active") != "active":
        return None

    vector = compute_embedding(capture.raw_content)
    chunks = list(session.exec(
        select(Chunk).where(Chunk.capture_id == capture.id, Chunk.user_id == capture.user_id)
    ).all())
    if chunks:
        chunk = chunks[0]
        for existing_chunk in chunks:
            existing_chunk.content = capture.raw_content
            existing_chunk.embedding = vector
            session.add(existing_chunk)
    else:
        chunk = Chunk(
            content=capture.raw_content,
            embedding=vector,
            user_id=capture.user_id,
            capture_id=capture.id,
        )
        session.add(chunk)
    return chunk


def process_capture(capture_id: int):
    """
    Background worker that takes a new Capture, generates the embedding,
    and inserts a corresponding Chunk into the database.
    (In a real implementation, you might chunk long texts; here we map 1:1 for simplicity)
    """
    with Session(engine) as session:
        capture = session.get(Capture, capture_id)
        if not capture:
            return

        upsert_capture_chunk(session, capture)
        session.commit()
