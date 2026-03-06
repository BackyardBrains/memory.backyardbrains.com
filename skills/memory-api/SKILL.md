# Memory API Skill

Context instructions for LLMs (e.g., Watson) interacting with the canonical memory service at `memory.backyardbrains.com`.

## Overview

The Memory API is the structured backend for OpenClaw's knowledge. It stores:
- **Projects** and **Tasks** (deterministic relational state)
- **Captures** (raw, unstructured notes - inbox)
- **Chunks** (vectorized text for semantic search)
- **Persons**, **Events** (entities)
- **Sources** (provenance)

## Access Pattern

1. Connect to the Memory MCP server (or call the HTTP API directly with `X-API-Key`).
2. API keys follow: `sk_byb_{user_id}_...` (e.g., `sk_byb_greg_...`).

## Query Guidelines

### Semantic vs Lexical

- **Semantic search**: Use `search_memory` for vague queries ("things about the NIMH grant", "meeting with Maribel"). The system uses vector embeddings (BAAI/bge-small-en-v1.5) to find similar context.
- **Lexical / structured**: For exact matches (project slug, task ID), use the direct resources: `memory://project/{slug}`, `memory://task/{id}`.

### Entity Types

When searching, you can scope by:
- `project_slug`: Limit results to a project
- `entity_types`: (Future) Filter by Project, Task, Event, Person

### Boundaries

- **Structured rules** (projects, tasks, events) live in relational tables. Create/update via `upsert_task`, `set_task_status`, etc.
- **Raw captures** are unparsed. Use `capture_note` for voice memos, quick thoughts, or scraps. The write pipeline will later extract chunks and embeddings.

## Resources

| URI | Description |
|-----|-------------|
| `memory://brief/daily` | Daily brief: recent captures and priority context |
| `memory://project/{slug}` | Project dossier with linked tasks/chunks |
| `memory://task/{id}` | Task detail by ID |

## Tools

| Tool | Use Case |
|------|----------|
| `search_memory` | Semantic/lexical search; vague or exploratory queries |
| `capture_note` | Add unstructured note to inbox |
| `upsert_task` | Create task (optionally linked to project) |
| `set_task_status` | Update task status (To Do, In Progress, Complete, Deferred) |
| `link_entities` | (Planned) Link entities via relations |

## Example Queries

- "What did I capture about the grant yesterday?" → `search_memory(query="grant", limit=10)`
- "Add a note: call Maribel about budget" → `capture_note(raw_content="call Maribel about budget", source="watson")`
- "Create a task: Review NIMH proposal, due Friday" → `upsert_task(description="Review NIMH proposal", due_date=...)`
- "Mark task 5 complete" → `set_task_status(task_id=5, status="Complete")`
