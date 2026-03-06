import sys
from pathlib import Path

# Provide access to the rest of the application
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlmodel import Session
from db.engine import engine
from db.schema import Capture, Chunk
from .embeddings import compute_embedding

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

        # Simple approach: one chunk per capture
        vector = compute_embedding(capture.raw_content)

        chunk = Chunk(
            content=capture.raw_content,
            embedding=vector,
            user_id=capture.user_id,
            capture_id=capture.id
        )
        session.add(chunk)
        session.commit()
