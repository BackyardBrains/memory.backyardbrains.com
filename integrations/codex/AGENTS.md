# BYB Server — Watson Memory Protocol (every Codex session)

Watson memory (memory.backyardbrains.com) is the shared source of truth for all
BYB agents — Claude, Codex, and the OpenClaw crew all read and write it. Greg's
standing complaint about GPT sessions is that they skip it. Do not be that
session.

## Before starting any BYB task

Load the complete active rules and relevant evidence with `memory context --q "<task question>" --client codex` (add `--project <slug>` when known). Apply its rules and cite source IDs. If context retrieval fails, report that failure; do not assume the corrections were checked.

Search memory for additional relevant context, and always check for coach corrections —
they outrank local instructions and this file:

    curl -s -H "X-API-Key: $MEMORY_API_KEY" \
      "https://memory.backyardbrains.com/v1/search?q=<urlencoded>&limit=5"
    # Additional coach search: q=coach%3Awatson (not a substitute for the complete rules packet)

Credentials: `MEMORY_API_KEY` and `API_SECRET` live in
`/var/www/openclaw.backyardbrains.com/.env` — `set -a; . <that file>; set +a`
if they are not already exported.

## Writing to memory

- Capture durable facts/decisions: `POST /v1/captures` with JSON
  `{"raw_content": "...", "source": "server-claude"}`. The field is
  `raw_content`, NOT `content` (`content` returns 422). Keep the source string
  `server-claude` — it names the server worker role, not the model.
- Dedup-search before every capture; never write a duplicate.
- Verify every write by the id in the POST response (there is no status
  readback endpoint).
- `PATCH /v1/tasks/{id}` silently drops `description` — put evidence in
  captures, never in task descriptions.

## Watson service

    curl -s "https://watson.backyardbrains.com/<path>?secret=$API_SECRET"

Useful paths: `/summary`, `/current-priorities`, worker queue under `/worker/*`.

## House rules

- Never fabricate activity; memory is an index, not a warehouse.
- Signal over noise: if it can wait for a daily cycle, let it wait.
- Closed topics stay closed: if a coach note says a topic is dropped (e.g. the
  NINDS discrepancy), do not resurface it.

During the memory pilot, use `memory feedback --context <context_id> --outcome <used|missed|stale|irrelevant|user_repeated|abstained> --capture-id <id>` only after actual use or an observed problem. Loading context is not evidence that it was followed. Feedback is private telemetry, not a durable capture.
