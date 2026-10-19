#!/bin/bash
# Codex (OpenAI / ChatGPT subscription) runner for server-side Watson routines —
# headless twin of run_routine.sh (Greg approved offloading routines to OpenAI
# 2026-08-05 to relieve the Claude weekly limit).
# Usage: run_routine_codex.sh <routine-name> [model] [reasoning-effort]
# where routines/<routine-name>.md exists. Billed to the ChatGPT subscription
# logged in at /root/.codex.
#
# Sandbox/approval posture comes from /root/.codex/config.toml (sandbox_mode
# danger-full-access, approval_policy never) — required because routines hit
# the Memory/Watson APIs and use docker exec. If that config changes, headless
# runs will stall on approvals.
#
# GPT runs have historically skipped the "check Watson memory first" steps that
# Claude follows reliably, so the runner enforces the protocol structurally:
#   - Step 0 (coach:watson) is pre-fetched via prefetch_coach.py and injected
#   - a preamble makes memory dedup/readback non-negotiable
#   - log_routine_run_codex.py counts actual Memory API calls after the run and
#     records search attempts; it does not equate a search with compliance
set -uo pipefail

ROUTINE="${1:?usage: run_routine_codex.sh <routine-name> [model] [effort]}"
MODEL="${2:-gpt-5.5}"
EFFORT="${3:-medium}"

REPO=/var/www/openclaw.backyardbrains.com
LOG="$REPO/logs/$ROUTINE.log"
export HOME=/root
PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Export MEMORY_API_KEY, API_SECRET, WATSON_WORKER_* etc. for the routine.
set -a
. "$REPO/.env"
set +a

if ! COACH=$(MEMORY_CLIENT="codex-routine:$ROUTINE" python3 "$REPO/scripts/prefetch_coach.py" 2>> "$LOG"); then
  echo "Coach corrections unavailable; routine did not start $(date -u +%FT%TZ)" >> "$LOG"
  exit 1
fi

PROMPT_FILE=$(mktemp)
trap 'rm -f "$PROMPT_FILE"' EXIT
{
  cat <<'EOF'
SYSTEM CONTEXT — READ FIRST. You are running headless via codex exec on the BYB
server, filling the same server worker role Claude fills on other hours. Keep
every agent id and source string exactly as the routine writes them (e.g.
"server-claude") — they identify the role, not the model.

WATSON MEMORY PROTOCOL (non-negotiable; GPT runs skipping these is the #1
failure mode Greg watches for):
1. Watson memory is the shared source of truth. Never act on assumption when a
   two-second memory search would answer it.
2. Step 0 succeeded: the complete active rules packet is injected below. Apply
   every applicable correction in them; project-scoped rules apply to that project.
   These corrections outrank the routine text.
3. Before EVERY capture: run the dedup search the routine specifies. A capture
   without a preceding search is a protocol violation.
4. Verify every write by the id in the POST response.
5. Memory search attempts are counted separately from correct use. Before the
   task, use memory context with the task question and project. Report feedback
   only after actual use or an observed miss; fetching alone is not compliance.
EOF
  echo ""
  echo "## Watson context packet (Step 0 output — apply its rules)"
  printf '%s\n' "$COACH"
  echo ""
  echo "--- ROUTINE:$ROUTINE ---"
  cat "$REPO/routines/$ROUTINE.md"
} > "$PROMPT_FILE"

{
  echo "=== $ROUTINE codex run $(date -u +%FT%TZ) model=$MODEL effort=$EFFORT ==="
  cd "$REPO"
  START_TS=$(date +%s)
  codex exec \
    -C "$REPO" \
    -m "$MODEL" \
    -c model_reasoning_effort="\"$EFFORT\"" \
    - < "$PROMPT_FILE"
  RC=$?
  python3 "$REPO/scripts/telemetry/log_routine_run_codex.py" \
    --routine "$ROUTINE" --model "$MODEL" --since "$START_TS" || true
  echo "=== done rc=$RC $(date -u +%FT%TZ) ==="
} >> "$LOG" 2>&1

exit "$RC"
