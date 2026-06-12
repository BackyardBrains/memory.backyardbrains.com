"""Memory API: versioned HTTP/JSON domain API with API Key authentication."""

import json
import os
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

<<<<<<< HEAD
from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel
=======
from fastapi import Depends, FastAPI, HTTPException, Header, status, BackgroundTasks
from pydantic import BaseModel, field_validator
>>>>>>> 2852dbe3669bbeb314cd86f160e53bfcafa1bf35
from sqlmodel import Session, select
from sqlalchemy import or_, text

from db.engine import engine, init_db
from db.schema import Capture, Chunk, Event, Link, MemoryFactCard, MemoryRevision, Person, Project, Task
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from packages.memory_retrieval.indexer import process_capture, upsert_capture_chunk
from packages.memory_retrieval.embeddings import compute_embedding

from services.memory_mcp.server import mcp
mcp_app = mcp.http_app(path="/")

class CaptureBody(BaseModel):
    raw_content: str
    source: str = "unknown"
    source_system: str | None = None
    source_path: str | None = None
    source_type: str | None = None
    observed_at: datetime | None = None
    imported_at: datetime | None = None
    content_hash: str | None = None
    import_batch_id: str | None = None
    historical_until_verified: bool = False


<<<<<<< HEAD
class CapturePatchBody(BaseModel):
    raw_content: str | None = None
    source: str | None = None
    source_system: str | None = None
    source_path: str | None = None
    source_type: str | None = None
    observed_at: datetime | None = None
    imported_at: datetime | None = None
    content_hash: str | None = None
    import_batch_id: str | None = None
    historical_until_verified: bool | None = None
    memory_status: str | None = None
    superseded_by_capture_id: int | None = None
    revision_reason: str | None = None
    revision_actor: str | None = None
    source_message_ids: list[str] | None = None
    idempotency_key: str | None = None
    expected_revision: int | None = None
    reindex: bool = True


class CaptureDeleteBody(BaseModel):
    reason: str | None = None
    actor: str | None = None
    source_message_ids: list[str] | None = None
    idempotency_key: str | None = None
    expected_revision: int | None = None
=======
# Known Cortex task states; tolerant — any short string is accepted (no hard enum).
KNOWN_TASK_STATES = {"ready", "decision", "deep", "plain"}
MAX_TASK_STATE_LENGTH = 64


def _validate_task_state(value: str | None) -> str | None:
    if value is None:
        return value
    state = value.strip()
    if not state:
        return None
    if state.lower() in KNOWN_TASK_STATES:
        return state.lower()
    if len(state) > MAX_TASK_STATE_LENGTH:
        raise ValueError(f"state must be at most {MAX_TASK_STATE_LENGTH} characters")
    return state
>>>>>>> 2852dbe3669bbeb314cd86f160e53bfcafa1bf35


class TaskBody(BaseModel):
    description: str
    status: str = "To Do"
    project_id: int | None = None
    due_date: str | None = None
    snooze_until: str | None = None
    draft_text: str | None = None
    state: str | None = None

    _validate_state = field_validator("state")(_validate_task_state)


class TaskStatusBody(BaseModel):
    """PATCH body for tasks. All fields optional; only provided fields are updated."""
    status: str | None = None
    draft_text: str | None = None
    state: str | None = None

    _validate_state = field_validator("state")(_validate_task_state)


class ProjectBody(BaseModel):
    slug: str
    title: str
    status: str = "Active"
    priority: str = "Normal"
    category: str | None = None
    last_activity_at: datetime | None = None
    waiting_on: str | None = None


class ProjectPatchBody(BaseModel):
    """PATCH body for projects. All fields optional; only provided fields are updated."""
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    category: str | None = None
    last_activity_at: datetime | None = None
    waiting_on: str | None = None


class ProjectPatchBody(BaseModel):
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    revision_reason: str | None = None
    revision_actor: str | None = None
    source_message_ids: list[str] | None = None
    idempotency_key: str | None = None


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
ACTIVE_MEMORY_STATUS = "active"
VALID_MEMORY_STATUSES = {"active", "superseded", "retracted", "duplicate", "stale", "deleted"}
INACTIVE_MEMORY_STATUSES = VALID_MEMORY_STATUSES - {ACTIVE_MEMORY_STATUS}
LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
URL_RE = re.compile(r"https?://\S+")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_:\-./@]{5,}$")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@'-]{1,}")
OPERATIONAL_RECALL_VISIBILITIES = {"automation_heartbeat", "operational_audit"}
# Email index captures ("[email]...", "[email][waiting-on-greg]...", etc. or source
# claude-desktop-1) are bulk mirrors of the inbox: keep them findable (dedup by
# threadId relies on exact/lexical lookup) but demote them on the semantic path so
# they don't crowd out organic memories of similar semantic score.
EMAIL_INDEX_CONTENT_PREFIX = "[email]"
EMAIL_INDEX_SOURCES = {"claude-desktop-1"}
EMAIL_INDEX_DEMOTION_FACTOR = float(os.getenv("EMAIL_INDEX_DEMOTION_FACTOR", "0.65"))
OPERATIONAL_QUERY_RE = re.compile(
    r"\b(agent operations?|cron|health checks?|heartbeat|midday pulse|business hours sync|"
    r"night watch|morning briefing|afternoon wrap|memory infrastructure|operational audit)\b",
    re.IGNORECASE,
)
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
        "version": "2026.06.10-memory-corrections",
        "canonical_memory": MEMORY_SYSTEM,
        "hybrid_search_enabled": True,
        "sync_capture_enabled": True,
        "write_verify_enabled": True,
        "memory_correction_enabled": True,
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


def normalize_memory_status(value: str) -> str:
    status_value = (value or "").strip().lower()
    if status_value not in VALID_MEMORY_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid memory_status",
                "allowed": sorted(VALID_MEMORY_STATUSES),
            },
        )
    return status_value


def is_inactive_memory_status(value: str | None) -> bool:
    return (value or ACTIVE_MEMORY_STATUS).strip().lower() in INACTIVE_MEMORY_STATUSES


def _utcnow() -> datetime:
    return datetime.utcnow()


def _require_reason(reason: str | None) -> str:
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise HTTPException(status_code=422, detail="revision_reason is required for memory updates/deletes")
    return clean_reason


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_dumps(value) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default)


CAPTURE_REVISION_FIELDS = (
    "raw_content",
    "source",
    "user_id",
    "created_at",
    "updated_at",
    "memory_status",
    "revision",
    "superseded_by_capture_id",
    "deleted_at",
    "revision_reason",
    "revision_actor",
    "revision_source_ids_json",
    "source_system",
    "source_path",
    "source_type",
    "observed_at",
    "imported_at",
    "content_hash",
    "import_batch_id",
    "historical_until_verified",
)
CARD_REVISION_FIELDS = (
    "source_capture_id",
    "content",
    "aliases_json",
    "aliases_text",
    "entities_json",
    "project_slug",
    "source_system",
    "source_type",
    "source_path",
    "observed_at",
    "historical_status",
    "memory_visibility",
    "memory_status",
    "revision",
    "superseded_by_card_id",
    "updated_at",
    "deleted_at",
    "revision_reason",
    "revision_actor",
    "revision_source_ids_json",
    "provenance_json",
    "created_at",
)
PROJECT_REVISION_FIELDS = ("slug", "title", "status", "priority")


def revision_snapshot(record, fields: tuple[str, ...]) -> dict:
    return {field: getattr(record, field, None) for field in fields}


def _source_ids_json(source_message_ids: list[str] | None) -> str | None:
    if not source_message_ids:
        return None
    return _json_dumps([str(item) for item in source_message_ids if str(item).strip()])


def _existing_revision_for_key(
    session: Session,
    *,
    target_type: str,
    target_id: int,
    user_id: str,
    idempotency_key: str | None,
) -> MemoryRevision | None:
    if not idempotency_key:
        return None
    return session.exec(
        select(MemoryRevision).where(
            MemoryRevision.target_type == target_type,
            MemoryRevision.target_id == target_id,
            MemoryRevision.user_id == user_id,
            MemoryRevision.idempotency_key == idempotency_key,
        )
    ).first()


def _record_revision(
    session: Session,
    *,
    target_type: str,
    target_id: int,
    user_id: str,
    action: str,
    before: dict,
    after: dict,
    reason: str,
    actor: str,
    source_message_ids: list[str] | None,
    idempotency_key: str | None,
) -> MemoryRevision:
    revision = MemoryRevision(
        target_type=target_type,
        target_id=target_id,
        user_id=user_id,
        action=action,
        before_json=_json_dumps(before),
        after_json=_json_dumps(after),
        reason=reason,
        actor=actor,
        source_ids_json=_source_ids_json(source_message_ids),
        idempotency_key=idempotency_key,
    )
    session.add(revision)
    return revision


def _project_response(project: Project, *, action: str | None = None, revision_id: int | None = None, idempotent: bool = False) -> dict:
    data = project.model_dump()
    if action:
        data["action"] = action
    if revision_id is not None:
        data["revision_id"] = revision_id
    if idempotent:
        data["idempotent"] = True
    return data


def _capture_response(capture: Capture, action: str, revision_id: int | None = None, idempotent: bool = False) -> dict:
    data = capture.model_dump()
    data.update(
        {
            "capture_id": capture.id,
            "memory_id": capture.id,
            "action": action,
            "revision_id": revision_id,
            "idempotent": idempotent,
            "memory_system": MEMORY_SYSTEM,
        }
    )
    return data


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
    proj = Project(
        slug=body.slug,
        title=body.title,
        status=body.status,
        priority=body.priority,
        category=body.category,
        last_activity_at=body.last_activity_at,
        waiting_on=body.waiting_on,
    )
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


@app.patch("/v1/projects/{slug}")
def patch_project(
    slug: str,
    body: ProjectPatchBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    proj = session.exec(select(Project).where(Project.slug == slug)).first()
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(proj, field, value)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


@app.patch("/v1/projects/{slug}")
def patch_project(
    slug: str,
    body: ProjectPatchBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Update a structured project row with an audit revision."""
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.id is None:
        raise HTTPException(status_code=500, detail="Project row has no id")

    existing_revision = _existing_revision_for_key(
        session,
        target_type="project",
        target_id=project.id,
        user_id=user_id,
        idempotency_key=body.idempotency_key,
    )
    if existing_revision:
        return _project_response(project, action="update", revision_id=existing_revision.id, idempotent=True)

    incoming = body.model_dump(exclude_unset=True)
    patch_values = {key: value for key, value in incoming.items() if key in {"title", "status", "priority"}}
    if not patch_values:
        raise HTTPException(status_code=422, detail="At least one mutable project field is required")

    reason = _require_reason(body.revision_reason)
    actor = (body.revision_actor or user_id).strip() or user_id
    before = revision_snapshot(project, PROJECT_REVISION_FIELDS)

    for field, value in patch_values.items():
        if value is None or not str(value).strip():
            raise HTTPException(status_code=422, detail=f"{field} cannot be blank")
        setattr(project, field, str(value).strip())

    after = revision_snapshot(project, PROJECT_REVISION_FIELDS)
    if before == after:
        return _project_response(project, action="update", idempotent=True)

    session.add(project)
    revision = _record_revision(
        session,
        target_type="project",
        target_id=project.id,
        user_id=user_id,
        action="update",
        before=before,
        after=after,
        reason=reason,
        actor=actor,
        source_message_ids=body.source_message_ids,
        idempotency_key=body.idempotency_key,
    )
    session.commit()
    session.refresh(project)
    session.refresh(revision)
    return _project_response(project, action="update", revision_id=revision.id)


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
    capture = Capture(
        raw_content=safe_content,
        source=body.source,
        user_id=user_id,
        source_system=body.source_system,
        source_path=body.source_path,
        source_type=body.source_type,
        observed_at=body.observed_at,
        imported_at=body.imported_at,
        content_hash=body.content_hash,
        import_batch_id=body.import_batch_id,
        historical_until_verified=body.historical_until_verified,
    )
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


CAPTURE_PATCH_FIELDS = {
    "raw_content",
    "source",
    "source_system",
    "source_path",
    "source_type",
    "observed_at",
    "imported_at",
    "content_hash",
    "import_batch_id",
    "historical_until_verified",
    "memory_status",
    "superseded_by_capture_id",
}


def _get_owned_capture(session: Session, capture_id: int, user_id: str) -> Capture:
    capture = session.get(Capture, capture_id)
    if not capture or capture.user_id != user_id:
        raise HTTPException(status_code=404, detail="Capture not found")
    return capture


def _check_expected_revision(capture: Capture, expected_revision: int | None) -> None:
    if expected_revision is not None and capture.revision != expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Capture revision conflict",
                "expected_revision": expected_revision,
                "current_revision": capture.revision,
            },
        )


def _validate_supersession_target(
    session: Session,
    *,
    capture: Capture,
    superseded_by_capture_id: int | None,
    user_id: str,
) -> None:
    if superseded_by_capture_id is None:
        return
    if superseded_by_capture_id == capture.id:
        raise HTTPException(status_code=422, detail="A capture cannot supersede itself")
    replacement = session.get(Capture, superseded_by_capture_id)
    if not replacement or replacement.user_id != user_id:
        raise HTTPException(status_code=422, detail="superseded_by_capture_id must reference one of this user's captures")


def _touch_capture_revision(
    capture: Capture,
    *,
    now: datetime,
    reason: str,
    actor: str,
    source_message_ids: list[str] | None,
) -> None:
    capture.updated_at = now
    capture.revision = (capture.revision or 1) + 1
    capture.revision_reason = reason
    capture.revision_actor = actor
    capture.revision_source_ids_json = _source_ids_json(source_message_ids)


def _touch_card_revision(
    card: MemoryFactCard,
    *,
    now: datetime,
    reason: str,
    actor: str,
    source_message_ids: list[str] | None,
) -> None:
    card.updated_at = now
    card.revision = (card.revision or 1) + 1
    card.revision_reason = reason
    card.revision_actor = actor
    card.revision_source_ids_json = _source_ids_json(source_message_ids)


def _mark_derived_cards(
    session: Session,
    *,
    capture: Capture,
    status_value: str,
    now: datetime,
    reason: str,
    actor: str,
    source_message_ids: list[str] | None,
    idempotency_key: str | None,
) -> list[int]:
    if not capture.id:
        return []
    cards = list(session.exec(select(MemoryFactCard).where(MemoryFactCard.source_capture_id == capture.id)).all())
    changed_card_ids: list[int] = []
    for card in cards:
        if card.memory_status == status_value:
            continue
        before = revision_snapshot(card, CARD_REVISION_FIELDS)
        card.memory_status = status_value
        if status_value == "deleted":
            card.deleted_at = card.deleted_at or now
        elif status_value == ACTIVE_MEMORY_STATUS:
            card.deleted_at = None
        _touch_card_revision(card, now=now, reason=reason, actor=actor, source_message_ids=source_message_ids)
        session.add(card)
        _record_revision(
            session,
            target_type="memory_fact_card",
            target_id=card.id,
            user_id=capture.user_id,
            action="derived_status",
            before=before,
            after=revision_snapshot(card, CARD_REVISION_FIELDS),
            reason=reason,
            actor=actor,
            source_message_ids=source_message_ids,
            idempotency_key=idempotency_key,
        )
        if card.id is not None:
            changed_card_ids.append(card.id)
    return changed_card_ids


@app.patch("/v1/memories/{capture_id}")
@app.patch("/v1/captures/{capture_id}")
def patch_capture(
    capture_id: int,
    body: CapturePatchBody,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Correct, supersede, retract, or otherwise revise a memory capture."""
    capture = _get_owned_capture(session, capture_id, user_id)
    existing_revision = _existing_revision_for_key(
        session,
        target_type="capture",
        target_id=capture_id,
        user_id=user_id,
        idempotency_key=body.idempotency_key,
    )
    if existing_revision:
        return _capture_response(capture, action="update", revision_id=existing_revision.id, idempotent=True)

    _check_expected_revision(capture, body.expected_revision)
    reason = _require_reason(body.revision_reason)
    actor = (body.revision_actor or user_id).strip() or user_id
    incoming = body.model_dump(exclude_unset=True)
    patch_values = {key: value for key, value in incoming.items() if key in CAPTURE_PATCH_FIELDS}
    if not patch_values:
        raise HTTPException(status_code=422, detail="At least one mutable memory field is required")

    before = revision_snapshot(capture, CAPTURE_REVISION_FIELDS)
    now = _utcnow()
    content_changed = False

    if "raw_content" in patch_values:
        if patch_values["raw_content"] is None:
            raise HTTPException(status_code=422, detail="raw_content cannot be null")
        safe_content = redact_secrets(patch_values["raw_content"])
        source_for_validation = patch_values.get("source") or capture.source
        allowed, reject_reason = should_capture_memory(safe_content, source_for_validation)
        if not allowed:
            raise HTTPException(status_code=422, detail={"message": "Capture update rejected", "reason": reject_reason})
        content_changed = safe_content != capture.raw_content
        capture.raw_content = safe_content

    if "source" in patch_values:
        source_value = (patch_values["source"] or "").strip()
        if not source_value:
            raise HTTPException(status_code=422, detail="source cannot be blank")
        capture.source = source_value

    for field in (
        "source_system",
        "source_path",
        "source_type",
        "observed_at",
        "imported_at",
        "content_hash",
        "import_batch_id",
    ):
        if field in patch_values:
            setattr(capture, field, patch_values[field])

    if "historical_until_verified" in patch_values:
        if patch_values["historical_until_verified"] is None:
            raise HTTPException(status_code=422, detail="historical_until_verified cannot be null")
        capture.historical_until_verified = bool(patch_values["historical_until_verified"])

    if "superseded_by_capture_id" in patch_values:
        _validate_supersession_target(
            session,
            capture=capture,
            superseded_by_capture_id=patch_values["superseded_by_capture_id"],
            user_id=user_id,
        )
        capture.superseded_by_capture_id = patch_values["superseded_by_capture_id"]
        if patch_values["superseded_by_capture_id"] and "memory_status" not in patch_values:
            capture.memory_status = "superseded"

    if "memory_status" in patch_values:
        if patch_values["memory_status"] is None:
            raise HTTPException(status_code=422, detail="memory_status cannot be null")
        capture.memory_status = normalize_memory_status(patch_values["memory_status"])

    if capture.memory_status == "deleted":
        capture.deleted_at = capture.deleted_at or now
    elif capture.memory_status == ACTIVE_MEMORY_STATUS:
        capture.deleted_at = None

    after_without_revision = revision_snapshot(capture, CAPTURE_REVISION_FIELDS)
    if before == after_without_revision:
        raise HTTPException(status_code=422, detail="Patch did not change the memory")

    _touch_capture_revision(capture, now=now, reason=reason, actor=actor, source_message_ids=body.source_message_ids)
    session.add(capture)

    derived_card_status: str | None = None
    if is_inactive_memory_status(capture.memory_status):
        derived_card_status = capture.memory_status
    elif content_changed:
        derived_card_status = "stale"
    changed_card_ids = []
    if derived_card_status:
        changed_card_ids = _mark_derived_cards(
            session,
            capture=capture,
            status_value=derived_card_status,
            now=now,
            reason=reason,
            actor=actor,
            source_message_ids=body.source_message_ids,
            idempotency_key=body.idempotency_key,
        )

    if content_changed and capture.memory_status == ACTIVE_MEMORY_STATUS and body.reindex:
        upsert_capture_chunk(session, capture)

    revision = _record_revision(
        session,
        target_type="capture",
        target_id=capture_id,
        user_id=user_id,
        action="update",
        before=before,
        after=revision_snapshot(capture, CAPTURE_REVISION_FIELDS),
        reason=reason,
        actor=actor,
        source_message_ids=body.source_message_ids,
        idempotency_key=body.idempotency_key,
    )
    session.commit()
    session.refresh(capture)
    session.refresh(revision)
    data = _capture_response(capture, action="update", revision_id=revision.id)
    data["derived_card_status"] = derived_card_status
    data["derived_card_ids"] = changed_card_ids
    data["reindexed"] = bool(content_changed and capture.memory_status == ACTIVE_MEMORY_STATUS and body.reindex)
    return data


@app.delete("/v1/memories/{capture_id}")
@app.delete("/v1/captures/{capture_id}")
def delete_capture(
    capture_id: int,
    body: CaptureDeleteBody | None = Body(default=None),
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Soft-delete a memory capture by tombstoning it and its derived cards."""
    body = body or CaptureDeleteBody()
    capture = _get_owned_capture(session, capture_id, user_id)
    existing_revision = _existing_revision_for_key(
        session,
        target_type="capture",
        target_id=capture_id,
        user_id=user_id,
        idempotency_key=body.idempotency_key,
    )
    if existing_revision:
        return _capture_response(capture, action="delete", revision_id=existing_revision.id, idempotent=True)

    _check_expected_revision(capture, body.expected_revision)
    reason = _require_reason(body.reason)
    actor = (body.actor or user_id).strip() or user_id
    if capture.memory_status == "deleted":
        return _capture_response(capture, action="delete", revision_id=None, idempotent=True)

    before = revision_snapshot(capture, CAPTURE_REVISION_FIELDS)
    now = _utcnow()
    capture.memory_status = "deleted"
    capture.deleted_at = now
    _touch_capture_revision(capture, now=now, reason=reason, actor=actor, source_message_ids=body.source_message_ids)
    session.add(capture)
    changed_card_ids = _mark_derived_cards(
        session,
        capture=capture,
        status_value="deleted",
        now=now,
        reason=reason,
        actor=actor,
        source_message_ids=body.source_message_ids,
        idempotency_key=body.idempotency_key,
    )
    revision = _record_revision(
        session,
        target_type="capture",
        target_id=capture_id,
        user_id=user_id,
        action="delete",
        before=before,
        after=revision_snapshot(capture, CAPTURE_REVISION_FIELDS),
        reason=reason,
        actor=actor,
        source_message_ids=body.source_message_ids,
        idempotency_key=body.idempotency_key,
    )
    session.commit()
    session.refresh(capture)
    session.refresh(revision)
    data = _capture_response(capture, action="delete", revision_id=revision.id)
    data["derived_card_ids"] = changed_card_ids
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
        draft_text=body.draft_text,
        state=body.state,
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
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
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
        "source_system": capture.source_system,
        "source_path": capture.source_path,
        "source_type": capture.source_type,
        "observed_at": capture.observed_at,
        "imported_at": capture.imported_at,
        "content_hash": capture.content_hash,
        "import_batch_id": capture.import_batch_id,
        "historical_until_verified": capture.historical_until_verified,
        "memory_status": capture.memory_status,
        "revision": capture.revision,
        "updated_at": capture.updated_at,
        "deleted_at": capture.deleted_at,
        "superseded_by_capture_id": capture.superseded_by_capture_id,
        "match_type": match_type,
        "rank_reason": rank_reason,
        "score": score,
        "memory_system": MEMORY_SYSTEM,
    }


def _format_card_result(
    card: MemoryFactCard,
    capture: Capture,
    *,
    match_type: str,
    rank_reason: str,
    score: float,
) -> dict:
    return {
        "id": capture.id,
        "capture_id": capture.id,
        "chunk_id": None,
        "card_id": card.id,
        "raw_content": capture.raw_content,
        "content": card.content,
        "created_at": capture.created_at,
        "source": capture.source,
        "source_system": card.source_system or capture.source_system,
        "source_path": card.source_path or capture.source_path,
        "source_type": card.source_type or capture.source_type,
        "observed_at": card.observed_at or capture.observed_at,
        "imported_at": capture.imported_at,
        "content_hash": capture.content_hash,
        "import_batch_id": capture.import_batch_id,
        "historical_until_verified": capture.historical_until_verified,
        "source_capture_id": card.source_capture_id,
        "project_slug": card.project_slug,
        "historical_status": card.historical_status,
        "memory_visibility": card.memory_visibility or "historical_evidence",
        "memory_status": capture.memory_status,
        "revision": capture.revision,
        "updated_at": capture.updated_at,
        "deleted_at": capture.deleted_at,
        "superseded_by_capture_id": capture.superseded_by_capture_id,
        "card_memory_status": card.memory_status,
        "card_revision": card.revision,
        "match_type": match_type,
        "rank_reason": rank_reason,
        "score": score,
        "memory_system": MEMORY_SYSTEM,
    }


def _captures_for_cards(session: Session, cards: list[MemoryFactCard], user_id: str) -> dict[int, Capture]:
    capture_ids = {card.source_capture_id for card in cards if card.source_capture_id}
    if not capture_ids:
        return {}
    captures = session.exec(
        select(Capture).where(Capture.id.in_(capture_ids), Capture.user_id == user_id, _active_capture_condition())
    ).all()
    return {capture.id: capture for capture in captures if capture.id is not None}


def _capture_project_filter(stmt, project_slug: str | None, session: Session):
    project, project_tag = _project_match_conditions(project_slug, session)
    if not project_slug:
        return stmt
    filters = [Capture.raw_content.ilike(project_tag)]
    if project:
        capture_ids = select(Chunk.capture_id).where(Chunk.project_id == project.id)
        filters.append(Capture.id.in_(capture_ids))
    return stmt.where(or_(*filters))


def _query_requests_operational_memory(q: str) -> bool:
    return bool(OPERATIONAL_QUERY_RE.search(q or ""))


def _excluded_memory_visibilities(include_operational: bool) -> set[str]:
    return set() if include_operational else OPERATIONAL_RECALL_VISIBILITIES


def _active_capture_condition():
    return or_(Capture.memory_status.is_(None), Capture.memory_status == ACTIVE_MEMORY_STATUS)


def _active_card_condition():
    return or_(MemoryFactCard.memory_status.is_(None), MemoryFactCard.memory_status == ACTIVE_MEMORY_STATUS)


def _card_visibility_filter(stmt, include_operational: bool):
    stmt = stmt.where(_active_card_condition())
    excluded = _excluded_memory_visibilities(include_operational)
    if not excluded:
        return stmt
    return stmt.where(
        or_(
            MemoryFactCard.memory_visibility.is_(None),
            MemoryFactCard.memory_visibility.notin_(excluded),
        )
    )


def _excluded_capture_ids_stmt(include_operational: bool):
    excluded = _excluded_memory_visibilities(include_operational)
    if not excluded:
        return None
    return select(MemoryFactCard.source_capture_id).where(
        MemoryFactCard.memory_visibility.in_(excluded),
        _active_card_condition(),
    )


def _capture_visibility_filter(stmt, include_operational: bool):
    stmt = stmt.where(_active_capture_condition())
    excluded_capture_ids = _excluded_capture_ids_stmt(include_operational)
    if excluded_capture_ids is None:
        return stmt
    return stmt.where(~Capture.id.in_(excluded_capture_ids))


def _chunk_visibility_filter(stmt, include_operational: bool):
    inactive_capture_ids = select(Capture.id).where(Capture.memory_status.in_(INACTIVE_MEMORY_STATUSES))
    stmt = stmt.where(or_(Chunk.capture_id.is_(None), ~Chunk.capture_id.in_(inactive_capture_ids)))
    excluded_capture_ids = _excluded_capture_ids_stmt(include_operational)
    if excluded_capture_ids is None:
        return stmt
    return stmt.where(or_(Chunk.capture_id.is_(None), ~Chunk.capture_id.in_(excluded_capture_ids)))


def _card_project_filter(stmt, project_slug: str | None, include_operational: bool = False):
    stmt = _card_visibility_filter(stmt, include_operational)
    if not project_slug:
        return stmt
    return stmt.where(MemoryFactCard.project_slug == project_slug)


def _query_aliases(q: str) -> set[str]:
    query = (q or "").strip()
    if not query:
        return set()
    aliases = {query.lower()}
    if len(query.split()) > 2:
        return aliases
    aliases.update(match.group(0).lower() for match in URL_RE.finditer(query))
    aliases.update(match.group(0).lower() for match in LONG_NUMBER_RE.finditer(query))
    return aliases


def _lexical_tokens(value: str) -> list[str]:
    return [token.lower().strip(".,;:)]}\"'") for token in TOKEN_RE.findall(value or "") if len(token.strip(".,;:)]}\"'")) >= 3]


def _rare_token_score(query_tokens: list[str], text_value: str) -> float:
    haystack = (text_value or "").lower()
    score = 0.0
    for token in query_tokens:
        if token not in haystack:
            continue
        if URL_RE.fullmatch(token) or "/" in token or "." in token:
            score += 8.0
        elif LONG_NUMBER_RE.fullmatch(token):
            score += 7.0
        elif "-" in token or "_" in token:
            score += 5.0
        elif len(token) >= 12:
            score += 4.0
        elif token not in {"the", "and", "for", "from", "with", "that", "this", "now", "run", "check"}:
            score += 1.0
    return score


def _rerank_card_results(q: str, cards: list[MemoryFactCard], limit: int) -> list[MemoryFactCard]:
    query = (q or "").strip()
    query_lower = query.lower()
    query_tokens = _lexical_tokens(query)
    scored: list[tuple[float, int, MemoryFactCard]] = []
    for idx, card in enumerate(cards):
        text_value = f"{card.content or ''}\n{card.aliases_text or ''}"
        text_lower = text_value.lower()
        aliases = [alias.strip().lower() for alias in (card.aliases_text or "").splitlines() if alias.strip()]
        overlap = sum(1 for token in set(query_tokens) if token in text_lower)
        coverage = overlap / max(len(set(query_tokens)), 1)
        exact_phrase = 1.0 if query_lower and query_lower in text_lower else 0.0
        exact_alias = 1.0 if query_lower in aliases else 0.0
        rare_score = _rare_token_score(query_tokens, text_value)
        score = (
            exact_alias * 10000.0
            + exact_phrase * 5000.0
            + coverage * 1000.0
            + rare_score * 50.0
            + (len(set(query_tokens)) if coverage >= 0.8 else 0.0)
            - idx * 0.01
        )
        scored.append((score, idx, card))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [card for _score, _idx, card in scored[:limit]]


def exact_card_alias_search(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
    aliases = _query_aliases(q)
    if not aliases:
        return []
    stmt = select(MemoryFactCard)
    stmt = _card_project_filter(stmt, project_slug, include_operational)
    stmt = stmt.where(or_(*[MemoryFactCard.aliases_text.ilike(f"%{alias}%") for alias in aliases]))
    cards = list(session.exec(stmt.order_by(MemoryFactCard.observed_at.desc(), MemoryFactCard.id.desc()).limit(max(limit * 5, 50))).all())
    exact_cards = [
        card for card in cards
        if any(alias == token.strip().lower() for alias in aliases for token in (card.aliases_text or "").splitlines())
    ]
    captures_by_id = _captures_for_cards(session, exact_cards, user_id)
    results = []
    for idx, card in enumerate(exact_cards):
        capture = captures_by_id.get(card.source_capture_id)
        if not capture:
            continue
        results.append(
            _format_card_result(
                card,
                capture,
                match_type="entity_alias",
                rank_reason="exact entity/alias match on compact memory card",
                score=1200.0 - idx,
            )
        )
        if len(results) >= limit:
            break
    return results


def _chunk_project_filter(stmt, project_slug: str | None, session: Session):
    project, project_tag = _project_match_conditions(project_slug, session)
    if not project_slug:
        return stmt
    filters = [Chunk.content.ilike(project_tag)]
    if project:
        filters.append(Chunk.project_id == project.id)
    return stmt.where(or_(*filters))


def lexical_card_search(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
    candidate_limit = max(limit * 40, 250)
    params = {"q": q, "limit": candidate_limit, "project_slug": project_slug}
    project_sql = "AND project_slug = :project_slug" if project_slug else ""
    visibility_sql = "" if include_operational else "AND (memory_visibility IS NULL OR memory_visibility NOT IN ('automation_heartbeat', 'operational_audit'))"
    sql = text(
        f"""
        SELECT id
        FROM memoryfactcard
        WHERE 1=1
          AND coalesce(memory_status, 'active') = 'active'
          {project_sql}
          {visibility_sql}
          AND (
            to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(aliases_text, '')) @@ plainto_tsquery('simple', :q)
            OR similarity(coalesce(content, '') || ' ' || coalesce(aliases_text, ''), :q) > 0.08
            OR aliases_text ILIKE '%' || :q || '%'
          )
        ORDER BY
          CASE WHEN aliases_text ILIKE '%' || :q || '%' THEN 1 ELSE 0 END DESC,
          ts_rank(to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(aliases_text, '')), plainto_tsquery('simple', :q)) DESC,
          similarity(coalesce(content, '') || ' ' || coalesce(aliases_text, ''), :q) DESC,
          observed_at DESC NULLS LAST,
          id DESC
        LIMIT :limit
        """
    )
    try:
        rows = session.execute(sql, params).fetchall()
        card_ids = [row[0] for row in rows]
    except Exception:
        session.rollback()
        words = [word for word in re.findall(r"[A-Za-z0-9_\-./:]{3,}", q) if len(word) >= 3]
        stmt = select(MemoryFactCard)
        stmt = _card_project_filter(stmt, project_slug, include_operational)
        if words:
            stmt = stmt.where(or_(*[MemoryFactCard.content.ilike(f"%{word}%") for word in words[:6]]))
        else:
            stmt = stmt.where(MemoryFactCard.content.ilike(f"%{q}%"))
        card_ids = [card.id for card in session.exec(stmt.order_by(MemoryFactCard.observed_at.desc()).limit(candidate_limit)).all() if card.id]
    if not card_ids:
        return []
    cards = session.exec(select(MemoryFactCard).where(MemoryFactCard.id.in_(card_ids), _active_card_condition())).all()
    cards_by_id = {card.id: card for card in cards}
    ordered_cards = [cards_by_id[card_id] for card_id in card_ids if card_id in cards_by_id]
    ordered_cards = _rerank_card_results(q, ordered_cards, limit)
    captures_by_id = _captures_for_cards(session, ordered_cards, user_id)
    return [
        _format_card_result(
            card,
            captures_by_id[card.source_capture_id],
            match_type="card_lexical",
            rank_reason="compact memory-card lexical match",
            score=900.0 - idx,
        )
        for idx, card in enumerate(ordered_cards)
        if card.source_capture_id in captures_by_id
    ]


def semantic_card_search(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
    query_vector = compute_embedding(q)
    stmt = select(MemoryFactCard)
    stmt = _card_project_filter(stmt, project_slug, include_operational)
    cards = list(session.exec(stmt.order_by(MemoryFactCard.embedding.cosine_distance(query_vector)).limit(limit)).all())
    captures_by_id = _captures_for_cards(session, cards, user_id)
    return [
        _format_card_result(
            card,
            captures_by_id[card.source_capture_id],
            match_type="card_semantic",
            rank_reason="compact memory-card vector similarity",
            score=700.0 - idx,
        )
        for idx, card in enumerate(cards)
        if card.source_capture_id in captures_by_id
    ]


def exact_capture_search(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
    stmt = select(Capture).where(Capture.user_id == user_id, Capture.raw_content.ilike(f"%{q}%"))
    stmt = _capture_project_filter(stmt, project_slug, session)
    stmt = _capture_visibility_filter(stmt, include_operational)
    captures = list(session.exec(stmt.order_by(Capture.created_at.desc()).limit(limit)).all())
    chunks_by_capture = _chunk_by_capture(session, {c.id for c in captures if c.id}, user_id)
    reason = "substring match on identifier" if is_identifier_query(q) else "substring match"
    return [
        _format_result(capture, chunk=chunks_by_capture.get(capture.id), match_type="exact", rank_reason=reason, score=1000.0)
        for capture in captures
        if capture.id is not None
    ]


def lexical_capture_search(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
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
    visibility_sql = ""
    if not include_operational:
        visibility_sql = """
          AND NOT EXISTS (
            SELECT 1 FROM memoryfactcard mfc
            WHERE mfc.source_capture_id = c.id
              AND mfc.memory_visibility IN ('automation_heartbeat', 'operational_audit')
          )
        """
    sql = text(
        f"""
        SELECT c.id
        FROM capture c
        WHERE c.user_id = :user_id
          AND coalesce(c.memory_status, 'active') = 'active'
          {project_sql}
          {visibility_sql}
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
        stmt = _capture_visibility_filter(stmt, include_operational)
        capture_ids = [capture.id for capture in session.exec(stmt.order_by(Capture.created_at.desc()).limit(limit)).all() if capture.id]

    if not capture_ids:
        return []
    captures = session.exec(
        select(Capture).where(Capture.id.in_(capture_ids), Capture.user_id == user_id, _active_capture_condition())
    ).all()
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


def semantic_chunk_search(
    q: str,
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
    query_vector = compute_embedding(q)
    stmt = select(Chunk).where(Chunk.user_id == user_id)
    stmt = _chunk_project_filter(stmt, project_slug, session)
    stmt = _chunk_visibility_filter(stmt, include_operational)
    chunks = list(session.exec(stmt.order_by(Chunk.embedding.cosine_distance(query_vector)).limit(limit)).all())
    capture_ids = {chunk.capture_id for chunk in chunks if chunk.capture_id}
    captures_by_id = {}
    if capture_ids:
        captures = session.exec(
            select(Capture).where(Capture.id.in_(capture_ids), Capture.user_id == user_id, _active_capture_condition())
        ).all()
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


def _is_email_index_result(item: dict) -> bool:
    raw_content = (item.get("raw_content") or "").lstrip()
    return raw_content.startswith(EMAIL_INDEX_CONTENT_PREFIX) or item.get("source") in EMAIL_INDEX_SOURCES


def demote_email_index_results(results: list[dict]) -> list[dict]:
    """Demote email-index captures on the semantic path (never filters them out).

    Exact/lexical matches (threadId dedup lookups, substring hits) are left alone.
    """
    demoted_any = False
    for item in results:
        if _is_email_index_result(item) and item.get("score", 0.0) > 0.0:
            item["score"] = round(item["score"] * EMAIL_INDEX_DEMOTION_FACTOR, 4)
            item["rank_reason"] = f"{item['rank_reason']}; demoted x{EMAIL_INDEX_DEMOTION_FACTOR} (email index capture)"
            demoted_any = True
    if demoted_any:
        results.sort(key=lambda item: item.get("score", 0.0), reverse=True)
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


def is_historical_import_result(item: dict) -> bool:
    source = str(item.get("source") or "").lower()
    historical_status = str(item.get("historical_status") or "").lower()
    return bool(
        item.get("historical_until_verified")
        or is_inactive_memory_status(item.get("memory_status"))
        or source.startswith("historical-import")
        or historical_status in {"historical", "legacy", "archived", "stale", "superseded"}
    )


def split_historical_import_results(results: list[dict]) -> tuple[list[dict], list[dict]]:
    current: list[dict] = []
    historical: list[dict] = []
    for item in results:
        if is_historical_import_result(item):
            historical.append(item)
        else:
            current.append(item)
    return current, historical


def recent_capture_records(
    limit: int,
    project_slug: str | None,
    session: Session,
    user_id: str,
    include_operational: bool = False,
) -> list[dict]:
    stmt = select(Capture).where(Capture.user_id == user_id)
    stmt = _capture_project_filter(stmt, project_slug, session)
    stmt = _capture_visibility_filter(stmt, include_operational)
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
    include_operational: bool | None = None,
) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 200))
    query = (q or "").strip()
    include_operational = _query_requests_operational_memory(query) if include_operational is None else include_operational
    if not query:
        return recent_capture_records(bounded_limit, project_slug, session, user_id, include_operational)

    exact = exact_capture_search(query, bounded_limit, project_slug, session, user_id, include_operational)
    exact_current, exact_historical = split_historical_import_results(exact)
    card_exact = exact_card_alias_search(query, bounded_limit, project_slug, session, user_id, include_operational)
    card_lexical = lexical_card_search(query, bounded_limit, project_slug, session, user_id, include_operational)
    card_semantic = []
    lexical = lexical_capture_search(query, bounded_limit, project_slug, session, user_id, include_operational)
    lexical_current, lexical_historical = split_historical_import_results(lexical)
    semantic = []
    if include_semantic:
        try:
            card_semantic = demote_email_index_results(
                semantic_card_search(query, bounded_limit, project_slug, session, user_id, include_operational)
            )
            semantic = demote_email_index_results(
                semantic_chunk_search(query, bounded_limit, project_slug, session, user_id, include_operational)
            )
        except Exception:
            session.rollback()
            card_semantic = []
            semantic = []
    if is_identifier_query(query):
        return merge_search_results(
            [exact_current, card_exact, exact_historical, lexical_current, card_lexical, card_semantic, lexical_historical, semantic],
            bounded_limit,
        )
    return merge_search_results(
        [exact_current, card_exact, card_lexical, card_semantic, exact_historical, lexical_current, lexical_historical, semantic],
        bounded_limit,
    )


@app.get("/v1/search")
def search_memory(
    q: str = "",
    limit: int = 10,
    project_slug: str | None = None,
    include_operational: bool = False,
    session: Session = Depends(get_db_session),
    user_id: str = Depends(get_user_from_api_key),
):
    """Hybrid exact, lexical, then semantic search."""
    return search_memory_records(
        q=q,
        limit=limit,
        project_slug=project_slug,
        session=session,
        user_id=user_id,
        include_operational=True if include_operational else None,
    )


# --- MCP Adapter Mount ---
app.mount("/mcp", mcp_app)
