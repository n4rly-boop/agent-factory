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
# Fall back to python3 whenever jq yields nothing — not merely when jq is ABSENT.
# A jq that exists but errors returned an empty path, and an empty path meant
# "nothing to judge → allow": the wall failed open on a broken jq.
_field() {   # $1 = json key
  local v=""
  command -v jq >/dev/null 2>&1 && \
    v="$(printf '%s' "$payload" | jq -r --arg k "$1" '.tool_input[$k] // empty' 2>/dev/null)"
  [ -n "$v" ] && { printf '%s' "$v"; return; }
  K="$1" python3 -c '
import json,sys,os
try: print(json.load(sys.stdin).get("tool_input",{}).get(os.environ["K"],"") or "")
except Exception: pass' <<<"$payload" 2>/dev/null
}
_toolname() {
  local v=""
  command -v jq >/dev/null 2>&1 && \
    v="$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null)"
  [ -n "$v" ] && { printf '%s' "$v"; return; }
  python3 -c '
import json,sys
try: print(json.load(sys.stdin).get("tool_name","") or "")
except Exception: pass' <<<"$payload" 2>/dev/null
}
tool="$(_toolname)"

# On macOS /tmp and /var are symlinks into /private, so "/tmp/agent-factory/x" and
# "/private/tmp/agent-factory/x" are THE SAME FILE. Comparing the raw strings let
# an agent step around the $AF_ROOT carve-out with one extra prefix and overwrite
# the settings file that installs this very hook. Normalise both sides.
_norm() {
  local p="$1"
  case "$p" in /private/*) p="${p#/private}" ;; esac
  printf '%s' "$p"
}

# Is a path inside the zone this agent is allowed to write?
_allowed() {
  local p w root
  p="$(_norm "$1")"; w="$(_norm "$work")"; root="$(_norm "${AF_ROOT:-/tmp/agent-factory}")"
  case "$p" in
    "$w"|"$w"/*) return 0 ;;
  esac
  # The factory's own state dir is NOT scratch: the agent's --settings file lives
  # under $AF_ROOT, and $AF_ROOT defaults under /tmp. Writing there would let it
  # disarm the wall. Checked before the /tmp allowlist, or the allowlist would
  # hand it back.
  case "$p" in
    "$root"|"$root"/*) return 1 ;;
  esac
  # Scratch is where a delegating agent stages a prompt or inspects output;
  # walling it off would block the very delegation we are demanding.
  case "$p" in
    /tmp/*|/var/folders/*) return 0 ;;
  esac
  return 1
}
_abs() {   # resolve a possibly-relative token against the agent's cwd
  case "$1" in
    /*) printf '%s' "$1" ;;
    *)  printf '%s/%s' "${AF_CWD:-$PWD}" "$1" ;;
  esac
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

    # Judge the WRITE TARGET, never "any path that appears in the command".
    #
    # The first version scanned the whole command for path-looking tokens and
    # blocked if any fell outside the zone. That is wrong in both directions, and
    # the false positives are the worse half: `grep -rn foo /abs/path 2>/dev/null`
    # was blocked (the `2>` reads as a write, the `/abs/path` as its target), so
    # the agent was told to delegate a *grep* — and loops. Meanwhile
    # `echo pwned > ai.sh` sailed through, because a bare filename has no slash
    # and produced no token at all: the most natural bypass of them all.
    #
    # So: extract the operand of each write construct, and judge only that.
    #
    # Tokenised with shlex, not a regex, because quoting is the whole problem: a
    # `>` INSIDE a quoted string is not a redirection. `awk '$1 > 2' file` and
    # `grep "a -> b" file` are read-only, and a regex scanner blocked both — which
    # left the agent being told to delegate a grep, and looping.
    targets="$(AF_CMD="$cmd" python3 -c '
import os, shlex, sys

cmd = os.environ["AF_CMD"]
lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
lex.whitespace_split = True
try:
    toks = list(lex)
except ValueError:            # unbalanced quotes — cannot reason, say nothing
    sys.exit(0)

out, i = [], 0
while i < len(toks):
    t = toks[i]
    # shlex with punctuation_chars groups redirection operators into their own
    # token: ">", ">>", ">|", "2>". The operand is whatever follows.
    if t.rstrip("0123456789").startswith(">") or t.endswith(">") or t in (">", ">>", ">|"):
        if set(t) <= set("0123456789>|&") and i + 1 < len(toks):
            out.append(toks[i + 1]); i += 2; continue
    if t.startswith("of="):
        out.append(t[3:])
    elif t == "tee":
        for n in toks[i + 1:]:
            if not n.startswith("-"):
                out.append(n); break
    elif t in ("-o", "-so", "--output"):          # curl and friends
        if i + 1 < len(toks): out.append(toks[i + 1])
    elif t in ("sed", "perl") and any(a.startswith("-") and "i" in a for a in toks[i + 1:i + 4]):
        if toks[-1:]: out.append(toks[-1])
    elif t in ("cp", "mv", "install", "patch", "truncate"):
        if toks[-1:]: out.append(toks[-1])
    i += 1

for p in out:
    if p and not p.startswith("/dev/") and not p.isdigit() and p not in ("-", "&"):
        print(p)
' 2>/dev/null)"

    # Here-string, not a pipe: a pipeline runs the loop in a SUBSHELL, where
    # _deny's `exit 2` would kill only that subshell and the hook would go on to
    # exit 0 — allowing the very write it just decided to block.
    while IFS= read -r tok; do
      [ -z "$tok" ] && continue
      abs="$(_abs "$tok")"
      _allowed "$abs" || _deny "$abs  (via Bash: $cmd)"
    done <<< "$targets"
    # Known gaps, accepted: an interpreter writing from inside its own source
    # (`python3 -c 'open(p,"w")'`), `git checkout`, and anything that computes its
    # target at runtime. This is a routing enforcer, not a sandbox — see the header.
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
