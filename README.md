# 🧠 memory.backyardbrains.com (OpenBrain Engine)

This is the canonical, neuro-symbolic memory engine for the Backyard Brains autonomous fleet.

Instead of relying on fragile flat-files or isolated agent logs, this service provides a unified PostgreSQL + pgvector backend. It acts as the single source of truth for all external LLMs (Claude/Cursor) and internal agents (Spike, Watson, Patsy, Addy).

## 🧬 The Architecture: A Two-Hemisphere Brain

This system is designed around a neuro-symbolic architecture, dividing data into strict relational state and fuzzy semantic context.

### 📐 The Left Brain (Deterministic State)

The Left Brain handles rigid, relational data. It doesn't guess; it executes state changes.

**Tables:** Projects, Tasks, Events, Links, Persons.

**Function:** Tracks exact booleans, foreign keys, and due dates. When Spike marks a task as "Complete" or deferred, the Left Brain updates the exact row.

### 🎨 The Right Brain (Semantic Memory)

The Right Brain handles context, rationale, and fuzzy retrieval using HuggingFace embeddings (BAAI/bge-small-en-v1.5).

**Tables:** Captures, Chunks, Embeddings.

**Function:** When Patsy logs an accounting reconciliation, or you drop a messy note from your phone, the Right Brain vectorizes it. Agents can perform semantic searches later (e.g., "What did we decide about the NSTA curriculum?") and retrieve the exact thought.

### ⚡️ The Corpus Callosum (API & MCP)

Agents never touch the database directly. They interact through two unified interfaces:

- **The REST API (The Warehouse):** Lightning-fast endpoints (`/v1/tasks`, `/v1/captures`) used by internal scripts and applications.
- **The MCP Server (The Front Desk):** The [watson.backyardbrains.com](https://github.com/BackyardBrains/watson.backyardbrains.com) translation layer that packages complex SQL queries into simple tools (`get_summary`, `post_inbox`) for LLMs.

## 🔐 Security & Data Scoping

Authentication is handled via Bearer Headers (`Authorization: Bearer sk_byb_{user_id}_{suffix}`). This creates a two-step security lock:

1. **The VIP List (Authentication):** The server checks the exact, full string against the `MEMORY_API_KEYS` environment variable allowlist. If the key isn't listed, the request is bounced with a 401.
2. **The Filing Cabinet (Data Isolation):** If authorized, the server extracts the `{user_id}` (e.g., `greg` from `sk_byb_greg_tKZ2X67`). The database will only return rows belonging to that specific user. You can rotate the suffix anytime without losing your memories.

## 🚀 Quick Start Guide

### 1. Start the Right Brain (Postgres + pgvector)

```bash
docker compose up -d
```

### 2. Hydrate the Left Brain (Run Migrations)

```bash
source venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. alembic upgrade head
```

### 3. Wake up the Engine (Start the API)

```bash
# Run locally for dev:
PYTHONPATH=. uvicorn services.memory_api.main:app --host 0.0.0.0 --port 8002

# Or restart the production daemon:
sudo systemctl restart memory.backyardbrains.com
```

### 4. Legacy Data Migration (One-Time Execution)

If you are migrating from a legacy OpenClaw flat-file system, run this to port the old JSON-LD ontology into the Postgres brain.

```bash
MEMORY_API_URL=https://memory.backyardbrains.com MEMORY_API_KEY=sk_byb_greg_local \
  PYTHONPATH=. python scripts/migrate_ontology_from_jsonld.py \
    /path/to/openclaw/workspaces/main/memory/ontology/entities \
    --clear
```

## 📂 Neurological Map (Repository Structure)

```
memory.backyardbrains.com/
├── db/                 # The Schema (Left/Right Brain table definitions)
├── packages/
│   ├── ontology-core/  # Domain models and entity definitions
│   └── memory_retrieval/  # pgvector lexical/semantic search logic
├── scripts/            # Legacy migration tools
├── services/
│   ├── memory_api/     # The REST API (The Warehouse)
│   └── memory_mcp/     # The FastMCP adapter (The Front Desk)
├── skills/
│   └── memory-api/     # SKILL.md context instructions for LLMs
└── alembic/            # Database migration history
```
