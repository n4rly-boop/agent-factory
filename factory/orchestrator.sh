#!/usr/bin/env bash
# Orchestrator: a tiny REPL. Type a task, it goes over the request FIFO to the
# worker, then it blocks until the worker's reply comes back on the response
# FIFO and prints it.
set -uo pipefail

DIR="${AF_DIR:-/tmp/agent-factory}"
REQ="$DIR/a2b"          # orchestrator -> worker
RESP="$DIR/b2a"         # worker -> orchestrator
NAME="${1:-orchestrator}"

b64enc() { base64 | tr -d '\n'; }

# Keep response FIFO open the whole time.
exec 4<>"$RESP"

echo "[$NAME] ready. Type a task + Enter to dispatch. Ctrl-C to quit."
while true; do
  printf '\n%s> ' "$NAME"
  IFS= read -r line || break
  [ -z "$line" ] && continue

  printf '%s\n' "$(printf '%s' "$line" | b64enc)" > "$REQ"
  echo "  … dispatched, waiting for worker …"

  IFS= read -r enc <&4
  echo "  ◀ worker replied:"
  printf '%s' "$enc" | base64 -d
  echo
done
