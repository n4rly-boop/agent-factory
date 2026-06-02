#!/usr/bin/env bash
# Worker agent: reads tasks from the request FIFO, runs a real `claude` headless
# turn, keeps one persistent session (so it remembers across tasks), and writes
# the reply to the response FIFO. Everything also prints to this pane so you can
# watch the agent think.
set -uo pipefail

DIR="${AF_DIR:-/tmp/agent-factory}"
REQ="$DIR/a2b"          # orchestrator -> worker
RESP="$DIR/b2a"         # worker -> orchestrator
NAME="${1:-worker}"
# Extra claude flags. To let the worker actually edit files/run tools, set e.g.
#   AF_CLAUDE_FLAGS="--permission-mode acceptEdits"
FLAGS="${AF_CLAUDE_FLAGS:-}"
# Preassigned session id (from af.sh) so the log is filterable. First turn uses
# --session-id + a grep-able marker; later turns --resume it.
SID="${AF_SID:-}"
FIRST=1

b64enc() { base64 | tr -d '\n'; }   # single-line, portable (macOS wraps otherwise)

echo "[$NAME] online — waiting for tasks on $REQ"

# Open FIFO read+write so it never hits EOF when a writer closes.
exec 3<>"$REQ"
while IFS= read -r enc <&3; do
  [ -z "$enc" ] && continue
  msg=$(printf '%s' "$enc" | base64 -d)

  echo; echo "════════ TASK ════════"; echo "$msg"; echo "══════════════════════"

  if [ "$FIRST" = 1 ]; then
    if [ -n "$SID" ]; then
      out=$(claude -p "$msg" --session-id "$SID" $FLAGS --output-format json 2>/dev/null)
    else
      out=$(claude -p "$msg" $FLAGS --output-format json 2>/dev/null)
    fi
    FIRST=0
  else
    out=$(claude -p "$msg" --resume "$SID" $FLAGS --output-format json 2>/dev/null)
  fi

  # --output-format json may be a single object OR an array of stream events
  # depending on CLI version. These filters handle both.
  SID=$(printf '%s' "$out"  | jq -r '[.. | objects | .session_id?] | map(select(.)) | last // empty' 2>/dev/null)
  text=$(printf '%s' "$out" | jq -r '([.. | objects | select(.type=="result") | .result] | last) // .result // .error // "no output"' 2>/dev/null)
  [ -z "$text" ] && text="(empty reply — check worker pane / auth)"

  echo; echo "──────── REPLY ───────"; echo "$text"; echo "──────────────────────"
  printf '%s\n' "$(printf '%s' "$text" | b64enc)" > "$RESP"
done
