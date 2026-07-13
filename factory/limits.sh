#!/usr/bin/env bash
# limits — survive the subscription usage limit: park the agents, wake them when it lifts.
#
#   limits watch [--all]     start the watcher for this line (detached; safe to re-run)
#   limits stop              stop it
#   limits status            what it knows: quota, reset time, who got cut off
#
# THE PROBLEM. The 5-hour limit is ACCOUNT-WIDE. When it lands it does not stop one agent;
# it stops every agent on the machine and the orchestrator session driving them, mid-turn,
# at the same instant. So the rescuer cannot be a Claude — there is no Claude left. It has
# to be a process that spends no tokens and does not care about the limit at all.
#
# That is this: a shell loop that sleeps.
#
# HOW IT KNOWS. Two documented signals, no screen-scraping:
#   * `StopFailure` hook (matcher rate_limit) → hooks/limit-hook.sh drops a marker for the
#     agent whose turn was killed. That distinguishes "was cut off mid-work" from "was
#     idle anyway" — only the first needs waking.
#   * The statusline JSON carries rate_limits.five_hour.resets_at — the exact epoch the
#     limit lifts. statusline.sh writes it to $STATE/limits.json. There is no CLI that
#     will tell you this from outside a session, so the agents have to leave it behind.
#
# The pane is checked too, but only as a BELT: a hook that fails to fire (wrong version,
# lost +x bit) fails SILENTLY, and a rescue system that quietly does not rescue is worse
# than none — you would find out hours later, from a line that never resumed.
#
# WHAT IT DOES NOT DO. It cannot recover the killed turn: that API call is gone, and its
# tool call with it. The agent keeps its full context, so it can pick the work back up —
# but it has to be TOLD to, and told what happened, or it will just sit there. That is the
# message it gets.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIL="$HERE/mail.sh"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
CWD="${AF_CWD:-$(pwd)}"
SLUG="${AF_SLUG:-$(basename "$CWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g' | cut -c1-12)}"
[ -z "$SLUG" ] && SLUG="proj"
STATE="$ROOT/.ai/$SLUG"
PID="$STATE/limits.pid"
LOG="$STATE/limits.log"
TICK="${AI_LIMITS_TICK:-60}"       # how often the watcher looks. It is asleep the rest of the time.
GRACE="${AI_LIMITS_GRACE:-45}"     # seconds AFTER resets_at before waking. The reset is not
                                   # instant on the server side, and a wake that lands one
                                   # second early just burns the agent's turn on the same error.

_sess()  { printf 'ai-%s-%s' "$SLUG" "$1"; }
_alive() { tmux has-session -t "$(_sess "$1")" 2>/dev/null; }
_sid()   { cat "$STATE/sid-$1" 2>/dev/null; }
_log()   { printf '[limits %s] %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# The pane belt. These are the strings Claude Code actually prints (verified against the
# 2.1.x binary): "You've hit your session limit · resets 3:45pm", plus the older wording.
_pane_limited() {
  tmux capture-pane -t "$(_sess "$1")" -p 2>/dev/null \
    | grep -qiE "hit your (session|usage) limit|usage limit reached|limit reached .*resets|limit will reset"
}

_resets_at() {           # epoch when the 5-hour window lifts, or empty if nobody has rendered yet
  python3 - "$STATE/limits.json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(int(((d.get("rate_limits") or {}).get("five_hour") or {}).get("resets_at") or 0) or "")
except Exception:
    print("")
PY
}

_pct() {
  python3 - "$STATE/limits.json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(int(((d.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage") or 0))
except Exception:
    print("")
PY
}

_agents() {              # every agent of this line that has a session
  tmux ls -F '#{session_name}' 2>/dev/null | sed -n "s/^ai-$SLUG-//p"
}

# Wake one agent. By MAIL, never by typing the message itself: the doorbell is one fixed
# token-free line, and the letter lands in the mailbox whatever state the pane is in.
_wake() {
  local a="$1" why="$2"
  AF_AGENT=limits AF_SLUG="$SLUG" AF_ROOT="$ROOT" \
    bash "$MAIL" send --to "$a" --from orchestrator --kind task \
    "The subscription usage limit cut your turn off ($why). It has now reset.

Your context is intact — only the interrupted turn was lost, along with whatever tool call was in flight. Nothing else was rolled back.

Pick the work back up: re-read your brief and your report file, work out where you were cut off, redo the lost step, and carry on. If you cannot tell what you were doing, say so and ask, rather than guessing." >/dev/null 2>&1
}

watch_() {
  local all="${1:-}"
  if [ -f "$PID" ] && kill -0 "$(cat "$PID" 2>/dev/null)" 2>/dev/null; then
    echo "[limits] already watching line '$SLUG' (pid $(cat "$PID")). Stop it first: limits stop"
    return 0
  fi
  mkdir -p "$STATE"
  nohup env AF_ROOT="$ROOT" AF_SLUG="$SLUG" AF_CWD="$CWD" AI_LIMITS_ALL="${all:+1}" \
        bash "$HERE/limits.sh" _loop >/dev/null 2>&1 &
  printf '%s' "$!" > "$PID"
  echo "[limits] watching line '$SLUG' (pid $(cat "$PID")). It spends no tokens; it will outlive the limit."
  echo "[limits]   log: $LOG"
}

# The loop. It must be boring: it runs for hours, unattended, through the exact event that
# kills everything else on the machine.
_loop() {
  local all="${AI_LIMITS_ALL:-}"
  _log "watcher up (tick ${TICK}s, grace ${GRACE}s)"
  while :; do
    sleep "$TICK"
    [ -f "$PID" ] || { _log "stopped"; exit 0; }

    local a marked=""
    for a in $(_agents); do
      # Hook marker: this agent was cut off MID-TURN. That is the case that needs a wake;
      # an agent that was idle when the limit landed lost nothing.
      if [ -f "$STATE/limited-$a" ]; then marked="$marked $a"; continue; fi
      # Belt: the hook may not have fired (old version, lost +x — hooks FAIL OPEN and say
      # nothing). Believe the screen too.
      if _pane_limited "$a"; then
        printf '%s\t%s\tpane\n' "$(date +%s)" "$(_sid "$a")" > "$STATE/limited-$a"
        _log "$a: limit detected on the PANE (the StopFailure hook did not fire — check it)"
        marked="$marked $a"
      fi
    done
    [ -z "$marked" ] && continue

    local reset; reset="$(_resets_at)"
    local now; now="$(date +%s)"
    if [ -z "$reset" ]; then
      # No agent has rendered a statusline yet, so nobody knows when the window lifts.
      # Do NOT invent a number: a wrong guess wakes them into the same wall and burns the
      # first turn of the new window. Wait and look again — the statusline writes on every
      # render, and a parked agent still renders.
      _log "cut off:$marked — but no resets_at on disk yet; waiting (is statusline.sh wired up?)"
      continue
    fi
    if [ "$now" -lt $((reset + GRACE)) ]; then
      local left=$(( reset + GRACE - now ))
      _log "cut off:$marked — sleeping $((left/60))m until the window resets"
      continue
    fi

    # Reset time has passed. Wake them, one at a time: four agents starting at the same
    # instant all fire their first request into the same fresh window.
    for a in $marked; do
      _alive "$a" || { _log "$a: gone — dropping its marker"; rm -f "$STATE/limited-$a"; continue; }
      # The name is the same; is the AGENT? A fresh spawn minted a new sid and never lived
      # through the limit — it must not be told to "carry on where you were cut off".
      local msid; msid="$(cut -f2 "$STATE/limited-$a" 2>/dev/null)"
      local nsid; nsid="$(_sid "$a")"
      if [ -n "$msid" ] && [ -n "$nsid" ] && [ "$msid" != "$nsid" ]; then
        _log "$a: session changed since it was cut off — a different agent holds the name; dropping the marker"
        rm -f "$STATE/limited-$a"; continue
      fi
      # Still showing the wall? Then the window did not really lift (the 7-day cap can hold
      # you down long past the 5-hour reset). Leave the marker; try again next tick.
      if _pane_limited "$a"; then
        _log "$a: reset time passed but the pane still shows the limit — 7-day cap? retrying next tick"
        continue
      fi
      _wake "$a" "at $(date -r "$(cut -f1 "$STATE/limited-$a")" '+%H:%M' 2>/dev/null)"
      rm -f "$STATE/limited-$a"
      _log "$a: woken — told to pick the work back up"
      sleep 10
    done
  done
}

stop() {
  local p; p="$(cat "$PID" 2>/dev/null)"
  [ -z "$p" ] && { echo "[limits] not watching line '$SLUG'."; return 0; }
  kill "$p" 2>/dev/null
  rm -f "$PID"
  echo "[limits] watcher stopped."
}

status() {
  local p; p="$(cat "$PID" 2>/dev/null)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo "[limits] watcher: LIVE (pid $p)"
  else echo "[limits] watcher: not running  — start it: limits watch"; fi
  local pct reset
  pct="$(_pct)"; reset="$(_resets_at)"
  if [ -n "$reset" ]; then
    local left=$(( reset - $(date +%s) ))
    [ "$left" -lt 0 ] && left=0
    echo "  5-hour quota : ${pct:-?}% used, resets in $((left/3600))h$(printf '%02d' $(( (left%3600)/60 )))m ($(date -r "$reset" '+%H:%M'))"
  else
    echo "  5-hour quota : unknown — no agent has rendered a statusline yet"
    echo "                 (without it the watcher cannot know WHEN to wake anyone)"
  fi
  local a any=0
  for a in $(_agents); do
    [ -f "$STATE/limited-$a" ] || continue
    echo "  cut off      : $a (at $(date -r "$(cut -f1 "$STATE/limited-$a")" '+%H:%M' 2>/dev/null))"
    any=1
  done
  [ "$any" = 0 ] && echo "  cut off      : nobody"
  echo "  log          : $LOG"
}

cmd="${1:-}"; shift 2>/dev/null || true
case "$cmd" in
  watch)  watch_ "$@" ;;
  stop)   stop "$@" ;;
  status) status "$@" ;;
  _loop)  _loop ;;
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0" ;;
esac
