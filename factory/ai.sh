#!/usr/bin/env bash
# ai — spawn a REAL interactive `claude` TUI in a visible Terminal.app window,
# then let THIS agent type into it and read its screen.
#
# How: interactive claude runs inside a tmux session (gives it a real TTY); a
# Terminal.app window is opened attached to that session, so you watch the live
# TUI. The controlling agent drives it with `tmux send-keys` and reads with
# `tmux capture-pane` — no FIFOs, no headless mode. The actual interactive app.
#
#   ai up    [name]          launch interactive claude + open Terminal window
#   ai say   [name] <text>   type text into it and submit (Enter)
#   ai keys  [name] <args>   send raw tmux keys (e.g. Escape, C-c, /model)
#   ai screen[name]          print the current TUI screen
#   ai ask   [name] <text>   say, wait for the turn to finish, print its result
#   ai wait  [name]          block until the agent is idle or needs input
#   ai result[name]          print the last completed turn's text (from the log)
#   ai approve[name] [1|2|3] answer a tool-permission prompt (default 2)
#   ai revive[name] [id]     relaunch a killed agent with its memory (resume its session)
#   ai attach[name]          print the command to attach another viewer
#   ai down  [name]          quit claude + kill the session
#   ai list                  list running interactive agents
set -uo pipefail

S() { echo "ai-${1:-claude}"; }                  # tmux session name
CWD="${AF_CWD:-$(pwd)}"                           # where claude runs
FLAGS="${AI_CLAUDE_FLAGS:-}"                      # extra claude flags
STATE="${AF_ROOT:-/tmp/agent-factory}/.ai"        # tracks window ids
MANIFEST="$HOME/.claude/agent-factory/manifest.tsv"  # registry of spawned agents

# Record a spawned agent so its session log can be filtered/purged later.
# Columns: epoch  tool  name  session_id  cwd
_manifest() {
  mkdir -p "$(dirname "$MANIFEST")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "ai" "$1" "$2" "$CWD" >> "$MANIFEST"
}

up() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux kill-session -t "$s" 2>/dev/null || true
  # Give the agent a known identity so its session log is filterable later:
  # --session-id <uuid> sets the log filename (<uuid>.jsonl); we record that
  # uuid in the manifest. (Note: --append-system-prompt is NOT written to the
  # transcript, so the manifest — not an in-log marker — is the durable link.)
  # If resuming, reuse the existing id instead of minting a new one.
  local id launchflags
  if [[ "$FLAGS" == *"--resume"* ]]; then
    id="$(printf '%s' "$FLAGS" | grep -oiE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
    launchflags="$FLAGS"
  else
    id="$(uuidgen | tr 'A-Z' 'a-z')"
    launchflags="--session-id $id $FLAGS"
  fi
  _manifest "$name" "$id"
  # Big virtual size so the TUI has room; start interactive claude as the cmd.
  tmux new-session -d -s "$s" -x 220 -y 50 -c "$CWD" "claude $launchflags"
  echo "[ai] interactive claude launched (session=$s id=$id cwd=$CWD)"
  # Open a visible Terminal.app window attached to it. Capture both the window
  # id AND its tty — `down` kills the tty's process to close the window cleanly
  # (AppleScript `close` pops a modal "terminate?" sheet that can't be dismissed
  # headlessly; killing the backing process closes the window with no prompt).
  mkdir -p "$STATE"
  local meta winid tty
  meta=$(osascript 2>/dev/null <<OSA
tell application "Terminal"
  activate
  set tb to do script "tmux attach -t $s"
  delay 0.2
  return (id of front window as text) & "|" & (tty of tb)
end tell
OSA
)
  winid="${meta%%|*}"; tty="${meta##*|}"
  printf '%s' "$winid" > "$STATE/win-$name"
  printf '%s' "$tty"   > "$STATE/tty-$name"
  printf '%s' "$id"    > "$STATE/sid-$name"   # for jsonl-based completion tracking
  echo "[ai] opened a Terminal.app window (id=$winid, $tty) showing the live TUI."
  echo "[ai] drive it:  ai say $name \"hello\"   |   watch screen:  ai screen $name"
}

say() {
  local name="${1:-claude}"; shift || true
  local s; s="$(S "$name")"; local msg="$*"
  [ -z "$msg" ] && { echo "[ai] usage: ai say $name <text>"; return 1; }
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name' — ai up $name"; return 1; }
  local try pending
  for try in 1 2; do
    tmux send-keys -t "$s" Escape        # close any autocomplete/file popup + clear line
    sleep 0.2
    tmux send-keys -t "$s" -l "$msg"     # literal text (no key interpretation)
    sleep 0.2
    tmux send-keys -t "$s" Enter         # submit
    sleep 0.5
    # Verify against the LIVE input box only — the last `❯` line in the capture.
    # Success = the box no longer holds OUR message. An empty box OR a greyed
    # autosuggestion (which differs from our text) both count as submitted; only
    # our exact message still sitting there means a popup ate the Enter.
    # (Submitted prompts also appear as `❯ ...` in scrollback history; the
    # tail -1 isolates the live box, ignoring those.)
    pending="$(tmux capture-pane -t "$s" -p | grep '❯' | tail -1 | sed 's/.*❯[[:space:]]*//')"
    [ "$pending" != "$msg" ] && { echo "[ai] sent to '$name': $msg"; return 0; }
    echo "[ai] input box still holds our text (popup?), retrying…"
  done
  echo "[ai] WARN: '$name' may not have submitted — check: ai screen $name"; return 1
}

keys() {
  local name="${1:-claude}"; shift || true
  tmux send-keys -t "$(S "$name")" "$@"
  echo "[ai] keys -> '$name': $*"
}

screen() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  tmux capture-pane -t "$s" -p
}

# --- State signals --------------------------------------------------------
# Completion is read from the agent's STRUCTURED LOG, not the screen: the screen
# is a viewport snapshot (scrollback-limited, racy between tool calls), whereas
# the jsonl is the authoritative append-only event stream. A finished turn lands
# an assistant record with `stop_reason: end_turn`.
#
# Permission pauses, however, leave NO trace in the jsonl (the pending tool_use
# isn't even flushed while it waits), so "needs input" can ONLY be read from the
# TUI. Hence the split below: log for done, screen for needs-input.

_log() {  # path to this agent's session jsonl, via the id we recorded at `up`
  local sid; sid="$(cat "$STATE/sid-$1" 2>/dev/null)"; [ -z "$sid" ] && return 1
  find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1
}
# Count completed turns. grep (not jq) so a half-written final line can't break
# it. Capture via $() because `grep -c` EXITS 1 on zero matches — an &&/|| chain
# would then emit a second "0" and yield the multiline "0\n0" that breaks -gt.
_endturns() {
  local f n; f="$(_log "$1")" || { echo 0; return; }
  [ -f "$f" ] || { echo 0; return; }
  n="$(grep -c '"stop_reason":"end_turn"' "$f" 2>/dev/null)"
  echo "${n:-0}"
}
# Actively generating: a live "(Ns · …)" timer on screen.
_busy()      { tmux capture-pane -t "$(S "$1")" -p | grep -qE '\([0-9]+s · '; }
# Paused on a tool-permission decision.
_permission(){ tmux capture-pane -t "$(S "$1")" -p | grep -qE 'Do you want to proceed\?|❯ 1\. Yes'; }

# Block until the in-flight turn ends, the agent pauses for input, or timeout.
# DONE requires BOTH a new end_turn in the log AND the timer gone — so a spurious
# early end_turn (e.g. a thinking block) followed by more tool calls won't fool
# it. Echoes DONE | NEEDS_INPUT | TIMEOUT.
_wait_turn() {
  local name="$1" base="$2" to="${3:-300}" i
  for ((i=0; i<to*2; i++)); do
    _permission "$name" && { echo NEEDS_INPUT; return; }
    if [ "$(_endturns "$name")" -gt "$base" ] && ! _busy "$name"; then echo DONE; return; fi
    sleep 0.5
  done
  echo TIMEOUT
}

# Print the text of the most recent completed turn, straight from the log — no
# scraping, no scrollback limit. `fromjson?` tolerates a partial trailing line.
result() {
  local f; f="$(_log "$1")" && [ -f "$f" ] || { echo "[ai] no log for '$1' (not spawned by this skill?)"; return 1; }
  jq -sRr 'split("\n") | map(fromjson? // empty)
           | map(select(.type=="assistant" and .message.stop_reason=="end_turn")
                 | [.message.content[]? | select(.type=="text") | .text] | join("\n"))
           | map(select(length>0)) | (last // "(no text in last turn)")' "$f" 2>/dev/null
}

# Block until the agent is idle/paused now (ad-hoc, when you don't have a
# baseline). Echoes DONE | NEEDS_INPUT | TIMEOUT.
wait_() {
  local name="${1:-claude}" to="${2:-300}" i settle=0
  tmux has-session -t "$(S "$name")" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  for ((i=0; i<to*2; i++)); do
    _permission "$name" && { echo NEEDS_INPUT; return; }
    if _busy "$name"; then settle=0; else settle=$((settle+1)); [ "$settle" -ge 4 ] && { echo DONE; return; }; fi
    sleep 0.5
  done
  echo TIMEOUT
}

# say, then wait for THIS turn to finish, then print its result from the log.
# Surfaces a permission pause instead of hanging or guessing "done".
ask() {
  local name="${1:-claude}"; shift || true
  local base; base="$(_endturns "$name")"
  say "$name" "$@" || return 1
  case "$(_wait_turn "$name" "$base" "${AI_TIMEOUT:-300}")" in
    DONE)        result "$name" ;;
    NEEDS_INPUT) echo "[ai] ⚠ '$name' paused on a permission prompt — approve: ai approve $name [1|2|3]"
                 tmux capture-pane -t "$(S "$name")" -p | grep -vE '^[[:space:]]*$' | tail -8 ;;
    TIMEOUT)     echo "[ai] ⚠ '$name' still working past ${AI_TIMEOUT:-300}s — check: ai result $name / ai screen $name" ;;
  esac
}

# Answer a permission prompt (default 2 = allow & don't ask again), then wait for
# the resumed turn to finish and print its result.
approve() {
  local name="${1:-claude}" choice="${2:-2}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  local base; base="$(_endturns "$name")"
  tmux send-keys -t "$s" -l "$choice"; tmux send-keys -t "$s" Enter
  sleep 1   # let the prompt dismiss before we re-check state (else we'd read the stale prompt as NEEDS_INPUT)
  case "$(_wait_turn "$name" "$base" "${AI_TIMEOUT:-300}")" in
    DONE)        result "$name" ;;
    NEEDS_INPUT) echo "[ai] ⚠ another permission prompt for '$name' — ai approve $name" ;;
    TIMEOUT)     echo "[ai] ⚠ '$name' still working — ai result $name" ;;
  esac
}

attach() { echo "tmux attach -t $(S "${1:-claude}")"; }

# Relaunch a previously-`down`ed agent WITH its full memory, by resuming its
# recorded session. `down` keeps the log; only `afctl purge` deletes it — so
# revive works any time the log still exists. Resolves the session id from (in
# order) an explicit arg, the sid state file, or the last manifest entry.
revive() {
  local name="${1:-claude}" sid="${2:-}"
  [ -z "$sid" ] && sid="$(cat "$STATE/sid-$name" 2>/dev/null)"
  [ -z "$sid" ] && sid="$(grep -P "\t$name\t" "$MANIFEST" 2>/dev/null | cut -f4 | tail -1)"
  [ -z "$sid" ] && { echo "[ai] no recorded session for '$name' — nothing to revive"; return 1; }
  [ -z "$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)" ] \
    && { echo "[ai] session $sid log is gone (purged?) — can't revive '$name'"; return 1; }
  echo "[ai] reviving '$name' from session $sid"
  FLAGS="--resume $sid $FLAGS"   # up() detects --resume and reuses this id
  up "$name"
}

down() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux kill-session -t "$s" 2>/dev/null || true
  local wf="$STATE/win-$name" tf="$STATE/tty-$name" wid tty
  [ -f "$wf" ] && wid="$(cat "$wf")"
  [ -f "$tf" ] && tty="$(cat "$tf")"
  # Preferred: kill every process on the window's tty (login shell + tmux
  # attach). The window closes with no modal prompt.
  if [ -n "${tty:-}" ]; then
    pkill -t "${tty#/dev/}" 2>/dev/null || true
    sleep 0.5
  fi
  # Fallback: if the window id still exists, ask Terminal to close it.
  if [ -n "${wid:-}" ]; then
    local n; n="$(osascript -e "tell application \"Terminal\" to count (every window whose id is $wid)" 2>/dev/null)"
    [ "$n" != "0" ] && osascript >/dev/null 2>&1 -e "tell application \"Terminal\" to close (every window whose id is $wid) saving no"
  fi
  rm -f "$wf" "$tf"
  echo "[ai] '$name' down — session killed, window closed."
}

list() { tmux ls 2>/dev/null | grep '^ai-' || echo "[ai] none"; }

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up "$@" ;;  say) say "$@" ;;  keys) keys "$@" ;;
  screen) screen "$@" ;;  ask) ask "$@" ;;  approve) approve "$@" ;;  attach) attach "$@" ;;
  wait) wait_ "$@" ;;  result) result "$@" ;;  revive) revive "$@" ;;
  down) down "$@" ;;  list) list ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
