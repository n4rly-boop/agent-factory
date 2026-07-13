#!/usr/bin/env bash
# delegate-wall — PreToolUse hook on Write|Edit|NotebookEdit|Bash.
#
# A mini-orchestrator that is TOLD to delegate will still, under pressure, just
# edit the file itself: it is faster, it is right there, and the instruction is
# 40k tokens back. Telling it again does not fix that — an instruction competes
# for attention, a wall does not. This hook denies the write and names the way
# out, so delegation stops being a preference and becomes the only path forward.
#
# Exit 2 = block the tool call; stderr goes back to the model as the reason.
#
# WHAT THIS IS NOT: a sandbox. It is a routing enforcer against a cooperative
# agent that forgets, not a jail against one that is trying to get out. Bash is
# checked for the obvious write idioms (redirection, tee, sed -i, …) because that
# is the first thing an agent reaches for after a denied Write — but a determined
# agent can always find a way to put bytes on disk. If you need containment, use
# permissions, not this.
set -uo pipefail

[ "${AF_DELEGATE:-}" = "required" ] || exit 0

payload="$(cat 2>/dev/null)"
work="${AF_WORK:-}"
[ -z "$work" ] && exit 0

# Claude Code always reports an ABSOLUTE file_path, but AF_WORK comes from the
# blueprint and is typically written relative ("work: ./work"). Comparing the two
# as strings blocks the agent from writing its own report — and the block message
# then tells it to write its report. Every station loops. Resolve both ends.
case "$work" in
  /*) ;;
  *)  work="$(cd "${AF_CWD:-$PWD}" 2>/dev/null && cd "$work" 2>/dev/null && pwd)" || work="" ;;
esac
[ -z "$work" ] && exit 0     # can't resolve the safe zone → don't pretend to guard it

# Always parse the JSON properly, scoped to .tool_input — a regex over the raw
# payload would happily read a "file_path" key out of a Write's *content* and
# judge that instead of the real target.
_field() {   # $1 = json key
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r --arg k "$1" '.tool_input[$k] // empty' 2>/dev/null
  else
    K="$1" python3 -c '
import json,sys,os
try: print(json.load(sys.stdin).get("tool_input",{}).get(os.environ["K"],"") or "")
except Exception: pass' <<<"$payload" 2>/dev/null
  fi
}
_toolname() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null
  else
    python3 -c '
import json,sys
try: print(json.load(sys.stdin).get("tool_name","") or "")
except Exception: pass' <<<"$payload" 2>/dev/null
  fi
}
tool="$(_toolname)"

# Is a path inside the zone this agent is allowed to write?
_allowed() {
  local p="$1"
  case "$p" in
    "$work"|"$work"/*) return 0 ;;
  esac
  # Scratch is where a delegating agent stages a prompt or inspects output;
  # walling it off would block the very delegation we are demanding. But NOT the
  # factory's own state dir — the agent's settings file (which installs this very
  # hook) lives under $AF_ROOT, and $AF_ROOT defaults under /tmp. Letting it write
  # there would let it disarm the wall.
  local root="${AF_ROOT:-/tmp/agent-factory}"
  case "$p" in
    "$root"|"$root"/*) return 1 ;;
  esac
  case "$p" in
    /tmp/*|/private/tmp/*|/var/folders/*) return 0 ;;
  esac
  return 1
}

_deny() {
  cat >&2 <<EOF
BLOCKED by the factory's delegate-wall: '${AF_AGENT:-this agent}' is a mini-orchestrator and must not modify files directly.

  refused: $1

Do it one of these ways instead:
  1. delegate-to-local-model skill — the way to get a file WRITTEN. It runs an
     external model in its own process, so it is not behind this wall.
  2. mail a peer agent that owns this area (bash \$AF_MAIL send --to <agent> --kind task "…").
  3. if this is your own report or working note, write it under $work/ (always allowed).

NOT a way out: a Task subagent. It inherits this same wall and will be blocked
identically — verified. Use it to READ and analyse, never to write.

Then verify what came back. Do not retry this write.
EOF
  exit 2
}

case "$tool" in
  Bash)
    cmd="$(_field command)"
    [ -z "$cmd" ] && exit 0
    # Best-effort: only the idioms an agent actually reaches for after a denied
    # Write. Anything that looks like it puts bytes somewhere, with a path that is
    # not in the allowed zone, gets stopped.
    printf '%s' "$cmd" | grep -qE '(^|[^>])>>?[[:space:]]*[^&|[:space:]]|[[:space:]]tee[[:space:]]|sed[[:space:]]+-i|(^|[[:space:]])(cp|mv|install|patch|truncate)[[:space:]]' || exit 0
    # Pull out every path-looking token and judge them. If any lands outside the
    # allowed zone, block — we cannot know which one the redirect targets.
    for tok in $(printf '%s' "$cmd" | tr '\n' ' ' | tr -s ' ' '\n' | grep -E '^/|^\./|^[A-Za-z0-9_.-]+/' | sed 's/^[">]*//; s/["'"'"']//g'); do
      case "$tok" in
        /*) abs="$tok" ;;
        *)  abs="${AF_CWD:-$PWD}/$tok" ;;
      esac
      _allowed "$abs" || _deny "$abs  (via Bash: $cmd)"
    done
    exit 0
    ;;
  *)
    path="$(_field file_path)"
    [ -z "$path" ] && path="$(_field notebook_path)"
    [ -z "$path" ] && exit 0     # nothing to judge → don't guess
    _allowed "$path" || _deny "$path"
    exit 0
    ;;
esac
