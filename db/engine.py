"""Database engine and session factory."""

import os
from sqlmodel import Session, create_engine, text

from db.schema import SQLModel

# Default for local dev; override via DATABASE_URL
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://byb_memory:super_secret_local_password@localhost:5433/openbrain",
)

engine = create_engine(DATABASE_URL, echo=os.getenv("DB_ECHO", "false").lower() == "true")


def init_db() -> None:
    """Create all tables. Use Alembic for migrations in production."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """Dependency-style session factory."""
    return Session(engine)
