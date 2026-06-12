"""MCP adapter for the Memory API. Exposes resources, prompts, and tools to LLM clients."""

import os
from typing import Optional

import httpx
from fastmcp import FastMCP

# Memory API base URL (e.g., http://localhost:8001 when API runs there)
MEMORY_API_URL = os.getenv("MEMORY_API_URL", "http://localhost:8001")
# API key for MCP->API calls (format: sk_byb_{user_id}_...)
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "sk_byb_greg_local")


def _api_headers() -> dict:
    return {"X-API-Key": MEMORY_API_KEY, "Content-Type": "application/json"}


def _source_message_ids(value: Optional[str]) -> list[str] | None:
    if not value:
        return None
    ids = [item.strip() for item in value.split(",") if item.strip()]
    return ids or None


mcp = FastMCP(
    name="Memory MCP",
    instructions=(
        "BYB Shared Memory service. Use byb_memory_search for canonical recall and "
        "byb_memory_capture_verified for durable user memory. Use byb_memory_patch_capture "
        "or byb_memory_delete_capture to correct, supersede, retract, or tombstone stale "
        "captures with a reason and source message IDs. Do not claim a note was saved "
        "unless the verified capture tool reports verified=true."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
def search_memory(
    query: str = "",
    limit: int = 10,
    project_slug: Optional[str] = None,
) -> str:
    """Legacy alias for BYB Shared Memory search. Prefer byb_memory_search."""
    return byb_memory_search(query=query, limit=limit, project_slug=project_slug)


@mcp.tool
def byb_memory_search(
    query: str = "",
    limit: int = 10,
    project_slug: Optional[str] = None,
) -> str:
    """Search canonical BYB Shared Memory with exact, lexical, then semantic ranking."""
    params = {"q": query, "limit": limit}
    if project_slug:
        params["project_slug"] = project_slug
    r = httpx.get(
        f"{MEMORY_API_URL}/v1/search",
        params=params,
        headers=_api_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return "\n".join(
            f"- [{c.get('created_at', '')}] system={c.get('memory_system', 'BYB Shared Memory')} "
            f"match={c.get('match_type', 'unknown')} source={c.get('source', 'unknown')} "
            f"id={c.get('capture_id', c.get('id', '?'))} {c.get('raw_content', '')}"
            for c in data
        )
    return str(data)


@mcp.tool
def capture_note(raw_content: str, source: str = "mcp") -> str:
    """Legacy alias for verified BYB Shared Memory capture. Prefer byb_memory_capture_verified."""
    return byb_memory_capture_verified(raw_content=raw_content, source=source)


@mcp.tool
def byb_memory_capture_verified(raw_content: str, source: str = "mcp") -> str:
    """Save a durable note to BYB Shared Memory and verify read-back before success."""
    r = httpx.post(
        f"{MEMORY_API_URL}/v1/captures",
        params={"sync": "true"},
        json={"raw_content": raw_content, "source": source},
        headers=_api_headers(),
        timeout=60.0,
    )
    r.raise_for_status()
    out = r.json()
    capture_id = out.get("capture_id") or out.get("id") or "?"
    if out.get("verified"):
        return f"Captured and verified in BYB Shared Memory (id={capture_id})."
    if out.get("indexed"):
        return f"Stored and indexed in BYB Shared Memory, but read-back verification failed (id={capture_id}). Do not rely on this memory yet."
    return f"Stored raw capture in BYB Shared Memory, but it is not searchable yet (id={capture_id})."


@mcp.tool
def byb_memory_get_capture(capture_id: int) -> str:
    """Get a BYB Shared Memory capture by exact capture ID."""
    r = httpx.get(
        f"{MEMORY_API_URL}/v1/captures/{capture_id}",
        headers=_api_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    return str(r.json())


@mcp.tool
def byb_memory_patch_capture(
    capture_id: int,
    reason: str,
    raw_content: Optional[str] = None,
    memory_status: Optional[str] = None,
    superseded_by_capture_id: Optional[int] = None,
    source_message_ids_csv: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    expected_revision: Optional[int] = None,
) -> str:
    """Correct or supersede a BYB Shared Memory capture with revision history."""
    payload = {"revision_reason": reason}
    if raw_content is not None:
        payload["raw_content"] = raw_content
    if memory_status is not None:
        payload["memory_status"] = memory_status
    if superseded_by_capture_id is not None:
        payload["superseded_by_capture_id"] = superseded_by_capture_id
    source_ids = _source_message_ids(source_message_ids_csv)
    if source_ids:
        payload["source_message_ids"] = source_ids
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    r = httpx.patch(
        f"{MEMORY_API_URL}/v1/captures/{capture_id}",
        json=payload,
        headers=_api_headers(),
        timeout=60.0,
    )
    r.raise_for_status()
    out = r.json()
    return (
        f"Capture {capture_id} revised: status={out.get('memory_status')} "
        f"revision={out.get('revision')} revision_id={out.get('revision_id')}"
    )


@mcp.tool
def byb_memory_delete_capture(
    capture_id: int,
    reason: str,
    source_message_ids_csv: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    expected_revision: Optional[int] = None,
) -> str:
    """Soft-delete a BYB Shared Memory capture with a tombstone reason."""
    payload = {"reason": reason}
    source_ids = _source_message_ids(source_message_ids_csv)
    if source_ids:
        payload["source_message_ids"] = source_ids
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    r = httpx.request(
        "DELETE",
        f"{MEMORY_API_URL}/v1/captures/{capture_id}",
        json=payload,
        headers=_api_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    out = r.json()
    return (
        f"Capture {capture_id} soft-deleted: status={out.get('memory_status')} "
        f"revision={out.get('revision')} revision_id={out.get('revision_id')}"
    )


@mcp.tool
def byb_memory_get_tasks(
    project_slug: Optional[str] = None,
    status: Optional[str] = None,
    include_complete: bool = True,
    limit: int = 50,
) -> str:
    """List canonical BYB Shared Memory tasks."""
    params = {"include_complete": include_complete, "limit": limit}
    if project_slug:
        params["project_slug"] = project_slug
    if status:
        params["status"] = status
    r = httpx.get(f"{MEMORY_API_URL}/v1/tasks", params=params, headers=_api_headers(), timeout=30.0)
    r.raise_for_status()
    return str(r.json())


@mcp.tool
def byb_memory_get_projects() -> str:
    """List canonical BYB Shared Memory projects."""
    r = httpx.get(f"{MEMORY_API_URL}/v1/projects", headers=_api_headers(), timeout=30.0)
    r.raise_for_status()
    return str(r.json())


@mcp.tool
def byb_memory_patch_task(task_id: int, status: str) -> str:
    """Patch a canonical BYB Shared Memory task status."""
    return set_task_status(task_id=task_id, status=status)


@mcp.tool
def byb_memory_audit(limit: int = 20) -> str:
    """Return lightweight BYB Shared Memory health and recent capture audit data."""
    health = httpx.get(f"{MEMORY_API_URL}/health", headers=_api_headers(), timeout=30.0)
    health.raise_for_status()
    recent = httpx.get(
        f"{MEMORY_API_URL}/v1/search",
        params={"q": "", "limit": max(1, min(limit, 50))},
        headers=_api_headers(),
        timeout=30.0,
    )
    recent.raise_for_status()
    return str({"health": health.json(), "recent": recent.json()})


@mcp.tool
def upsert_task(
    description: str,
    status: str = "To Do",
    project_id: Optional[int] = None,
) -> str:
    """Create or update a task. Use for structured work items.
    """
    r = httpx.post(
        f"{MEMORY_API_URL}/v1/tasks",
        json={
            "description": description,
            "status": status,
            "project_id": project_id,
        },
        headers=_api_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    out = r.json()
    return f"Task created (id={out.get('id', '?')})"


@mcp.tool
def set_task_status(task_id: int, status: str) -> str:
    """Set a task's status. Allowed: To Do, In Progress, Complete, Deferred."""
    r = httpx.patch(
        f"{MEMORY_API_URL}/v1/tasks/{task_id}",
        json={"status": status},
        headers=_api_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    return f"Task {task_id} status set to {status}"


@mcp.tool
def link_entities(
    from_type: str,
    from_id: str,
    relation: str,
    to_type: str,
    to_id: str,
) -> str:
    """Link two entities (e.g., project-task, person-event).
    Placeholder: full implementation will use a relations/links API.
    """
    return "link_entities: Not yet implemented. Use project_id on tasks for project-task links."


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("memory://brief/daily")
def daily_brief() -> str:
    """Daily brief: priority tasks and relevant context."""
    try:
        r = httpx.get(
            f"{MEMORY_API_URL}/v1/search",
            params={"q": "", "limit": 20},
            headers=_api_headers(),
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return "\n".join(
                f"- {c.get('raw_content', '')}"
                for c in data[:15]
            )
        return "No recent captures."
    except Exception as e:
        return f"Error fetching daily brief: {e}"


@mcp.resource("memory://project/{slug}")
def project_dossier(slug: str) -> str:
    """Project dossier: project details and linked context."""
    try:
        r = httpx.get(
            f"{MEMORY_API_URL}/v1/projects/{slug}",
            headers=_api_headers(),
            timeout=30.0,
        )
        r.raise_for_status()
        return str(r.json())
    except Exception as e:
        return f"Error fetching project {slug}: {e}"


@mcp.resource("memory://task/{task_id}")
def task_detail(task_id: str) -> str:
    """Task detail by ID."""
    try:
        r = httpx.get(
            f"{MEMORY_API_URL}/v1/tasks/{task_id}",
            headers=_api_headers(),
            timeout=30.0,
        )
        r.raise_for_status()
        return str(r.json())
    except Exception as e:
        return f"Error fetching task {task_id}: {e}"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt
def daily_brief_prompt() -> str:
    """Generate the daily brief prompt for the LLM."""
    return """You are preparing a daily brief. Use the memory://brief/daily resource
to fetch recent captures and context. Summarize priorities and suggest
what to focus on today."""


@mcp.prompt
def weekly_review_prompt() -> str:
    """Generate the weekly review prompt."""
    return """You are preparing a weekly review. Use search_memory and
memory://brief/daily to gather context. Summarize accomplishments,
outstanding tasks, and suggest priorities for next week."""


@mcp.prompt
def capture_inbox_prompt() -> str:
    """Prompt for capturing notes into the inbox."""
    return """The user wants to capture a note. Use capture_note with their
raw content. Suggest a source (e.g., claude-ios, watson, slack) if known."""


if __name__ == "__main__":
    mcp.run(transport="stdio")
