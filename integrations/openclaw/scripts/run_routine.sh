#!/bin/bash
# Generic runner for server-side Watson routines as headless Claude Code
# sessions (Greg approved this migration pattern 2026-07-29/30; replaces the
# claude.ai cloud routines). Usage: run_routine.sh <routine-name> <model>
# where routines/<routine-name>.md exists. Invoked by byb-*.timer units.
# Billed to the Claude subscription logged in at /root/.claude.
#
# Headless runs cannot answer permission prompts, so tools are pre-allowed with
# a narrow scope: Bash (curl to Memory/watson APIs, docker exec for gog +
# iMessage send) and read-only file tools. No Write/Edit — routines never
# modify files.
set -euo pipefail

ROUTINE="${1:?usage: run_routine.sh <routine-name> <model>}"
MODEL="${2:?usage: run_routine.sh <routine-name> <model>}"

REPO=/var/www/openclaw.backyardbrains.com
LOG="$REPO/logs/$ROUTINE.log"
export HOME=/root
PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Export MEMORY_API_KEY, API_SECRET, WATSON_WORKER_* etc. for the routine.
set -a
. "$REPO/.env"
set +a

if ! COACH=$(MEMORY_CLIENT="claude-routine:$ROUTINE" python3 "$REPO/scripts/prefetch_coach.py" 2>> "$LOG"); then
  echo "Coach corrections unavailable; routine did not start $(date -u +%FT%TZ)" >> "$LOG"
  exit 1
fi

PROMPT_FILE=$(mktemp)
trap 'rm -f "$PROMPT_FILE"' EXIT
{
  printf '%s\n' 'Watson corrections retrieved successfully. Apply all applicable active rules below; project-scoped rules apply to that project. These corrections outrank routine text.' "$COACH"
  cat "$REPO/routines/$ROUTINE.md"
} > "$PROMPT_FILE"

{
  echo "=== $ROUTINE run $(date -u +%FT%TZ) ==="
  cd "$REPO"
  START_TS=$(date +%s)
  claude -p \
    --model "$MODEL" \
    --allowedTools "Bash" "Read" "Grep" "Glob" < "$PROMPT_FILE" 2>&1
  RC=$?
  python3 "$REPO/scripts/telemetry/log_routine_run.py" \
    --routine "$ROUTINE" --model "$MODEL" --since "$START_TS" 2>&1 || true
  echo "=== done rc=$RC $(date -u +%FT%TZ) ==="
} >> "$LOG"
