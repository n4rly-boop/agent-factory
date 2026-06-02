#!/usr/bin/env bash
# Fire one task at the worker from ANY shell (third terminal, cron, another
# agent) and print its reply. Lets you script autonomous loops without the REPL.
#   bash send.sh "summarize the repo README"
set -uo pipefail

DIR="${AF_DIR:-/tmp/agent-factory}"
REQ="$DIR/a2b"; RESP="$DIR/b2a"
msg="$*"
[ -z "$msg" ] && { echo "usage: send.sh <task text>"; exit 1; }

exec 4<>"$RESP"
printf '%s\n' "$(printf '%s' "$msg" | base64 | tr -d '\n')" > "$REQ"
IFS= read -r enc <&4
printf '%s' "$enc" | base64 -d
echo
