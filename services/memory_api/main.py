"""Memory API: versioned HTTP/JSON domain API with API Key authentication."""

import os
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Header, status, BackgroundTasks
from pydantic import BaseModel
from sqlmodel import Session, select
from sqlalchemy import or_, text

from db.engine import engine, init_db
from db.schema import Capture, Chunk, Event, Link, Person, Project, Task
import sys
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
SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_SYSTEM = "BYB Shared Memory"
LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
URL_RE = re.compile(r"https?://\S+")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_:\-./@]{5,}$")
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)),
    ("api_key", re.compile(r"\b(?:sk|pk|rk|pat|ghp|gho|ghu|ghs)_[A-Za-z0-9_\-]{16,}\b")),
    ("password", re.compile(r"\b(password|passwd|pwd)\s*[:=]\s*[^\s,;]+", re.IGNORECASE)),
    ("token", re.compile(r"\b(token|secret|api[_-]?key)\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)),
)


def _git_value(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(SERVICE_ROOT), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def service_metadata() -> dict:
    dirty = bool(_git_value("status", "--short"))
    return {
        "service": "Memory API",
        "status": "ok",
        "environment": os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "production")),
        "git_sha": os.getenv("GIT_SHA") or _git_value("rev-parse", "HEAD"),
        "git_branch": os.getenv("GIT_BRANCH") or _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_worktree": dirty,
        "deployed_at": os.getenv("DEPLOYED_AT"),
        "version": "2026.06.03-memory-stabilization",
        "canonical_memory": MEMORY_SYSTEM,
        "hybrid_search_enabled": True,
        "sync_capture_enabled": True,
        "write_verify_enabled": True,
    }


def redact_secrets(text_value: str) -> str:
    redacted = text_value or ""
    for kind, pattern in SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            value = match.group(0)
            return f"[REDACTED_SECRET:kind={kind},last4={value[-4:]}]"

        redacted = pattern.sub(repl, redacted)
    return redacted


def should_capture_memory(raw_content: str, source: str, tool_status: str | None = None) -> tuple[bool, str]:
    text_value = (raw_content or "").strip()
    lowered = text_value.lower()
    if not text_value:
        return False, "empty capture"
    if tool_status and tool_status.lower() not in {"ok", "success", "verified"}:
        return False, "source tool failed"

    junk_phrases = (
        "nothing found",
        "no results",
        "unable to access",
        "tool failed",
        "error fetching",
        "no recent captures",
        "all quiet",
        "0 tasks processed",
        "heartbeat_ok",
    )
    if any(phrase in lowered for phrase in junk_phrases) and len(text_value) < 700:
        return False, "junk/empty tool output"

    durable_markers = (
        "greg decided",
        "decision:",
        "task:",
        "deadline",
        "reservation",
        "pixel",
        "token",
        "project",
        "status:",
        "remember",
        "confirmed",
        "blocked",
        "[project:",
        "[task-current]",
        "[project-status-current]",
    )
    if not any(marker in lowered for marker in durable_markers) and len(text_value) < 80:
        return False, "not durable enough"
    return True, "ok"


def choose_verification_query(raw_content: str) -> str:
    text_value = raw_content or ""
    long_numbers = LONG_NUMBER_RE.findall(text_value)
    if long_numbers:
        return long_numbers[0]
    urls = URL_RE.findall(text_value)
    if urls:
        return urls[0].rstrip(").,;")
    words = re.findall(r"[A-Za-z0-9_\-]{4,}", text_value)
    return " ".join(words[:8]) if words else text_value[:80]


def is_identifier_query(q: str) -> bool:
    query = (q or "").strip()
    return bool(LONG_NUMBER_RE.search(query) or IDENTIFIER_RE.match(query))


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
    return service_metadata()


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
    sync: bool = False,
    verify_q: str | None = None,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Capture a raw note and optionally verify it is immediately retrievable."""
    allowed, reason = should_capture_memory(body.raw_content, body.source)
    if not allowed:
        raise HTTPException(status_code=422, detail={"message": "Capture rejected", "reason": reason})

    safe_content = redact_secrets(body.raw_content)
    capture = Capture(raw_content=safe_content, source=body.source, user_id=user_id)
    session.add(capture)
    session.commit()
    session.refresh(capture)

    if not sync:
        background_tasks.add_task(process_capture, capture.id)
        data = capture.model_dump()
        data.update(
            {
                "capture_id": capture.id,
                "indexed": False,
                "verified": False,
                "status": "queued_for_indexing",
                "memory_system": MEMORY_SYSTEM,
            }
        )
        return data

    process_capture(capture.id)
    chunks = list(session.exec(select(Chunk).where(Chunk.capture_id == capture.id)).all())
    verification_query = verify_q or choose_verification_query(safe_content)
    verification_results = search_memory_records(
        q=verification_query,
        limit=5,
        project_slug=None,
        session=session,
        user_id=user_id,
        include_semantic=not is_identifier_query(verification_query),
    )
    verified_match = next((row for row in verification_results if row.get("capture_id") == capture.id), None)
    data = capture.model_dump()
    data.update(
        {
            "capture_id": capture.id,
            "indexed": bool(chunks),
            "verified": bool(verified_match),
            "verification_query": verification_query,
            "verified_result_id": verified_match.get("id") if verified_match else None,
            "status": "verified" if verified_match else "stored_but_not_verified",
            "memory_system": MEMORY_SYSTEM,
        }
    )
    return data


@app.get("/v1/captures/{capture_id}")
def get_capture(
    capture_id: int,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    capture = session.get(Capture, capture_id)
    if not capture or capture.user_id != user_id:
        raise HTTPException(status_code=404, detail="Capture not found")
    data = capture.model_dump()
    data["capture_id"] = capture.id
    data["memory_system"] = MEMORY_SYSTEM
    return data


@app.get("/v1/captures/{capture_id}/chunks")
def get_capture_chunks(
    capture_id: int,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    capture = session.get(Capture, capture_id)
    if not capture or capture.user_id != user_id:
        raise HTTPException(status_code=404, detail="Capture not found")
    chunks = list(session.exec(select(Chunk).where(Chunk.capture_id == capture_id, Chunk.user_id == user_id)).all())
    return {"capture_id": capture_id, "chunks": chunks, "indexed": bool(chunks), "memory_system": MEMORY_SYSTEM}


@app.get("/v1/captures/{capture_id}/verification")
def get_capture_verification(
    capture_id: int,
    verify_q: str | None = None,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    capture = session.get(Capture, capture_id)
    if not capture or capture.user_id != user_id:
        raise HTTPException(status_code=404, detail="Capture not found")
    query = verify_q or choose_verification_query(capture.raw_content)
    chunks = list(session.exec(select(Chunk).where(Chunk.capture_id == capture_id, Chunk.user_id == user_id)).all())
    results = search_memory_records(q=query, limit=5, project_slug=None, session=session, user_id=user_id)
    verified_match = next((row for row in results if row.get("capture_id") == capture_id), None)
    return {
        "capture_id": capture_id,
        "indexed": bool(chunks),
        "verified": bool(verified_match),
        "verification_query": query,
        "verified_result_id": verified_match.get("id") if verified_match else None,
        "match_type": verified_match.get("match_type") if verified_match else None,
        "memory_system": MEMORY_SYSTEM,
    }


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


def _project_match_conditions(project_slug: str | None, session: Session):
    if not project_slug:
        return None, None
    project = session.exec(select(Project).where(Project.slug == project_slug)).first()
    project_tag = f"%[project: {project_slug}]%"
    return project, project_tag


def _chunk_by_capture(session: Session, capture_ids: set[int], user_id: str) -> dict[int, Chunk]:
    if not capture_ids:
        return {}
    chunks = session.exec(select(Chunk).where(Chunk.user_id == user_id, Chunk.capture_id.in_(capture_ids))).all()
    by_capture: dict[int, Chunk] = {}
    for chunk in chunks:
        if chunk.capture_id and chunk.capture_id not in by_capture:
            by_capture[chunk.capture_id] = chunk
    return by_capture


def _format_result(
    capture: Capture,
    *,
    chunk: Chunk | None = None,
    match_type: str,
    rank_reason: str,
    score: float,
) -> dict:
    return {
        "id": capture.id,
        "capture_id": capture.id,
        "chunk_id": chunk.id if chunk else None,
        "raw_content": capture.raw_content,
        "content": chunk.content if chunk else capture.raw_content,
        "created_at": capture.created_at,
        "source": capture.source,
        "match_type": match_type,
        "rank_reason": rank_reason,
        "score": score,
        "memory_system": MEMORY_SYSTEM,
    }


def _capture_project_filter(stmt, project_slug: str | None, session: Session):
    project, project_tag = _project_match_conditions(project_slug, session)
    if not project_slug:
        return stmt
    filters = [Capture.raw_content.ilike(project_tag)]
    if project:
        capture_ids = select(Chunk.capture_id).where(Chunk.project_id == project.id)
        filters.append(Capture.id.in_(capture_ids))
    return stmt.where(or_(*filters))


def _chunk_project_filter(stmt, project_slug: str | None, session: Session):
    project, project_tag = _project_match_conditions(project_slug, session)
    if not project_slug:
        return stmt
    filters = [Chunk.content.ilike(project_tag)]
    if project:
        filters.append(Chunk.project_id == project.id)
    return stmt.where(or_(*filters))


def exact_capture_search(q: str, limit: int, project_slug: str | None, session: Session, user_id: str) -> list[dict]:
    stmt = select(Capture).where(Capture.user_id == user_id, Capture.raw_content.ilike(f"%{q}%"))
    stmt = _capture_project_filter(stmt, project_slug, session)
    captures = list(session.exec(stmt.order_by(Capture.created_at.desc()).limit(limit)).all())
    chunks_by_capture = _chunk_by_capture(session, {c.id for c in captures if c.id}, user_id)
    reason = "substring match on identifier" if is_identifier_query(q) else "substring match"
    return [
        _format_result(capture, chunk=chunks_by_capture.get(capture.id), match_type="exact", rank_reason=reason, score=1000.0)
        for capture in captures
        if capture.id is not None
    ]


def lexical_capture_search(q: str, limit: int, project_slug: str | None, session: Session, user_id: str) -> list[dict]:
    params = {"q": q, "user_id": user_id, "limit": limit, "project_tag": f"%[project: {project_slug}]%" if project_slug else None}
    project, _ = _project_match_conditions(project_slug, session)
    params["project_id"] = project.id if project else None
    project_sql = ""
    if project_slug:
        project_sql = """
          AND (
            c.raw_content ILIKE :project_tag
            OR (:project_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM chunk pc WHERE pc.capture_id = c.id AND pc.project_id = :project_id
            ))
          )
        """
    sql = text(
        f"""
        SELECT c.id
        FROM capture c
        WHERE c.user_id = :user_id
          {project_sql}
          AND (
            to_tsvector('simple', coalesce(c.raw_content, '')) @@ plainto_tsquery('simple', :q)
            OR similarity(c.raw_content, :q) > 0.08
          )
        ORDER BY
          ts_rank(to_tsvector('simple', coalesce(c.raw_content, '')), plainto_tsquery('simple', :q)) DESC,
          similarity(c.raw_content, :q) DESC,
          c.created_at DESC
        LIMIT :limit
        """
    )
    try:
        rows = session.execute(sql, params).fetchall()
        capture_ids = [row[0] for row in rows]
    except Exception:
        session.rollback()
        words = [word for word in re.findall(r"[A-Za-z0-9_\-]{3,}", q) if len(word) >= 3]
        stmt = select(Capture).where(Capture.user_id == user_id)
        if words:
            stmt = stmt.where(or_(*[Capture.raw_content.ilike(f"%{word}%") for word in words[:6]]))
        else:
            stmt = stmt.where(Capture.raw_content.ilike(f"%{q}%"))
        stmt = _capture_project_filter(stmt, project_slug, session)
        capture_ids = [capture.id for capture in session.exec(stmt.order_by(Capture.created_at.desc()).limit(limit)).all() if capture.id]

    if not capture_ids:
        return []
    captures = session.exec(select(Capture).where(Capture.id.in_(capture_ids), Capture.user_id == user_id)).all()
    captures_by_id = {capture.id: capture for capture in captures}
    chunks_by_capture = _chunk_by_capture(session, set(capture_ids), user_id)
    return [
        _format_result(
            captures_by_id[capture_id],
            chunk=chunks_by_capture.get(capture_id),
            match_type="lexical",
            rank_reason="full-text/trigram match",
            score=500.0 - idx,
        )
        for idx, capture_id in enumerate(capture_ids)
        if capture_id in captures_by_id
    ]


def semantic_chunk_search(q: str, limit: int, project_slug: str | None, session: Session, user_id: str) -> list[dict]:
    query_vector = compute_embedding(q)
    stmt = select(Chunk).where(Chunk.user_id == user_id)
    stmt = _chunk_project_filter(stmt, project_slug, session)
    chunks = list(session.exec(stmt.order_by(Chunk.embedding.cosine_distance(query_vector)).limit(limit)).all())
    capture_ids = {chunk.capture_id for chunk in chunks if chunk.capture_id}
    captures_by_id = {}
    if capture_ids:
        captures = session.exec(select(Capture).where(Capture.id.in_(capture_ids), Capture.user_id == user_id)).all()
        captures_by_id = {capture.id: capture for capture in captures}
    results = []
    for idx, chunk in enumerate(chunks):
        capture = captures_by_id.get(chunk.capture_id)
        if not capture:
            continue
        results.append(
            _format_result(
                capture,
                chunk=chunk,
                match_type="semantic",
                rank_reason="vector similarity",
                score=100.0 - idx,
            )
        )
    return results


def merge_search_results(groups: list[list[dict]], limit: int) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[int | None, int | None]] = set()
    for group in groups:
        for item in group:
            key = (item.get("capture_id"), item.get("chunk_id"))
            capture_key = (item.get("capture_id"), None)
            if key in seen or capture_key in seen:
                continue
            seen.add(key)
            seen.add(capture_key)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def recent_capture_records(limit: int, project_slug: str | None, session: Session, user_id: str) -> list[dict]:
    stmt = select(Capture).where(Capture.user_id == user_id)
    stmt = _capture_project_filter(stmt, project_slug, session)
    captures = list(session.exec(stmt.order_by(Capture.created_at.desc()).limit(limit)).all())
    chunks_by_capture = _chunk_by_capture(session, {c.id for c in captures if c.id}, user_id)
    return [
        _format_result(capture, chunk=chunks_by_capture.get(capture.id), match_type="recent", rank_reason="recent capture", score=0.0)
        for capture in captures
        if capture.id is not None
    ]


def search_memory_records(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_semantic: bool = True,
) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 200))
    query = (q or "").strip()
    if not query:
        return recent_capture_records(bounded_limit, project_slug, session, user_id)

    exact = exact_capture_search(query, bounded_limit, project_slug, session, user_id)
    lexical = lexical_capture_search(query, bounded_limit, project_slug, session, user_id)
    semantic = []
    if include_semantic:
        try:
            semantic = semantic_chunk_search(query, bounded_limit, project_slug, session, user_id)
        except Exception:
            session.rollback()
            semantic = []
    if is_identifier_query(query):
        return merge_search_results([exact, lexical, semantic], bounded_limit)
    return merge_search_results([exact, lexical, semantic], bounded_limit)


@app.get("/v1/search")
def search_memory(
    q: str = "",
    limit: int = 10,
    project_slug: str | None = None,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Hybrid exact, lexical, then semantic search."""
    return search_memory_records(q=q, limit=limit, project_slug=project_slug, session=session, user_id=user_id)


# --- MCP Adapter Mount ---
app.mount("/mcp", mcp_app)
