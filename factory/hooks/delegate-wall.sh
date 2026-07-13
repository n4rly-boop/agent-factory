#!/usr/bin/env bash
# delegate-wall — PreToolUse hook on Write|Edit|NotebookEdit|Bash.
#
# A mini-orchestrator that is TOLD to delegate will still, under pressure, just
# edit the file itself: it is faster, it is right there, and the instruction is
# 40k tokens back. Telling it again does not fix that — an instruction competes for
# attention, a hook does not.
#
# TWO LEVELS, because the first version had one and it was the wrong one.
#
#   AF_DELEGATE=advised   (the default for a line's stations)
#       Never blocks. A SMALL write goes through in silence. A BULK write outside
#       work/ goes through too, with a note injected into the model's context:
#       this is bulk, delegate-to-local-model is free and keeps it off your context.
#       Nudge, not gate.
#
#   AF_DELEGATE=required  (opt in, per station)
#       The hard wall: any write outside work/ is denied, whatever its size.
#       For a station that must not touch shared code at all.
#
# WHY THE DEFAULT MOVED. Observed on a live `required` agent asked to write one line
# to one file: the wall blocked it, the agent dutifully re-routed to
# delegate-to-local-model — and spun up an external LLM to write a single line. The
# discipline we bought was real and the price was absurd. Delegation pays for itself
# on bulk (spec-code, boilerplate, mass conversion, big first drafts); on a
# three-line fix it is pure overhead. So size, not zone, is what the default judges.
#
# WHAT THIS IS NOT, AT EITHER LEVEL: a sandbox. It routes; it does not contain. And
# note it does not even bound WHERE bytes land: the sanctioned escape,
# delegate-to-local-model, runs in its own process and writes wherever it is told,
# work/ or not. What the wall reliably buys is that the agent dispatches and verifies
# instead of doing bulk work itself. If you need containment, use permissions.
#
# Exit 2 = block (required only). Exit 0 + JSON = allow, with context for the model.
set -uo pipefail

LEVEL="${AF_DELEGATE:-}"
case "$LEVEL" in
  required|advised) ;;
  *) exit 0 ;;                       # not a delegating agent → say nothing
esac

# A write this big or bigger is "bulk" — worth delegating. Below it, the agent just
# does the edit. Override per line with AF_BULK_LINES (or `bulk_lines:` in the blueprint).
#
# Sanitised, because a junk value did not fail safe: `[ abc -lt 40 ]` errors, the &&
# never fires, and control falls THROUGH to the advisory — so every two-line edit got
# nagged as bulk. A bad threshold turned the nudge into noise, which is how a nudge
# gets ignored.
BULK="${AF_BULK_LINES:-40}"
case "$BULK" in ''|*[!0-9]*) BULK=40 ;; esac

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

# How many lines is this write? The size of what LANDS, not of the tool call: an Edit
# is judged on its new_string, not on the file it edits. This is the number the
# `advised` level acts on, and getting it from the wrong field is how you end up
# nagging about a two-line fix to a big file.
_writelines() {
  AF_PAYLOAD="$payload" python3 -c '
import json, os, sys
try: t = (json.loads(os.environ["AF_PAYLOAD"]).get("tool_input") or {})
except Exception: print(0); sys.exit(0)

# MultiEdit keeps its payload in edits[].new_string — neither `content` nor
# `new_string` exists on it, so it measured ZERO and a 50-edit, 400-line rewrite got
# no advice at all: the easiest way in the toolbox to do exactly the bulk work this
# hook exists to redirect. Sum the edits.
edits = t.get("edits")
if isinstance(edits, list) and edits:
    n = 0
    for e in edits:
        if isinstance(e, dict):
            n += len(str(e.get("new_string") or "").splitlines())
    print(n); sys.exit(0)

body = t.get("content") or t.get("new_string") or t.get("new_source") or t.get("command") or ""
if isinstance(body, list): body = "\n".join(map(str, body))
print(len(str(body).splitlines()))
' 2>/dev/null || echo 0
}

# ALLOW the tool, and put a note in the MODEL's context.
#
# It has to be this JSON shape. A PreToolUse hook that exits 0 sends its stdout to the
# debug log and NOWHERE ELSE — the model never sees it — and `permissionDecisionReason`
# is likewise only logged. `additionalContext` is the one field that reaches the model
# while still letting the call through. ("ask" would reach the human instead, and would
# still prompt even under --dangerously-skip-permissions: an unattended agent would
# hang on it forever.)
_advise() {
  AF_MSG="$1" python3 -c '
import json, os
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "delegate-wall: advisory only",
    "additionalContext": os.environ["AF_MSG"],
}}))'
  exit 0
}

# The one decision, made once, per write target.
#   $1 = the resolved path to judge.  $2 = how to describe it to the model (optional).
# Two arguments, not one, because the Bash path wants to show the command too — and
# folding that into $1 would have handed _allowed a decorated string to compare
# against the work dir, which never matches: every Bash write would look forbidden.
#
# IT RETURNS. It must never `exit 0` on an allowed target: one Bash command can carry
# several write targets, and the caller loops over them. An early exit meant the FIRST
# allowed target ended the hook and every target after it went unjudged —
# `echo ok > /tmp/x; echo pwned > ai.sh` sailed through a `required` wall. One throwaway
# write to /tmp, and the wall was disarmed for the rest of the command. (The here-string
# below exists to stop exactly this class of escape at the subshell level; the refactor
# reopened it one level up.)
ADVISE_TARGET=""; ADVISE_N=0
_judge() {
  local path="$1" target="${2:-$1}" n
  _allowed "$path" && return 0            # inside work/ or scratch → never our business
  [ "$LEVEL" = "required" ] && _deny "$target"    # _deny exits 2 — the only exit from here
  # advised: only bulk is worth a word. A small edit is the agent doing its job.
  n="$(_writelines)"
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  [ "$n" -lt "$BULK" ] && return 0
  # Remember the first bulk target; the caller advises ONCE, after every target is judged.
  [ -z "$ADVISE_TARGET" ] && { ADVISE_TARGET="$target"; ADVISE_N="$n"; }
  return 0
}

# Emit the advisory, if any target earned one. Called after the judging loop.
_flush_advice() {
  [ -z "$ADVISE_TARGET" ] && exit 0
  _advise "This write is bulk (${ADVISE_N} lines, threshold ${BULK}) and lands outside your work dir:
  $ADVISE_TARGET

You are '${AF_AGENT:-this agent}', a mini-orchestrator. Bulk writing is exactly what the
delegate-to-local-model skill is for: it is free, it runs in its own process, and it keeps
the tokens off your context. Mailing the peer who owns this area is the other route.

The write was ALLOWED — if you have already thought about it, carry on. If you were just
doing it because it was quicker than delegating, delegate it and verify what comes back."
}

case "$tool" in
  Bash)
    cmd="$(_field command)"
    [ -z "$cmd" ] && exit 0
    cmdhead="$(printf '%s' "$cmd" | head -1 | cut -c1-160)"
    [ "$cmdhead" != "$cmd" ] && cmdhead="$cmdhead …"
    # ADVISORY BLIND SPOT, accepted: for Bash we can only measure the COMMAND, and only a
    # heredoc actually carries the payload there. `cp big.py repo/`, `sed -i`, `tee`,
    # `curl -o`, `python3 gen.py > out.py` are one line each, so `advised` says nothing
    # about them however much they write. `required` is unaffected — it judges the target,
    # not the size. Fixing this properly means predicting what a command will produce,
    # which is not something a PreToolUse hook can do.

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
      # Only the command's FIRST LINE goes into the label. It ends up inside
      # additionalContext, i.e. back in the model's context — and a 500-line heredoc
      # would re-inject all 500 lines in a message whose whole point is "this is bulk,
      # keep it off your context".
      _judge "$abs" "$abs  (via Bash: $cmdhead)"
    done <<< "$targets"
    _flush_advice
    # Known gaps, accepted: an interpreter writing from inside its own source
    # (`python3 -c 'open(p,"w")'`), `git checkout`, and anything that computes its
    # target at runtime. This is a routing enforcer, not a sandbox — see the header.
    exit 0
    ;;
  *)
    path="$(_field file_path)"
    [ -z "$path" ] && path="$(_field notebook_path)"
    [ -z "$path" ] && exit 0     # nothing to judge → don't guess
    _judge "$path"
    _flush_advice
    exit 0
    ;;
esac
