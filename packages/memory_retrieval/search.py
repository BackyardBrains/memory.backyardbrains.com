"""Semantic (vector) and lexical (full-text) search logic."""

from typing import List, Optional

from sqlmodel import Session, select
from sqlmodel.sql.expression import col

# Import Chunk after db is set up to avoid circular imports
# from db.schema import Chunk


def semantic_search(
    session: Session,
    query_embedding: List[float],
    user_id: str,
    *,
    limit: int = 10,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> List:
    """Find chunks by cosine similarity to query embedding."""
    from db.schema import Chunk

    stmt = select(Chunk).where(col(Chunk.user_id) == user_id)
    if project_id is not None:
        stmt = stmt.where(col(Chunk.project_id) == project_id)
    if task_id is not None:
        stmt = stmt.where(col(Chunk.task_id) == task_id)

    # pgvector cosine distance: <=> operator, order by nearest first
    stmt = stmt.order_by(Chunk.embedding.cosine_distance(query_embedding)).limit(limit)
    return list(session.exec(stmt).all())
