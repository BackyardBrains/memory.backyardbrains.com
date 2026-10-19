#!/usr/bin/env bash
# SessionStart: current rules and evidence are separate from priority summaries.
set -uo pipefail
if [ -f /var/www/openclaw.backyardbrains.com/.env ]; then
  set -a
  . /var/www/openclaw.backyardbrains.com/.env
  set +a
fi
exec python3 /var/www/openclaw.backyardbrains.com/scripts/claude_memory_start.py
