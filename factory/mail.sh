#!/usr/bin/env bash
# mail — reliable agent↔agent message transport for the factory.
#
# THE IDEA: the push carries a DOORBELL, not the letter.
#
# The old channel typed the whole message into the recipient's TUI with
# `tmux send-keys -l`. That works until the payload has quotes, backslashes,
# newlines or a regex in it — and until an autocomplete popup eats the Enter.
# There is no ack, no ordering, no retry: a message either lands or vanishes.
#
# Here the payload NEVER goes through the keyboard. It is appended to the
# recipient's mailbox file; the only thing typed into their pane is a fixed,
# path-free command that reads it back:
#
#     !bash $AF_MAIL
#
# Empirically (verified on a live agent, see README):
#   - `!cmd` in the TUI runs the command AND triggers a model turn, so one
#     send-keys both delivers and wakes. No tool-call spent deciding to read.
#   - Typed while the agent is GENERATING, it queues and fires exactly at the
#     turn boundary — a message can never interrupt work in progress.
#   - `$AF_MAIL` expands (shell mode inherits the agent's env), so the typed
#     text contains no slash → the file-autocomplete popup never opens.
#
# DELIVERY GUARANTEE: each mailbox has a cursor = how many messages the agent
# has actually read. The cursor is the ACK. A sender can see that its message
# is still unread and ring again; nothing is lost if the agent was dead, busy,
# or restarted mid-delivery.
#
#   mail.sh send --to <agent> [--kind K] [--from F] <text>   append + ring
#   mail.sh read [--agent A] [--peek]                        read unread, ack
#   mail.sh unread [--agent A]                               count unread
#   mail.sh ring <agent>                                     doorbell only
#   mail.sh dump <agent>                                     whole mailbox
#
# Identity/paths come from env injected at spawn (AF_AGENT, AF_SLUG, AF_ROOT).
# Mailboxes are PER-SLUG: agents of one project can never see another's mail.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLUG="${AF_SLUG:-proj}"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
MAILROOT="${AF_MAILROOT:-$ROOT/.ai/$SLUG/mail}"   # per-slug: no cross-project leaks
SELF="${AF_AGENT:-orchestrator}"                  # who am I (unset => I'm the orchestrator)

_box()    { printf '%s/%s.jsonl'  "$MAILROOT" "$1"; }
_cursor() { printf '%s/%s.cursor' "$MAILROOT" "$1"; }
_cap()    { printf '%s/cap-%s'    "$MAILROOT" "$1"; }   # exists => understands the $AF_MAIL doorbell
_pane()   { printf '%s/pane-%s'   "$MAILROOT" "$1"; }   # explicit tmux target (orchestrator registers here)

# `grep -c ''` prints 0 AND EXITS 1 on an empty file, so the obvious
# `[ -f x ] && grep -c '' x || echo 0` fires BOTH branches and returns the
# two-line string "0\n0". That poisons every arithmetic comparison downstream —
# the Stop hook's `-gt 0` throws "integer expression expected" and silently stops
# delivering mail. Capture, then default.
_lines() {
  [ -f "$1" ] || { echo 0; return; }
  local n; n="$(grep -c '' "$1" 2>/dev/null)"; echo "${n:-0}"
}
# A cursor ahead of the mailbox means the box was truncated or rotated under it
# (AF_ROOT lives in /tmp, which macOS purges) while the cursor survived. Left
# alone, `unread` goes negative forever and no mail is ever delivered again —
# silently. Clamp instead.
_read_cursor() {
  local c t; c="$(cat "$(_cursor "$1")" 2>/dev/null)"; c="${c:-0}"
  case "$c" in ''|*[!0-9]*) c=0 ;; esac
  t="$(_lines "$(_box "$1")")"
  [ "$c" -gt "$t" ] && c="$t"
  echo "$c"
}

# The cursor is a read-modify-write, and the two readers we DESIGNED are racy by
# construction: the Stop hook runs `mail read` at the turn boundary while the
# doorbell has typed `!bash $AF_MAIL read` into the same pane. Both fire at once,
# both deliver the same messages, and the later writer can even rewind the cursor.
# mkdir is the portable atomic test-and-set (no flock(1) on macOS).
_lock() {
  local d="$MAILROOT/.lock-$1" i
  for ((i=0; i<50; i++)); do
    mkdir "$d" 2>/dev/null && return 0
    sleep 0.1
  done
  # A stale lock must not wedge the mailbox forever — take it after 5s.
  rm -rf "$d" 2>/dev/null; mkdir "$d" 2>/dev/null && return 0
  return 1
}
_unlock() { rm -rf "$MAILROOT/.lock-$1" 2>/dev/null; }

# Resolve the tmux target of an agent: an explicitly registered pane wins (that's
# how a human-launched orchestrator makes itself reachable), else the
# deterministic session ai-<slug>-<agent>.
_target() {
  local a="$1" p; p="$(_pane "$a")"
  [ -f "$p" ] && { cat "$p"; return; }
  printf 'ai-%s-%s' "$SLUG" "$a"
}
_alive() { local t; t="$(_target "$1")"; tmux has-session -t "${t%%:*}" 2>/dev/null; }
# NO _busy() HERE ANY MORE. It existed to answer "is it safe to press Escape", and the
# answer was read off the screen — which is racy: a just-rung agent has not painted its
# timer yet, so it reads idle and its turn gets cancelled. The doorbell now clears with
# C-u, which never cancels a turn, so the question no longer needs asking. Typed at a
# busy agent, the command simply queues and fires at the turn boundary.
#
# Paused on a tool-permission decision. Ringing now would be destructive, not
# merely useless: the prompt is a SELECT — the doorbell text would be typed into it and
# the Enter would confirm the highlighted default. ai.sh's compact() refuses on this
# state for the same reason; the transport must too.
_permission(){ tmux capture-pane -t "$(_target "$1")" -p 2>/dev/null | grep -qE 'Do you want to proceed\?|❯ 1\. Yes'; }

# Is $2 still sitting UNSENT in the pane's live input box?
# Only the last prompt line counts. Submitted text also appears in the scrollback
# transcript, so searching the whole pane would match forever and report every
# message as unsent. The box renders as "❯ text" in normal mode and "! text" in
# shell mode (note the space after the bang — an exact-match check written
# without it silently never fires).
_pending() {
  local tgt="$1" want="$2" live
  live="$(tmux capture-pane -t "$tgt" -p 2>/dev/null | grep -E '^[[:space:]]*[❯!]' | tail -1 \
          | sed 's/^[[:space:]]*[❯!][[:space:]]*//')"
  [ "$live" = "$want" ]
}

# --- send -----------------------------------------------------------------
# One physical line per message, so appends stay atomic: a write smaller than
# PIPE_BUF to an O_APPEND fd cannot interleave with another writer's. That budget
# is in BYTES, not characters — a 2000-char Cyrillic body JSON-encodes to \uXXXX
# at 6 bytes each (~12KB) and would blow straight past it. So the threshold is
# measured on the ENCODED line, and anything too long spills to its own blob file
# with only a short reference left in the jsonl.
BLOB_AT=2000        # bytes of encoded line we consider safely atomic

# Encode one message as a single JSON line. jq if present, python3 otherwise —
# this is the transport's only hard dependency and its failure mode is a message
# that is never appended and never noticed. The old TSV path had no dependency at
# all; the least we owe it is a second way to succeed.
_encode() {
  local id="$1" ts="$2" from="$3" to="$4" kind="$5" key="$6" val="$7"
  if command -v jq >/dev/null 2>&1; then
    jq -cn --arg id "$id" --arg ts "$ts" --arg from "$from" --arg to "$to" \
           --arg kind "$kind" --arg k "$key" --arg v "$val" \
      '{id:$id, ts:($ts|tonumber), from:$from, to:$to, kind:$kind} + {($k): $v}' 2>/dev/null && return 0
  fi
  AF_E_ID="$id" AF_E_TS="$ts" AF_E_FROM="$from" AF_E_TO="$to" AF_E_KIND="$kind" \
  AF_E_KEY="$key" AF_E_VAL="$val" python3 -c '
import json, os
m = {"id": os.environ["AF_E_ID"], "ts": int(os.environ["AF_E_TS"]),
     "from": os.environ["AF_E_FROM"], "to": os.environ["AF_E_TO"],
     "kind": os.environ["AF_E_KIND"], os.environ["AF_E_KEY"]: os.environ["AF_E_VAL"]}
print(json.dumps(m, ensure_ascii=False))' 2>/dev/null
}

send() {
  local to="" kind="" from="$SELF"
  while [ "${1:-}" ]; do
    case "$1" in
      --to)   to="${2:-}";   shift 2 || break ;;
      --kind) kind="${2:-}"; shift 2 || break ;;
      --from) from="${2:-}"; shift 2 || break ;;
      *) break ;;
    esac
  done
  kind="${kind:-fyi}"
  local body="$*"
  [ -z "$to" ]   && { echo "[mail] usage: mail.sh send --to <agent> [--kind K] <text>"; return 1; }
  [ -z "$body" ] && { echo "[mail] refusing to send an empty message"; return 1; }

  mkdir -p "$MAILROOT/blob"
  local id ts line
  ts="$(date +%s)"
  id="m-$ts-$$-$RANDOM"

  line="$(_encode "$id" "$ts" "$from" "$to" "$kind" body "$body")"
  [ -z "$line" ] && { echo "[mail] FAILED to encode the message (no working jq or python3) — NOT SENT"; return 1; }
  # Judge atomicity on the encoded bytes, not the source characters.
  if [ "$(printf '%s' "$line" | wc -c)" -gt "$BLOB_AT" ]; then
    local blob="$MAILROOT/blob/$id.txt"
    printf '%s' "$body" > "$blob"
    line="$(_encode "$id" "$ts" "$from" "$to" "$kind" body_file "$blob")"
    [ -z "$line" ] && { echo "[mail] FAILED to encode the message — NOT SENT"; return 1; }
  fi
  printf '%s\n' "$line" >> "$(_box "$to")"

  # Track whether an agent is MID-TASK, so compaction can tell a turn boundary
  # from a task boundary. A turn ends many times inside one task; compacting at
  # the wrong one throws away the working state the agent still needs. The mail
  # protocol already carries the signal — a task goes out, a done/result comes
  # back — so we record it rather than invent a second mechanism.
  #
  # `done`/`result` clears the state of the SENDER only when it is answering the
  # party that tasked it. Clearing on any done/result would let a side-reply to a
  # peer mark an agent idle while its real task is still open.
  case "$kind" in
    task) printf 'busy' > "$MAILROOT/state-$to" ;;
    done|result)
      if [ "$(cat "$MAILROOT/tasker-$from" 2>/dev/null)" = "$to" ]; then
        printf 'idle' > "$MAILROOT/state-$from"
      fi ;;
  esac
  [ "$kind" = task ] && printf '%s' "$from" > "$MAILROOT/tasker-$to"

  local seq; seq="$(_lines "$(_box "$to")")"
  if ring "$to"; then
    echo "[mail] $from → $to [$kind] #$seq delivered (doorbell rung), id=$id"
  else
    echo "[mail] $from → $to [$kind] #$seq QUEUED — '$to' has no live/idle pane. It will be rung on next 'ai up/revive $to', or read on its next 'mail read'. id=$id"
  fi
}

# --- ring (the doorbell) ---------------------------------------------------
# Type a fixed command into the recipient's pane. It runs, prints their unread
# mail into their context, and wakes them. We never type the message itself.
ring() {
  local to="${1:-}"; [ -z "$to" ] && { echo "[mail] usage: mail.sh ring <agent>"; return 1; }
  local tgt; tgt="$(_target "$to")"
  _alive "$to" || return 1

  # A pane paused on a permission prompt must NOT be rung. Escape there REJECTS
  # the pending tool call, and the doorbell text would be typed into a select
  # prompt. The message stays in the mailbox; the agent reads it after the human
  # (or `ai approve`) answers.
  _permission "$to" && return 1

  # Clear the input box so the command lands clean (leftover text would concatenate
  # with ours). C-u, NOT Escape: Escape cancels a turn in progress, so it could only be
  # sent when `_busy` said the agent was idle — and that is a screen read with a race
  # in it. C-u clears the line and closes any popup WITHOUT touching a running turn
  # (verified live), so it needs no guard and can never kill work.
  # Typed at a busy agent the command still queues and fires at the turn boundary.
  tmux send-keys -t "$tgt" C-u 2>/dev/null; sleep 0.2

  if [ -f "$(_cap "$to")" ]; then
    # Mail-capable agent: AF_MAIL is in its env, so the typed text has no slash
    # and the file-autocomplete popup cannot open. This is the reliable path.
    # `read` is required: with no subcommand mail.sh prints its help, and the
    # agent would be left to GUESS that it should go fetch its mail — which is
    # exactly the model-judgment dependency this channel exists to remove.
    # Still slash-free, so the autocomplete popup stays shut.
    local doorbell='!bash $AF_MAIL read' body='bash $AF_MAIL read'
    local try
    for try in 1 2; do
      tmux send-keys -t "$tgt" -l "$doorbell" 2>/dev/null || return 1
      sleep 0.2
      tmux send-keys -t "$tgt" Enter 2>/dev/null || return 1
      sleep 0.5
      # Verify it actually SUBMITTED. `send` used to claim "delivered" purely on
      # the strength of having typed the keys, while a popup could still swallow
      # the Enter.
      #
      # This must look at the LIVE INPUT LINE only, not the whole pane: the pane
      # also holds the transcript of previous rings, so a substring search over it
      # matches forever and reports "unsent" every time. (An earlier attempt did
      # exactly that, and was additionally dead code — the TUI renders shell mode
      # as "! bash …", with a space, so a grep for "!bash …" never matched at all
      # and the check silently always passed.) Isolate the last prompt line and
      # compare its contents, tolerating either rendering.
      _pending "$tgt" "$body" || return 0
      # Still sitting in the box → a popup ate the Enter. C-u clears the line (popup
      # included), so retype from scratch — and unlike Escape it cannot cancel a turn
      # that started in the meantime.
      tmux send-keys -t "$tgt" C-u 2>/dev/null; sleep 0.2
    done
    return 1
  else
    # DEGRADED PATH — for agents spawned before the mail channel existed (no
    # AF_MAIL in their env, so the path-free doorbell is impossible) and for an
    # orchestrator launched as a plain `claude`.
    #
    # Shell mode is not usable here: without $AF_MAIL we would have to type a
    # literal path, and a path opens the file-autocomplete popup which swallows
    # the Enter. Clearing after typing does NOT rescue that — it wipes the whole input
    # line, command included (observed: the pane was left holding an empty "! " and the
    # agent never woke).
    #
    # So we send an ordinary prompt and let the agent run the command itself. That
    # reintroduces a model-judgment step — it has to decide to obey — which is
    # precisely what the fast path removes. Acceptable only because these agents
    # are legacy; `ai revive <name>` moves one onto the reliable channel for good.
    local prompt="NEW MAIL — run: bash $HERE/mail.sh read"
    local try pending
    for try in 1 2; do
      tmux send-keys -t "$tgt" C-u 2>/dev/null; sleep 0.2
      tmux send-keys -t "$tgt" -l "$prompt" 2>/dev/null || return 1
      sleep 0.2
      tmux send-keys -t "$tgt" Enter 2>/dev/null || return 1
      sleep 0.5
      # Submitted ⇔ the live input box no longer holds our text. A popup that ate
      # the Enter leaves it sitting there; the next pass clears the line with C-u first.
      pending="$(tmux capture-pane -t "$tgt" -p 2>/dev/null | grep -E '❯|^!' | tail -1 | sed 's/.*❯[[:space:]]*//; s/^![[:space:]]*//')"
      [ "$pending" != "$prompt" ] && return 0
    done
    return 1
  fi
  return 0
}

# Decode one stored line for display. This MUST have the same fallback `_encode`
# has: `send` succeeded via python3 on a jq-less host, and then `read` printed an
# empty envelope — while the cursor advanced anyway (it moves before printing).
# The message was not merely undelivered, it was destroyed. A decoder that is
# weaker than the encoder is a data-loss bug, not a formatting one.
_decode() {
  local line="$1" out=""
  if command -v jq >/dev/null 2>&1; then
    out="$(printf '%s' "$line" | jq -r '
      "── from: \(.from)   kind: \(.kind)   id: \(.id)",
      (if .body_file then (.body_file) else .body end)' 2>/dev/null)"
  fi
  [ -n "$out" ] && { printf '%s\n' "$out"; return; }
  printf '%s' "$line" | python3 -c '
import json, sys
try:
    m = json.loads(sys.stdin.read())
except Exception:
    sys.exit(1)
print("── from: %s   kind: %s   id: %s" % (m.get("from"), m.get("kind"), m.get("id")))
print(m.get("body_file") or m.get("body") or "")' 2>/dev/null
}

# --- read (the ack) --------------------------------------------------------
# Print everything since the cursor, then advance it. Advancing the cursor IS the
# acknowledgement: a sender can compare it against the mailbox length to see that
# a message was actually consumed, and ring again if it wasn't. --peek reads
# without acking (for a hook that wants to look before deciding).
read_() {
  local who="$SELF" peek=0
  while [ "${1:-}" ]; do
    case "$1" in
      --agent) who="${2:-$SELF}"; shift 2 || break ;;
      --peek)  peek=1; shift ;;
      *) break ;;
    esac
  done
  mkdir -p "$MAILROOT"

  # Read-modify-write of the cursor under a lock. Without it the two readers this
  # design INTENDS to have — the Stop hook at the turn boundary, and the doorbell
  # typed into the same pane — both snapshot the same cursor and deliver the same
  # messages twice, and the later writer can rewind the cursor so they arrive a
  # third time. "The cursor is the ack" is only true if the ack is atomic.
  _lock "$who" || { echo "[mail] mailbox of '$who' is locked by another reader — try again"; return 1; }

  local box cur tot
  box="$(_box "$who")"; cur="$(_read_cursor "$who")"; tot="$(_lines "$box")"
  if [ "$tot" -le "$cur" ]; then _unlock "$who"; echo "[mail] no new mail for '$who'"; return 0; fi

  # Advance the cursor BEFORE printing. If we print first and die (timeout, killed
  # pane) the cursor never moves and the message is re-delivered forever; moving
  # first means at-most-once, and an unread message that was truly missed is still
  # visible in `mail dump`. Re-delivering an escalation on a loop is the worse
  # failure: it is what the exactly-once claim exists to prevent.
  [ "$peek" = 1 ] || printf '%s' "$tot" > "$(_cursor "$who")"
  _unlock "$who"

  echo "═══ MAIL for '$who' — $((tot-cur)) new ═══"
  sed -n "$((cur+1)),${tot}p" "$box" | while IFS= read -r line; do
    _decode "$line" | while IFS= read -r out; do
      # A blob reference is expanded here, so the agent sees the full text
      # inline and never has to go open a file.
      if [ -f "$out" ] && [[ "$out" == "$MAILROOT/blob/"* ]]; then cat "$out"; echo
      else printf '%s\n' "$out"; fi
    done
  done
  echo "═══ end of mail ═══"
  echo "Reply with: bash \$AF_MAIL send --to <agent> --kind <question|blocked|result|done|fyi> \"...\""
}

# How many messages the agent has NOT yet consumed. This is what a sender polls
# to decide whether to ring again — the retry signal.
unread() {
  local who="${1:-$SELF}"
  [ "${1:-}" = "--agent" ] && who="${2:-$SELF}"
  echo $(( $(_lines "$(_box "$who")") - $(_read_cursor "$who") ))
}

# The recovery path when something went wrong with delivery — so it must not
# depend on the same tool whose absence caused the loss.
dump() {
  local who="${1:-$SELF}" box line; box="$(_box "$who")"
  [ -s "$box" ] || { echo "[mail] mailbox of '$who' is empty"; return; }
  while IFS= read -r line; do _decode "$line"; done < "$box"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  send)   send "$@" ;;
  read)   read_ "$@" ;;
  unread) unread "$@" ;;
  ring)   ring "$@" && echo "[mail] rang '$1'" || echo "[mail] '$1' has no live pane" ;;
  dump)   dump "$@" ;;
  *) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
