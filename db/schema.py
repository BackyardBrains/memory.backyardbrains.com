"""SQLModel schema definitions for the canonical memory store.

Uses concrete relational tables for deterministic data (projects, tasks, persons, events)
and an embeddings-backed Chunk table for semantic/fuzzy retrieval via pgvector.
"""

from typing import List, Optional
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
from sqlalchemy import Column, Text
from pgvector.sqlalchemy import Vector

# Vector dimension: BAAI/bge-small-en-v1.5 and all-MiniLM-L6-v2 both output 384
VECTOR_DIM = 384

# ---------------------------------------------------------
# RELATIONAL STATE (The "Left Brain")
# ---------------------------------------------------------

class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True) # e.g., "nimh-grant"
    title: str
    status: str = Field(default="Active")
    priority: str = Field(default="Normal")
    category: Optional[str] = None  # e.g., "neuro", "plant", "invertebrate", "human"
    last_activity_at: Optional[datetime] = None  # Last meaningful change (Watson steward)
    waiting_on: Optional[str] = None  # e.g., "greg" or "external:Moritz"

    # Relationships
    tasks: List["Task"] = Relationship(back_populates="project")
    chunks: List["Chunk"] = Relationship(back_populates="project")

class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    description: str
    status: str = Field(default="To Do") # To Do, In Progress, Complete, Deferred
    snooze_until: Optional[datetime] = None # For delayed/snoozed tasks
    due_date: Optional[datetime] = None
    draft_text: Optional[str] = Field(default=None, sa_column=Column(Text))  # Prepared email/reply body (Watson)
    state: Optional[str] = None  # Cortex triage state: "ready", "decision", "deep", "plain" (tolerant)

    project_id: Optional[int] = Field(default=None, foreign_key="project.id")
    project: Optional[Project] = Relationship(back_populates="tasks")
    
    chunks: List["Chunk"] = Relationship(back_populates="task")

class Person(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True) # e.g., "maribel"
    name: str
    role: Optional[str] = None

class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    label: str
    date_time: datetime
    location: Optional[str] = None

    chunks: List["Chunk"] = Relationship(back_populates="event")


class Link(SQLModel, table=True):
    """Project-related documents/URLs (Drive folders, docs, external links)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)  # e.g., "nsta-drive-folder"
    label: str
    url: str  # description in JSON-LD
    folder_id: Optional[str] = None  # Google Drive folder/doc ID
    note: Optional[str] = None
    policy: Optional[str] = None  # e.g., "READ_ONLY"
    project_id: Optional[int] = Field(default=None, foreign_key="project.id")
    updated_at: Optional[datetime] = None

# ---------------------------------------------------------
# PROVENANCE
# ---------------------------------------------------------

class Source(SQLModel, table=True):
    """Provenance tracking for data origin (e.g., slack, claude-ios, watson-cron)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    identifier: str = Field(index=True, unique=True)  # e.g., "slack-channel-x"
    kind: str  # e.g., "slack", "claude-ios", "watson"
    meta_json: Optional[str] = None  # Extra provenance as JSON

# ---------------------------------------------------------
# CAPTURE & SEMANTIC MEMORY (The "Right Brain")
# ---------------------------------------------------------

class Capture(SQLModel, table=True):
    """Raw, unstructured input from the user/agent before processing."""
    id: Optional[int] = Field(default=None, primary_key=True)
    raw_content: str
    source: str  # e.g., "claude-ios", "watson-cron", "slack"
    user_id: str = Field(index=True)  # Maps to the API Key used (e.g., "greg")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_system: Optional[str] = Field(default=None, index=True)
    source_path: Optional[str] = Field(default=None, index=True)
    source_type: Optional[str] = None
    observed_at: Optional[datetime] = Field(default=None, index=True)
    imported_at: Optional[datetime] = Field(default=None, index=True)
    content_hash: Optional[str] = Field(default=None, index=True)
    import_batch_id: Optional[str] = Field(default=None, index=True)
    historical_until_verified: bool = Field(default=False, index=True)

    chunks: List["Chunk"] = Relationship(back_populates="capture")


class Chunk(SQLModel, table=True):
    """Vectorized text segments linked back to deterministic state."""
    id: Optional[int] = Field(default=None, primary_key=True)
    content: str

    # pgvector column (384 dims: BAAI/bge-small-en-v1.5, all-MiniLM-L6-v2)
    embedding: List[float] = Field(sa_column=Column(Vector(VECTOR_DIM)))

    created_at: datetime = Field(default_factory=datetime.utcnow)
    user_id: str = Field(index=True)  # Isolation for semantic search

    # Polymorphic-style linkages back to concrete tables
    project_id: Optional[int] = Field(default=None, foreign_key="project.id")
    project: Optional[Project] = Relationship(back_populates="chunks")

    task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    task: Optional[Task] = Relationship(back_populates="chunks")

    event_id: Optional[int] = Field(default=None, foreign_key="event.id")
    event: Optional[Event] = Relationship(back_populates="chunks")

    capture_id: Optional[int] = Field(default=None, foreign_key="capture.id")
    capture: Optional[Capture] = Relationship(back_populates="chunks")


class MemoryFactCard(SQLModel, table=True):
    """Compact retrieval card derived from a raw capture.

    Raw captures remain the evidence layer; these cards are the primary retrieval
    layer for historical memories.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    source_capture_id: int = Field(foreign_key="capture.id", index=True)
    content: str
    aliases_json: Optional[str] = None
    aliases_text: Optional[str] = Field(default=None, index=True)
    entities_json: Optional[str] = None
    project_slug: Optional[str] = Field(default=None, index=True)
    source_system: Optional[str] = Field(default=None, index=True)
    source_type: Optional[str] = None
    source_path: Optional[str] = Field(default=None, index=True)
    observed_at: Optional[datetime] = Field(default=None, index=True)
    historical_status: Optional[str] = Field(default=None, index=True)
    memory_visibility: Optional[str] = Field(default="historical_evidence", index=True)
    provenance_json: Optional[str] = None
    embedding: List[float] = Field(sa_column=Column(Vector(VECTOR_DIM)))
    created_at: datetime = Field(default_factory=datetime.utcnow)
