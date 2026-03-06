# memory.backyardbrains.com

A knowledge base and semantic search backend for AI assistants. Stores projects, tasks, and events in PostgreSQL with [pgvector](https://github.com/pgvector/pgvector), so LLMs can search and retrieve relevant context when helping you plan or prioritize work.

This service powers Watson and other MCP servers that need access to structured, searchable task and project data.

## Quick Start

```bash
# 1. Start Postgres + pgvector
docker compose up -d

# 2. Run migrations
source venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. alembic upgrade head

# 3. Start the API (or use systemd: systemctl start memory-api)
PYTHONPATH=. uvicorn services.memory_api.main:app --host 0.0.0.0 --port 8002

# 4. Migrate Watson's ontology (one-time)
MEMORY_API_URL=https://memory.backyardbrains.com MEMORY_API_KEY=sk_byb_greg_local \
  PYTHONPATH=. python scripts/migrate_ontology_from_jsonld.py \
    /var/www/openclaw.backyardbrains.com/workspaces/main/memory/ontology/entities \
    --clear
```

## Watson Integration

Watson uses memory-api when `USE_MEMORY_API=true` (default). Set in Watson's env:

- `USE_MEMORY_API=true`
- `MEMORY_API_URL=https://memory.backyardbrains.com`
- `MEMORY_API_KEY=sk_byb_greg_local` (or your production key)

Links still come from JSON-LD (`ontology/entities/links.jsonld`) until a links API is added.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://byb_memory:...@localhost:5433/openbrain` | Postgres connection |
| `MEMORY_API_URL` | `https://memory.backyardbrains.com` | API base URL (for Watson/MCP) |
| `MEMORY_API_KEY` | `sk_byb_greg_local` | API key for Watson→API calls |
| `MEMORY_API_KEYS` | (empty) | Comma-separated allowed keys; if set, enforces allowlist |

## API Key Format

Keys must match `sk_byb_{user_id}_...`. The `user_id` segment is used for data isolation (e.g., `sk_byb_greg_...` → `user_id=greg`).

**Production key:** Generate with `openssl rand -hex 16`, then use `sk_byb_greg_<suffix>`. Memories from `sk_byb_greg_local` stay accessible — data is keyed by `user_id`, not the full key. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details.

## Structure

```
memory.backyardbrains.com/
├── db/                 # Schema, engine, migrations
├── packages/
│   ├── ontology-core/  # Domain models, JSON-LD
│   └── memory-retrieval/  # Vector/lexical search
├── scripts/
│   └── migrate_ontology_from_jsonld.py
├── services/
│   ├── memory_api/     # FastAPI HTTP API
│   └── memory_mcp/     # FastMCP adapter
├── skills/
│   └── memory-api/     # SKILL.md for LLMs
└── alembic/            # Migrations
```
