#!/usr/bin/env bash
# polling — put an agent on a timer. Every N MINUTES it gets the same message.
#
#   polling start <agent> <minutes> <message> [--times N] [--kind K]
#   polling stop  [agent]        # no agent = $AF_AGENT, i.e. an agent switching ITSELF off
#   polling list                 # every timer on this line
#   polling status <agent>
#
# The interval is MINUTES, everywhere — the argument, the state file, every message this
# script prints, and the hint the agent gets. Seconds exist only inside the sleep. A tool
# whose flag says one unit and whose docs say another is how someone eventually types
# `polling start orc 20` meaning twenty minutes and gets a tick every twenty seconds.
#
# WHY IT DELIVERS BY MAIL AND NOT BY TYPING. The obvious build is `sleep N; ai say
# <agent> "<msg>"` in a loop. That types into a live TUI on a timer, with no idea what
# the agent is doing at that instant — mid-turn, on a permission prompt, halfway through
# a tool call. The message lands inside someone else's turn. Mail has a mailbox, a
# cursor and a doorbell: the letter is APPENDED (always safe, whatever the agent is
# doing) and only the doorbell is typed. That is the channel; polling just rings it on
# a clock.
#
# THREE WAYS A TIMER TURNS INTO A WEAPON, AND WHAT STOPS EACH:
#
# 1. IT OUTLIVES ITS AGENT. The agent dies, the loop keeps running, and the next agent
#    to take that name inherits a stream of orders meant for a ghost. The loop records
#    the agent's SID at start and re-checks it every tick: a changed sid means a
#    different agent wearing the same name, and the timer exits rather than talk to it.
#    (`ai revive` / `line up --resume` keep the sid — a resumed agent keeps its timer.
#    A fresh spawn mints a new one — that agent gets a clean slate, no inherited timer.)
#
# 2. IT OUTRUNS THE AGENT. An interval shorter than a turn means every tick lands on an
#    agent still working on the last one. The mailbox grows, and the agent spends its
#    life reading its own alarm clock. So: a tick is SKIPPED while the previous one is
#    still unread. The timer cannot build a backlog — one outstanding message, ever.
#    (And the floor is 1 minute. A poller is not a busy-wait.)
#
# 3. NOBODY REMEMBERS IT EXISTS. A forgotten timer burns tokens forever. `--times N`
#    exists for that, `polling list` shows every live one, and the agent itself can run
#    `bash $AF_POLL stop` — it is the one that knows when the wait is over.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIL="$HERE/mail.sh"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
CWD="${AF_CWD:-$(pwd)}"
SLUG="${AF_SLUG:-$(basename "$CWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g' | cut -c1-12)}"
[ -z "$SLUG" ] && SLUG="proj"
STATE="$ROOT/.ai/$SLUG"
POLLDIR="$STATE/poll"
MIN_MINUTES="${AI_POLL_MIN:-1}"        # minutes, like everything else the user types

_d()    { printf '%s/%s' "$POLLDIR" "$1"; }
_sid()  { cat "$STATE/sid-$1" 2>/dev/null; }
_sess() { printf 'ai-%s-%s' "$SLUG" "$1"; }
_alive(){ tmux has-session -t "$(_sess "$1")" 2>/dev/null; }
_now()  { date +%s; }

_running() {                       # a timer is running iff its pid is
  local d; d="$(_d "$1")"
  local pid; pid="$(cat "$d/pid" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start() {
  local agent="${1:-}" mins="${2:-}" ; shift 2 2>/dev/null || true
  local times=0 kind="fyi" msg=""
  while [ "${1:-}" ]; do
    case "$1" in
      --times) times="${2:-0}"; shift 2 || break ;;
      --kind)  kind="${2:-fyi}"; shift 2 || break ;;
      *)       msg="${msg:+$msg }$1"; shift ;;
    esac
  done

  [ -z "$agent" ] || [ -z "$mins" ] || [ -z "$msg" ] && {
    echo "[poll] usage: polling start <agent> <minutes> <message> [--times N] [--kind K]" >&2
    return 1
  }
  # `5m` is what a human types when the unit is minutes. Take it; do not make them
  # discover by silence that it parsed as garbage.
  mins="${mins%m}"; mins="${mins%min}"
  case "$mins" in
    ''|*[!0-9]*) echo "[poll] interval is in MINUTES and must be a whole number, got '$mins'" >&2; return 1 ;;
  esac
  # A tick faster than a turn is not a poll, it is a denial of service against your own
  # agent: every message lands on an agent still answering the last one.
  if [ "$mins" -lt "$MIN_MINUTES" ]; then
    echo "[poll] $mins min is below the floor of ${MIN_MINUTES} min — an agent cannot finish a" >&2
    echo "[poll]   turn that fast, so the ticks would pile up behind it." >&2
    echo "[poll]   Deliberate? AI_POLL_MIN=$mins polling start …" >&2
    return 1
  fi
  _alive "$agent" || { echo "[poll] '$agent' has no tmux session — nothing to poll." >&2; return 1; }
  local sid; sid="$(_sid "$agent")"
  [ -z "$sid" ] && { echo "[poll] no recorded session for '$agent' — refusing to poll an agent I cannot identify." >&2; return 1; }
  if _running "$agent"; then
    echo "[poll] '$agent' already has a timer (every $(cat "$(_d "$agent")/minutes" 2>/dev/null) min). Stop it first: polling stop $agent" >&2
    return 1
  fi

  local d; d="$(_d "$agent")"
  mkdir -p "$d"
  printf '%s' "$msg"      > "$d/msg"       # raw bytes: a message is data, never eval'd
  printf '%s' "$mins"     > "$d/minutes"   # MINUTES on disk too — no unit changes hands
  printf '%s' "$kind"     > "$d/kind"
  # The tick is FROM whoever set the timer — not from some fictional "poll" agent.
  # A poll-shaped sender has no pane and no reader, so the agent's answer ("pong 1")
  # was filed into a mailbox nobody would ever open. Observed on the first live test.
  # Sending it as the caller means the reply lands where the caller reads: `ai mail`.
  printf '%s' "${AF_POLL_FROM:-${AF_AGENT:-orchestrator}}" > "$d/from"
  printf '%s' "$times"    > "$d/times"
  printf '%s' "$sid"      > "$d/sid"
  printf '%s' "0"         > "$d/sent"
  printf '%s' "$(_now)"   > "$d/started"
  rm -f "$d/pid"

  # Detached, and deliberately not disowned into the void: the pid is the handle.
  nohup env AF_ROOT="$ROOT" AF_SLUG="$SLUG" AF_CWD="$CWD" \
        bash "$HERE/polling.sh" _loop "$agent" >"$d/log" 2>&1 &
  printf '%s' "$!" > "$d/pid"
  local every="every ${mins} min"
  [ "$times" != 0 ] && every="$every, $times time(s)"
  echo "[poll] '$agent' → $every: $msg"
  echo "[poll] stop it:  polling stop $agent      (the agent itself: bash \$AF_POLL stop)"
}

# The loop. Everything it does is guarded, because it runs unattended for hours.
_loop() {
  local agent="$1" d; d="$(_d "$agent")"
  local mins secs msg kind times sent sid from
  mins="$(cat "$d/minutes")"; msg="$(cat "$d/msg")"; kind="$(cat "$d/kind")"
  times="$(cat "$d/times")";  sid="$(cat "$d/sid")"
  from="$(cat "$d/from" 2>/dev/null)"; from="${from:-orchestrator}"
  secs=$((mins * 60))         # the ONE place minutes become seconds

  while :; do
    sleep "$secs"

    # Stopped from the outside (or by the agent itself).
    [ -f "$d/pid" ] || { _log "$agent" "stopped"; exit 0; }

    # The agent is gone. Do not linger: the name will be reused.
    _alive "$agent" || { _log "$agent" "'$agent' is down — timer exits"; _cleanup "$agent"; exit 0; }

    # The name is the same; the AGENT is not. A fresh spawn minted a new session id,
    # so whoever holds this name now never asked for this timer.
    local now_sid; now_sid="$(_sid "$agent")"
    if [ "$now_sid" != "$sid" ]; then
      _log "$agent" "session changed ($sid → $now_sid) — a different agent holds this name; timer exits"
      _cleanup "$agent"; exit 0
    fi

    # The previous tick is still unread. Sending another would queue an alarm behind an
    # alarm; the agent is busy or ignoring us, and either way more mail does not help.
    local un; un="$(AF_AGENT="$agent" AF_SLUG="$SLUG" AF_ROOT="$ROOT" bash "$MAIL" unread --agent "$agent" 2>/dev/null)"
    if [ "${un:-0}" -gt 0 ]; then
      _log "$agent" "skipped — previous tick still unread"
      continue
    fi

    sent="$(cat "$d/sent" 2>/dev/null || echo 0)"
    # An agent being ticked has no way to know it is on a timer, let alone that it may
    # switch it off — the mail just looks like someone nagging it every few minutes.
    # Say so ONCE, on the first tick. Repeating it every tick would buy nothing and be
    # paid for out of the agent's context, forever.
    local body="$msg"
    [ "$sent" = 0 ] && body="$msg

(You are on a timer: this message repeats every ${mins} min. When the wait is over, switch it off yourself: bash \$AF_POLL stop)"

    AF_AGENT="$from" AF_SLUG="$SLUG" AF_ROOT="$ROOT" \
      bash "$MAIL" send --to "$agent" --from "$from" --kind "$kind" "$body" >/dev/null 2>&1
    sent=$((sent+1))
    printf '%s' "$sent" > "$d/sent"
    _log "$agent" "tick $sent sent"

    if [ "$times" != 0 ] && [ "$sent" -ge "$times" ]; then
      _log "$agent" "$times tick(s) done — timer exits"
      _cleanup "$agent"; exit 0
    fi
  done
}

_log()     { printf '[poll %s] %s: %s\n' "$(date '+%H:%M:%S')" "$1" "$2"; }
_cleanup() { rm -f "$(_d "$1")/pid"; }

stop() {
  # No argument = the caller is an agent switching its own timer off. That is the whole
  # point of handing $AF_POLL to the agent: it is the one that knows the wait is over.
  local agent="${1:-${AF_AGENT:-}}"
  [ -z "$agent" ] && { echo "[poll] usage: polling stop <agent>   (inside an agent: polling stop)" >&2; return 1; }
  local d; d="$(_d "$agent")"
  local pid; pid="$(cat "$d/pid" 2>/dev/null)"
  if [ -z "$pid" ]; then
    echo "[poll] '$agent' has no timer running."
    return 0
  fi
  kill "$pid" 2>/dev/null
  rm -f "$d/pid"
  echo "[poll] '$agent' timer stopped (was every $(cat "$d/minutes" 2>/dev/null) min, $(cat "$d/sent" 2>/dev/null) tick(s) sent)."
}

list() {
  [ -d "$POLLDIR" ] || { echo "[poll] no timers on line '$SLUG'."; return 0; }
  local any=0 a d
  printf '%-10s %-9s %-7s %-6s %s\n' AGENT EVERY SENT/MAX STATE MESSAGE
  for d in "$POLLDIR"/*; do
    [ -d "$d" ] || continue
    a="$(basename "$d")"
    local st="dead"
    _running "$a" && st="live"
    local mx; mx="$(cat "$d/times" 2>/dev/null)"; [ "$mx" = 0 ] && mx="∞"
    printf '%-10s %-9s %-7s %-6s %s\n' \
      "$a" "$(cat "$d/minutes" 2>/dev/null)m" \
      "$(cat "$d/sent" 2>/dev/null)/$mx" "$st" \
      "$(cut -c1-48 < "$d/msg" 2>/dev/null)"
    any=1
  done
  [ "$any" = 0 ] && echo "[poll] no timers on line '$SLUG'."
  return 0
}

status() {
  local agent="${1:-${AF_AGENT:-}}"
  [ -z "$agent" ] && { echo "[poll] usage: polling status <agent>" >&2; return 1; }
  local d; d="$(_d "$agent")"
  [ -d "$d" ] || { echo "[poll] '$agent': no timer."; return 0; }
  _running "$agent" && echo "[poll] '$agent': LIVE" || echo "[poll] '$agent': stopped"
  echo "  every    : $(cat "$d/minutes" 2>/dev/null) min"
  echo "  sent     : $(cat "$d/sent" 2>/dev/null) / $(cat "$d/times" 2>/dev/null | sed 's/^0$/∞/')"
  echo "  kind     : $(cat "$d/kind" 2>/dev/null)"
  echo "  message  : $(cat "$d/msg" 2>/dev/null)"
  echo "  last log : $(tail -1 "$d/log" 2>/dev/null)"
}

cmd="${1:-}"; shift 2>/dev/null || true
case "$cmd" in
  start)  start "$@" ;;
  stop)   stop "$@" ;;
  list)   list "$@" ;;
  status) status "$@" ;;
  _loop)  _loop "$@" ;;
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0" ;;
esac
