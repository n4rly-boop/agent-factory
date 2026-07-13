#!/usr/bin/env bash
# delegate-wall — PreToolUse hook on Write|Edit|NotebookEdit.
#
# A mini-orchestrator that is TOLD to delegate will still, under pressure, just
# edit the file itself: it is faster, it is right there, and the instruction is
# 40k tokens back. Telling it again does not fix that — an instruction competes
# for attention, a wall does not. This hook denies the write and names the way
# out, so delegation stops being a preference and becomes the only path forward.
#
# Exit 2 = block the tool call; stderr goes back to the model as the reason.
#
# Scope: only agents spawned with AF_DELEGATE=required. Writes inside the agent's
# own work directory are always allowed — that is where it files its report, which
# is its real job. Everything else must go through a subagent or the local model.
set -uo pipefail

[ "${AF_DELEGATE:-}" = "required" ] || exit 0

payload="$(cat 2>/dev/null)"
work="${AF_WORK:-}"
[ -z "$work" ] && exit 0

# file_path via jq when available, else a targeted grep — the hook must never
# crash the agent's turn just because jq is missing.
if command -v jq >/dev/null 2>&1; then
  path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' 2>/dev/null)"
else
  path="$(printf '%s' "$payload" | grep -oE '"(file_path|notebook_path)"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"//; s/"$//')"
fi
[ -z "$path" ] && exit 0          # no path to judge → don't guess, let it through

case "$path" in
  "$work"/*|"$work") exit 0 ;;    # its own report dir: allowed
esac
# Scratch/tmp writes are how a delegating agent stages a prompt or inspects
# output; walling those off would block the very delegation we are demanding.
case "$path" in
  /tmp/*|/private/tmp/*|/var/folders/*) exit 0 ;;
esac

cat >&2 <<EOF
BLOCKED by the factory's delegate-wall: '${AF_AGENT:-this agent}' is a mini-orchestrator and must not edit files directly.

  refused write: $path

Do it one of these ways instead:
  1. delegate-to-local-model skill  (preferred — free, keeps the work off your context)
  2. spawn a subagent with the Task tool and have IT make the change
  3. if this is your report or working note, write it under $work/ (that is always allowed)

Then verify what came back. Do not retry this write.
EOF
exit 2
