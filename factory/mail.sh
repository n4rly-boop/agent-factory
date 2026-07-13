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

_lines()  { [ -f "$1" ] && grep -c '' "$1" 2>/dev/null || echo 0; }
_read_cursor() { local c; c="$(cat "$(_cursor "$1")" 2>/dev/null)"; echo "${c:-0}"; }

# Resolve the tmux target of an agent: an explicitly registered pane wins (that's
# how a human-launched orchestrator makes itself reachable), else the agent's
# deterministic session name ai-<slug>-<agent>.
_target() {
  local a="$1" p; p="$(_pane "$a")"
  [ -f "$p" ] && { cat "$p"; return; }
  printf 'ai-%s-%s' "$SLUG" "$a"
}
_alive() { local t; t="$(_target "$1")"; tmux has-session -t "${t%%:*}" 2>/dev/null; }
# Actively generating: a live "(Ns · …)" timer on screen. Escape is only safe when
# this is false — mid-turn it would CANCEL the turn instead of closing a popup.
_busy()  { tmux capture-pane -t "$(_target "$1")" -p 2>/dev/null | grep -qE '\([0-9]+s · '; }

# --- send -----------------------------------------------------------------
# One physical line per message, so appends stay atomic (a single short write to
# an O_APPEND fd cannot interleave). Bodies over BLOB_AT are spilled to their own
# file and referenced by path, which keeps the jsonl line small no matter how big
# the message is — the 4KB regex-laden briefs agents actually send stay atomic.
BLOB_AT=2000

send() {
  local to="" kind="blocked" from="$SELF" a
  while [ "${1:-}" ]; do
    case "$1" in
      --to)   to="${2:-}";   shift 2 || break ;;
      --kind) kind="${2:-}"; shift 2 || break ;;
      --from) from="${2:-}"; shift 2 || break ;;
      *) break ;;
    esac
  done
  local body="$*"
  [ -z "$to" ]   && { echo "[mail] usage: mail.sh send --to <agent> [--kind K] <text>"; return 1; }
  [ -z "$body" ] && { echo "[mail] refusing to send an empty message"; return 1; }

  mkdir -p "$MAILROOT/blob"
  local id ts line
  ts="$(date +%s)"
  id="m-$ts-$$-$RANDOM"

  # jq builds the JSON so quotes/backslashes/newlines/unicode in the body are
  # encoded, not guessed at. The body is data; it must never be parsed as syntax.
  if [ "${#body}" -gt "$BLOB_AT" ]; then
    local blob="$MAILROOT/blob/$id.txt"
    printf '%s' "$body" > "$blob"
    line="$(jq -cn --arg id "$id" --arg ts "$ts" --arg from "$from" --arg to "$to" \
                   --arg kind "$kind" --arg bf "$blob" \
              '{id:$id, ts:($ts|tonumber), from:$from, to:$to, kind:$kind, body_file:$bf}')"
  else
    line="$(jq -cn --arg id "$id" --arg ts "$ts" --arg from "$from" --arg to "$to" \
                   --arg kind "$kind" --arg body "$body" \
              '{id:$id, ts:($ts|tonumber), from:$from, to:$to, kind:$kind, body:$body}')"
  fi
  [ -z "$line" ] && { echo "[mail] jq failed to encode the message — not sent"; return 1; }
  printf '%s\n' "$line" >> "$(_box "$to")"

  # Track whether an agent is MID-TASK, so compaction can tell a turn boundary
  # from a task boundary. A turn ends many times inside one task; compacting at
  # the wrong one throws away the working state the agent still needs. The mail
  # protocol already carries the signal — a task goes out, a done/result comes
  # back — so we just record it instead of inventing a second mechanism.
  case "$kind" in
    task)        printf 'busy' > "$MAILROOT/state-$to" ;;
    done|result) printf 'idle' > "$MAILROOT/state-$from" ;;
  esac

  local seq; seq="$(_lines "$(_box "$to")")"
  if ring "$to"; then
    echo "[mail] $from → $to [$kind] #$seq delivered (doorbell rung), id=$id"
  else
    echo "[mail] $from → $to [$kind] #$seq queued in mailbox (no live pane) — they'll read it on next 'mail read', id=$id"
  fi
}

# --- ring (the doorbell) ---------------------------------------------------
# Type a fixed command into the recipient's pane. It runs, prints their unread
# mail into their context, and wakes them. We never type the message itself.
ring() {
  local to="$1" tgt; tgt="$(_target "$to")"
  _alive "$to" || return 1

  # Escape closes a stray autocomplete popup / leaves sticky shell mode, so the
  # command we type lands in a clean input box. ONLY when idle: mid-turn, Escape
  # cancels the agent's work. When busy we skip it and just type — the TUI queues
  # the command and fires it at the turn boundary, which is exactly what we want.
  _busy "$to" || { tmux send-keys -t "$tgt" Escape 2>/dev/null; sleep 0.2; }

  if [ -f "$(_cap "$to")" ]; then
    # Mail-capable agent: AF_MAIL is in its env, so the typed text has no slash
    # and the file-autocomplete popup cannot open. This is the reliable path.
    # `read` is required: with no subcommand mail.sh prints its help, and the
    # agent would be left to GUESS that it should go fetch its mail — which is
    # exactly the model-judgment dependency this channel exists to remove.
    # Still slash-free, so the autocomplete popup stays shut.
    tmux send-keys -t "$tgt" -l '!bash $AF_MAIL read' 2>/dev/null || return 1
    sleep 0.2
    tmux send-keys -t "$tgt" Enter 2>/dev/null || return 1
  else
    # DEGRADED PATH — for agents spawned before the mail channel existed (no
    # AF_MAIL in their env, so the path-free doorbell is impossible) and for an
    # orchestrator launched as a plain `claude`.
    #
    # Shell mode is not usable here: without $AF_MAIL we would have to type a
    # literal path, and a path opens the file-autocomplete popup which swallows
    # the Enter. Escape does NOT rescue that — in Claude Code it CLEARS THE WHOLE
    # INPUT LINE, so dismissing the popup after typing wipes the command (observed:
    # the pane was left holding an empty "! " and the agent never woke).
    #
    # So we send an ordinary prompt and let the agent run the command itself. That
    # reintroduces a model-judgment step — it has to decide to obey — which is
    # precisely what the fast path removes. Acceptable only because these agents
    # are legacy; `ai revive <name>` moves one onto the reliable channel for good.
    local prompt="NEW MAIL — run: bash $HERE/mail.sh read"
    local try pending
    for try in 1 2; do
      _busy "$to" || { tmux send-keys -t "$tgt" Escape 2>/dev/null; sleep 0.2; }
      tmux send-keys -t "$tgt" -l "$prompt" 2>/dev/null || return 1
      sleep 0.2
      tmux send-keys -t "$tgt" Enter 2>/dev/null || return 1
      sleep 0.5
      # Submitted ⇔ the live input box no longer holds our text. A popup that ate
      # the Enter leaves it sitting there; retry re-opens with a clearing Escape.
      pending="$(tmux capture-pane -t "$tgt" -p 2>/dev/null | grep -E '❯|^!' | tail -1 | sed 's/.*❯[[:space:]]*//; s/^![[:space:]]*//')"
      [ "$pending" != "$prompt" ] && return 0
    done
    return 1
  fi
  return 0
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
  local box cur tot
  box="$(_box "$who")"; cur="$(_read_cursor "$who")"; tot="$(_lines "$box")"
  if [ "$tot" -le "$cur" ]; then echo "[mail] no new mail for '$who'"; return 0; fi

  echo "═══ MAIL for '$who' — $((tot-cur)) new ═══"
  sed -n "$((cur+1)),${tot}p" "$box" | while IFS= read -r line; do
    printf '%s' "$line" | jq -r '
      "── from: \(.from)   kind: \(.kind)   id: \(.id)",
      (if .body_file then (.body_file) else .body end)' 2>/dev/null \
    | while IFS= read -r out; do
        # A blob reference is expanded here, so the agent sees the full text
        # inline and never has to go open a file.
        if [ -f "$out" ] && [[ "$out" == "$MAILROOT/blob/"* ]]; then cat "$out"; echo
        else printf '%s\n' "$out"; fi
      done
  done
  echo "═══ end of mail ═══"
  echo "Reply with: bash \$AF_MAIL send --to <agent> --kind <question|result|done|fyi> \"...\""

  [ "$peek" = 1 ] || { mkdir -p "$MAILROOT"; printf '%s' "$tot" > "$(_cursor "$who")"; }
}

# How many messages the agent has NOT yet consumed. This is what a sender polls
# to decide whether to ring again — the retry signal.
unread() {
  local who="${1:-$SELF}"
  [ "${1:-}" = "--agent" ] && who="${2:-$SELF}"
  echo $(( $(_lines "$(_box "$who")") - $(_read_cursor "$who") ))
}

dump() {
  local who="${1:-$SELF}" box; box="$(_box "$who")"
  [ -s "$box" ] || { echo "[mail] mailbox of '$who' is empty"; return; }
  jq -r '"[\(.ts)] \(.from) → \(.to) [\(.kind)]: \(.body // ("(blob) " + .body_file))"' "$box" 2>/dev/null
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
