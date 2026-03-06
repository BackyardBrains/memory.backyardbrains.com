# Memory & Agent Architecture

Answers to common configuration questions and a guide for production setup.

---

## 1. Service Naming

The systemd service is `memory.backyardbrains.com` (not `memory-api`), so it matches the URL:

```bash
systemctl status memory.backyardbrains.com
systemctl restart memory.backyardbrains.com
```

---

## 2. Production API Key

### How keys work

- Format: `sk_byb_{user_id}_{suffix}` (e.g. `sk_byb_greg_local`, `sk_byb_greg_prod_abc123`)
- `user_id` is the first segment after `sk_byb_` (e.g. `greg`)
- All data is scoped by `user_id`
- The suffix can be anything; it does not affect data isolation

### Creating a production key

1. Generate a random suffix (e.g. 24+ chars):
   ```bash
   openssl rand -hex 16
   ```
2. Build the key: `sk_byb_greg_<your_suffix>` (e.g. `sk_byb_greg_a1b2c3d4e5f6...`)
3. Add it to allowed keys (optional but recommended):
   ```bash
   export MEMORY_API_KEYS="sk_byb_greg_local,sk_byb_greg_a1b2c3d4e5f6..."
   ```
   Put this in `/etc/systemd/system/memory.backyardbrains.com.service` under `Environment=` if using allowlist.

### Memories from the sample key

- All captures created with `sk_byb_greg_local` are stored with `user_id=greg`
- Any key whose `user_id` is `greg` will see the same data
- Switching to `sk_byb_greg_prod_xyz` does not migrate or duplicate data; you see the same memories because `user_id` is still `greg`

**Summary:** Memories are keyed by `user_id`, not by the full API key. Changing from sample to production key keeps access to the same data as long as `user_id` is unchanged.

---

## 3. Watson, Ontology, and Storage

### What Watson actually uses

| Source | Used by | How |
|--------|---------|-----|
| **Watson API** (watson.backyardbrains.com) | External clients, MCP | REST + MCP at `/mcp`; reads from **memory-api** when `USE_MEMORY_API=true` |
| **memory-api** (memory.backyardbrains.com) | Watson API, watson-memory skill | PostgreSQL: projects, tasks, events, captures, chunks (vector search) |
| **Watson agent** (byb-watson) | Cron briefings, spawns | Uses `read`/`write` on **files** (`ontology/entities/*.jsonld`, `memory/*.md`) — not memory-api |
| **watson-memory skill** | Spike, Watson (when loaded) | Uses curl to memory-api for search + captures |

### Ontology in chats

- Ontology data is not stored inside chat/session JSON
- When tools like `get_summary` or `get_tasks` run, their JSON results go into the message history
- So the “ontology in chats” is just tool-output context; it is not the canonical store

### Canonical vs workspace

| Store | Canonical? | Contents |
|-------|------------|----------|
| **memory-api (Postgres)** | ✅ Yes | Projects, tasks, events, persons, captures, vector chunks |
| **ontology/entities/*.jsonld** | ❌ No (legacy) | Old file-based ontology; Watson agent still writes here |
| **ontology/entities/links.jsonld** | Still used | Links; memory-api has no links table yet |
| **workspace memory/** | Per-agent | `memory/YYYY-MM-DD.md`, `MEMORY.md` — agent logs and notes |

**Current mismatch:** Watson agent edits JSON-LD files; Watson API reads from memory-api. Those can diverge until Watson agent uses Watson MCP or another path into memory-api.

---

## 4. Spike and Memory Flow

### Current setup

- **Spike** (main): Coordinator; can spawn Watson, Patsy, Addy
- **Watson MCP** is not in `mcporter.json` — Spike does not have Watson MCP
- Spike has the **watson-memory** skill, which uses curl to memory-api for search and captures (not tasks, projects, or status updates)

### How work can reach memory today

1. **Spike → Watson spawn**  
   Spike spawns Watson with a task like “Log this to memory.”  
   Watson agent writes to JSON-LD and `memory/*.md`, not memory-api.

2. **Spike → watson-memory skill**  
   Spike uses curl `POST /v1/captures` to add inbox notes.  
   This goes to memory-api.

3. **External → Watson API**  
   POST `/inbox` or `post_inbox` MCP → memory-api captures.

4. **Patsy / Addy**  
   No direct memory-api integration; their outputs live in workspace files only.

### Gaps

- Spike cannot update task status (no `set_task_status`)
- Spike cannot read projects/tasks from memory-api (only search/captures via skill)
- Patsy and Addy have no standard way to log to memory
- Watson agent edits files; Watson API reads memory-api → risk of divergence

---

## 5. Recommended Configuration

### A. Add Watson MCP to mcporter

In `mcporter.json`, add:

```json
"byb-memory": {
  "url": "https://watson.backyardbrains.com/mcp",
  "transport": "http"
}
```

Watson MCP uses `privateKey` per tool call (from `API_SECRET` or `BACKYARDBRAINS_API_KEY`). The MCP client or agent context must supply this when calling tools.

### B. Give Spike Watson MCP (or memory MCP)

- Option 1: Add Watson MCP to mcporter so Spike gets `get_summary`, `get_tasks`, `set_task_status`, `post_inbox`.
- Option 2: Use memory MCP at `https://memory.backyardbrains.com/mcp` if it exposes equivalent tools.

Then Spike can read and update ontology directly.

### C. Spike completion → memory protocol

When Spike completes work that should be remembered:

1. Use `post_inbox` with project tag, or  
2. Spawn Watson with “Log to ontology: [summary]”, or  
3. Use `set_task_status` when a task is done (if Spike has Watson MCP).

Document this in Spike’s SOUL.md or a dedicated skill.

### D. Patsy and Addy → memory

- Add a simple skill: “When you complete significant work, capture a note via `post_inbox` (or curl POST /v1/captures) with `source: patsy` or `source: addy` and project tag.”
- Or grant them Watson MCP and instruct: “Use `post_inbox` for summaries of completed work.”

### E. Make Watson agent use memory-api

- Give Watson agent Watson MCP so it uses `get_summary`, `get_tasks`, `set_task_status` instead of file reads/writes.
- Stop editing `ontology/entities/*.jsonld` for tasks/events/projects.
- Keep `links.jsonld` until memory-api has a links table.

### F. Single source of truth

- **Memory-api = canonical** for projects, tasks, events, captures.
- Deprecate editing of `tasks.jsonld`, `projects.jsonld`, `events.jsonld`.
- `links.jsonld` stays file-based for now.

---

## 6. Data Flow (Target State)

```
External (ChatGPT, Claude, curl)
        │
        ▼
Watson API /inbox ──────────► memory-api /v1/captures
        │
        └── Watson reconciles (via MCP or cron)

Spike (with Watson MCP)
        │
        ├── get_summary, get_tasks ──► memory-api
        ├── set_task_status ─────────► memory-api
        └── post_inbox ──────────────► memory-api

Watson agent (with Watson MCP)
        │
        ├── get_summary, get_tasks ──► memory-api (via Watson API)
        ├── set_task_status ─────────► memory-api
        └── post_inbox ──────────────► memory-api

Patsy / Addy (with capture skill or MCP)
        │
        └── post_inbox / POST captures ─► memory-api
```

---

## 7. Reference Paths

| Item | Path |
|------|------|
| Agent config | `openclaw.json` |
| MCP servers | `mcporter.json` |
| Watson API | `services/watson.backyardbrains.com/main.py` |
| Memory API | `memory.backyardbrains.com` (service + codebase) |
| Watson skill (curl) | `skills/watson/SKILL.md` |
| Watson AGENTS | `workspaces/watson/AGENTS.md` |
