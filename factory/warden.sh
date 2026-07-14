#!/usr/bin/env bash
# warden — the thing that watches a line WHEN NOBODY IS DRIVING IT.
#
#   warden watch     start it for this line (detached; safe to re-run; `line up` does it)
#   warden stop
#   warden status    quota, reset time, who got cut off, when it last swept
#
# It does two jobs, and they are the same job: keep the line alive through the hours when
# no human and no orchestrator is issuing commands.
#
#   1. CONTEXT. `ai sweep` compacts idle agents past their threshold — but sweep only ever
#      ran from `ai post` / `ai mail` / `ai sweep`, i.e. only when the DRIVING session
#      spoke. An autonomous line does not go through those: the agents mail each other via
#      $AF_MAIL (mail.sh), which never swept. So a line left to work overnight was never
#      compacted at all. Observed, and it is why this file grew a second job: lead reached
#      767k tokens against a 500k HARD threshold, with nothing to trip it. "Automatic"
#      compaction that only fires while a human is at the keyboard is not automatic; it is
#      a manual command with a misleading name.
#
#   2. THE USAGE LIMIT. Account-wide: it kills every agent AND the orchestrator session at
#      the same instant, mid-turn. The rescuer therefore cannot be a Claude — there is none
#      left. See the detail below.
#
# Both are the same shape: a shell loop that spends no tokens and does not need permission
# from anyone. That is the only kind of process that can be relied on here.
#
# HOW IT KNOWS THE LIMIT LIFTED. Two documented signals, no screen-scraping:
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
AI="$HERE/ai.sh"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
CWD="${AF_CWD:-$(pwd)}"
SLUG="${AF_SLUG:-$(basename "$CWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g' | cut -c1-12)}"
[ -z "$SLUG" ] && SLUG="proj"
STATE="$ROOT/.ai/$SLUG"
PID="$STATE/warden.pid"
LOG="$STATE/warden.log"
TICK="${AI_LIMITS_TICK:-60}"       # how often the watcher looks. It is asleep the rest of the time.
GRACE="${AI_LIMITS_GRACE:-45}"     # seconds AFTER resets_at before waking. The reset is not
                                   # instant on the server side, and a wake that lands one
                                   # second early just burns the agent's turn on the same error.
SWEEP_EVERY="${AI_SWEEP_EVERY:-300}"   # how often to run the context guard. Not every tick:
                                       # `ai sweep` reads every agent's session log, and there
                                       # is no point paying that once a minute for a threshold
                                       # that takes an hour of work to cross.
SWEEP_OFF="${AI_SWEEP_OFF:-0}"

_sess()  { printf 'ai-%s-%s' "$SLUG" "$1"; }
_alive() { tmux has-session -t "$(_sess "$1")" 2>/dev/null; }
_sid()   { cat "$STATE/sid-$1" 2>/dev/null; }
_log()   { printf '[warden %s] %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

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
  AF_AGENT=warden AF_SLUG="$SLUG" AF_ROOT="$ROOT" \
    bash "$MAIL" send --to "$a" --from orchestrator --kind task \
    "The subscription usage limit cut your turn off ($why). It has now reset.

Your context is intact — only the interrupted turn was lost, along with whatever tool call was in flight. Nothing else was rolled back.

Pick the work back up: re-read your brief and your report file, work out where you were cut off, redo the lost step, and carry on. If you cannot tell what you were doing, say so and ask, rather than guessing." >/dev/null 2>&1
}

watch_() {
  local all="${1:-}"
  if [ -f "$PID" ] && kill -0 "$(cat "$PID" 2>/dev/null)" 2>/dev/null; then
    echo "[warden] already watching line '$SLUG' (pid $(cat "$PID")). Stop it first: warden stop"
    return 0
  fi
  mkdir -p "$STATE"
  nohup env AF_ROOT="$ROOT" AF_SLUG="$SLUG" AF_CWD="$CWD" AI_LIMITS_ALL="${all:+1}" \
        bash "$HERE/warden.sh" _loop >/dev/null 2>&1 &
  printf '%s' "$!" > "$PID"
  echo "[warden] watching line '$SLUG' (pid $(cat "$PID"))."
  echo "[warden]   compacts idle agents past their threshold every $((SWEEP_EVERY/60))m — with or without you"
  echo "[warden]   and wakes the line when the usage limit resets. It spends no tokens, so the limit cannot kill it."
  echo "[warden]   log: $LOG"
}

# The loop. It must be boring: it runs for hours, unattended, through the exact event that
# kills everything else on the machine.
_loop() {
  local all="${AI_LIMITS_ALL:-}" last_sweep=0
  _log "warden up (tick ${TICK}s, grace ${GRACE}s, sweep every ${SWEEP_EVERY}s)"
  while :; do
    sleep "$TICK"
    [ -f "$PID" ] || { _log "stopped"; exit 0; }

    # THE CONTEXT GUARD. This is the whole reason the warden exists as well as the limit
    # rescue: `ai sweep` used to run only when the driving session called post/mail/sweep,
    # so a line working autonomously overnight was never compacted — the agents talk to
    # each other through mail.sh, which never swept. Now the guard runs on a clock, whether
    # anyone is watching or not.
    #
    # `sweep` itself is the careful one: it skips agents that are generating, agents on a
    # permission prompt, and agents below their threshold; it compacts a BUSY agent only
    # past the HARD line, where running out of context would lose everything anyway.
    if [ "$SWEEP_OFF" != 1 ] && [ $(( $(date +%s) - last_sweep )) -ge "$SWEEP_EVERY" ]; then
      last_sweep="$(date +%s)"
      local out
      out="$(AF_SLUG="$SLUG" AF_ROOT="$ROOT" AF_CWD="$CWD" AI_SWEEP_OFF=0 \
             bash "$AI" sweep 2>&1 | grep -E "compact|⚠" | tr '\n' ' ')"
      [ -n "$out" ] && _log "sweep: $out"
    fi

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
  [ -z "$p" ] && { echo "[warden] not watching line '$SLUG'."; return 0; }
  kill "$p" 2>/dev/null
  rm -f "$PID"
  echo "[warden] watcher stopped."
}

status() {
  local p; p="$(cat "$PID" 2>/dev/null)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo "[warden] watcher: LIVE (pid $p)"
  else echo "[warden] watcher: not running  — start it: warden watch"; fi
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
