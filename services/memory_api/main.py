"""Memory API: versioned HTTP/JSON domain API with API Key authentication."""

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Header, status, BackgroundTasks
from pydantic import BaseModel
from sqlmodel import Session, select
from sqlalchemy import or_

from db.engine import engine, init_db
from db.schema import Capture, Chunk, Event, Link, Person, Project, Task
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from packages.memory_retrieval.indexer import process_capture
from packages.memory_retrieval.embeddings import compute_embedding

from services.memory_mcp.server import mcp
mcp_app = mcp.http_app(path="/")

class CaptureBody(BaseModel):
    raw_content: str
    source: str = "unknown"


class TaskBody(BaseModel):
    description: str
    status: str = "To Do"
    project_id: int | None = None
    due_date: str | None = None
    snooze_until: str | None = None


class TaskStatusBody(BaseModel):
    status: str


class ProjectBody(BaseModel):
    slug: str
    title: str
    status: str = "Active"
    priority: str = "Normal"


class EventBody(BaseModel):
    label: str
    date_time: str  # ISO format
    location: str | None = None


class PersonBody(BaseModel):
    slug: str
    name: str
    role: str | None = None


class LinkBody(BaseModel):
    slug: str
    label: str
    url: str
    folder_id: str | None = None
    note: str | None = None
    policy: str | None = None
    project_slug: str | None = None


# API Key: sk_byb_{user_id} e.g. sk_byb_greg_xxxx
API_KEY_PREFIX = "sk_byb_"


def get_user_from_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Extract user_id from API key. Keys must match sk_byb_{user_id}_* pattern."""
    if not x_api_key or not x_api_key.startswith(API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Use X-API-Key: sk_byb_{user_id}_...",
        )
    parts = x_api_key[len(API_KEY_PREFIX) :].split("_", 1)
    user_id = parts[0] if parts else ""
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format.",
        )
    # Optionally validate against a store of allowed keys
    allowed = os.getenv("MEMORY_API_KEYS", "").split(",")
    if allowed and allowed[0] and x_api_key not in [k.strip() for k in allowed if k.strip()]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key not authorized.",
        )
    return user_id


def get_db_session():
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(
    title="Memory API",
    description="Canonical memory service for OpenClaw",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/projects", response_model=list)
def list_projects(
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """List projects (no user filter on projects for now; scope later)."""
    stmt = select(Project)
    return list(session.exec(stmt).all())


@app.get("/v1/projects/{slug}")
def get_project(slug: str, session: Session = Depends(get_db_session), user_id: str = Depends(get_user_from_api_key)):
    proj = session.exec(select(Project).where(Project.slug == slug)).first()
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


@app.post("/v1/projects")
def create_project(
    body: ProjectBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    existing = session.exec(select(Project).where(Project.slug == body.slug)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Project slug '{body.slug}' already exists")
    proj = Project(slug=body.slug, title=body.title, status=body.status, priority=body.priority)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


@app.get("/v1/tasks")
def list_tasks(
    project_slug: str | None = None,
    status: str | None = None,
    include_complete: bool = True,
    limit: int = 50,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    stmt = select(Task)
    if project_slug:
        proj = session.exec(select(Project).where(Project.slug == project_slug)).first()
        if proj:
            stmt = stmt.where(Task.project_id == proj.id)
    if status:
        stmt = stmt.where(Task.status.ilike(f"%{status}%"))
    if not include_complete:
        stmt = stmt.where(
            ~Task.status.ilike("%complete%"),
            ~Task.status.ilike("%deferred%"),
            ~Task.status.ilike("%✅%"),
        )
    stmt = stmt.limit(min(limit, 200))
    tasks = list(session.exec(stmt).all())
    # Enrich with project slug for Watson
    result = []
    for t in tasks:
        d = t.model_dump()
        if t.project_id:
            p = session.get(Project, t.project_id)
            d["partOf"] = f"byb:project/{p.slug}" if p else None
        else:
            d["partOf"] = None
        d["@id"] = f"byb:task/{t.id}"
        d["dueDate"] = d.get("due_date")
        result.append(d)
    return result


@app.post("/v1/events")
def create_event(
    body: EventBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    from datetime import datetime

    dt = datetime.fromisoformat(body.date_time.replace("Z", "+00:00"))
    evt = Event(label=body.label, date_time=dt, location=body.location)
    session.add(evt)
    session.commit()
    session.refresh(evt)
    return evt


@app.get("/v1/events")
def list_events(
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    stmt = select(Event)
    if from_date:
        stmt = stmt.where(Event.date_time >= from_date)
    if to_date:
        stmt = stmt.where(Event.date_time <= to_date)
    stmt = stmt.order_by(Event.date_time).limit(min(limit, 200))
    events = list(session.exec(stmt).all())
    return [{"@id": f"byb:event/{e.id}", "label": e.label, "date": e.date_time.strftime("%Y-%m-%d"), "dateTime": e.date_time.isoformat()} for e in events]


@app.get("/v1/links")
def list_links(
    project_slug: str | None = None,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Links/documents for projects. Watson-compatible: docs with id, label, url."""
    stmt = select(Link)
    if project_slug:
        proj = session.exec(select(Project).where(Project.slug == project_slug)).first()
        if proj:
            stmt = stmt.where(Link.project_id == proj.id)
    links = list(session.exec(stmt).all())
    return {
        "docs": [
            {"@id": f"byb:link/{l.slug}", "id": f"byb:link/{l.slug}", "label": l.label, "url": l.url, "description": l.url}
            for l in links
        ]
    }


@app.post("/v1/links")
def create_link(
    body: LinkBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    project_id = None
    if body.project_slug:
        proj = session.exec(select(Project).where(Project.slug == body.project_slug)).first()
        if proj:
            project_id = proj.id
    existing = session.exec(select(Link).where(Link.slug == body.slug)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Link slug '{body.slug}' already exists")
    link = Link(
        slug=body.slug,
        label=body.label,
        url=body.url,
        folder_id=body.folder_id,
        note=body.note,
        policy=body.policy,
        project_id=project_id,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


@app.post("/v1/persons")
def create_person(
    body: PersonBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    existing = session.exec(select(Person).where(Person.slug == body.slug)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Person slug '{body.slug}' already exists")
    person = Person(slug=body.slug, name=body.name, role=body.role)
    session.add(person)
    session.commit()
    session.refresh(person)
    return person


@app.post("/v1/captures")
def create_capture(
    body: CaptureBody,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Capture a raw note (inbox) and generate semantic embedding."""
    capture = Capture(raw_content=body.raw_content, source=body.source, user_id=user_id)
    session.add(capture)
    session.commit()
    session.refresh(capture)
    
    # Trigger background indexing
    background_tasks.add_task(process_capture, capture.id)
    
    return capture


@app.get("/v1/tasks/{task_id}")
def get_task(
    task_id: int,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/v1/tasks")
def create_task(
    body: TaskBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    from datetime import datetime

    due_dt = datetime.fromisoformat(body.due_date.replace("Z", "+00:00")) if body.due_date else None
    snooze_dt = datetime.fromisoformat(body.snooze_until.replace("Z", "+00:00")) if body.snooze_until else None
    task = Task(
        description=body.description,
        status=body.status,
        project_id=body.project_id,
        due_date=due_dt,
        snooze_until=snooze_dt,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@app.patch("/v1/tasks/{task_id}")
def set_task_status(
    task_id: int,
    body: TaskStatusBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.status = body.status
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@app.get("/v1/search")
def search_memory(
    q: str = "",
    limit: int = 10,
    project_slug: str | None = None,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Semantic search using vector similarity."""
    if not q:
        # Fallback to recent captures if query is empty
        stmt = select(Capture).where(Capture.user_id == user_id).order_by(Capture.created_at.desc()).limit(limit)
        return list(session.exec(stmt).all())

    # 1. Compute embedding of the search query
    query_vector = compute_embedding(q)

    # 2. Vector search using pgvector cosine distance (<=>)
    stmt = (
        select(Chunk)
        .where(Chunk.user_id == user_id)
        .order_by(Chunk.embedding.cosine_distance(query_vector))
        .limit(limit)
    )
    results = session.exec(stmt).all()
    
    # 3. Format back as capture-style response for compatibility
    return [{"raw_content": c.content, "created_at": c.created_at, "id": c.capture_id} for c in results]


# --- MCP Adapter Mount ---
app.mount("/mcp", mcp_app)
