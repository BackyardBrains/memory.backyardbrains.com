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


mcp = FastMCP(
    name="Memory MCP",
    instructions="Memory service for OpenClaw. Use tools to search, capture notes, and manage tasks.",
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
    """Search the memory store. Use for vague or semantic-style queries.
    Returns matching captures and context.
    """
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
            f"- [{c.get('created_at', '')}] {c.get('raw_content', '')}"
            for c in data
        )
    return str(data)


@mcp.tool
def capture_note(raw_content: str, source: str = "mcp") -> str:
    """Add a raw note to the inbox (captures table). Use for unstructured input
    like voice memos, quick thoughts, or scraps. The note will be persisted
    and later processed for embeddings/assertions.
    """
    r = httpx.post(
        f"{MEMORY_API_URL}/v1/captures",
        json={"raw_content": raw_content, "source": source},
        headers=_api_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    out = r.json()
    return f"Captured (id={out.get('id', '?')})"


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
