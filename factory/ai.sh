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
#   ai ask   [name] <text>   say + wait for idle + print what changed
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
    # (Submitted prompts also appear as `❯ ...` in scrollback history; ignore those.)
    pending="$(tmux capture-pane -t "$s" -p | grep '❯' | tail -1 | sed 's/.*❯[[:space:]]*//')"
    [ -z "$pending" ] && { echo "[ai] sent to '$name': $msg"; return 0; }
    echo "[ai] input box still holds text (popup?), retrying…"
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

# Busy = claude shows a live status line with a running timer, e.g.
#   "✻ Computing… (4s · ↓ 160 tokens · thought for 3s)".
# Done = that line is gone (replaced by a static past-tense "✻ Cooked for 13s")
# and the input box is empty. This is a real state marker — far better than
# diffing the whole screen.
_busy() { tmux capture-pane -t "$(S "$1")" -p | grep -qE '\([0-9]+s · '; }

# say, then wait for the generation timer to appear and then disappear, then
# print the final screen. Bounded.
ask() {
  local name="${1:-claude}"; shift || true
  say "$name" "$@" || return 1
  local i
  for i in $(seq 1 16); do _busy "$name" && break; sleep 0.5; done   # wait for it to start (fast answers may skip)
  for i in $(seq 1 360); do _busy "$name" || break; sleep 0.5; done   # wait for it to finish (~180s cap)
  sleep 0.3
  echo "----- screen of '$name' -----"
  tmux capture-pane -t "$(S "$name")" -p
}

attach() { echo "tmux attach -t $(S "${1:-claude}")"; }

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
  screen) screen "$@" ;;  ask) ask "$@" ;;  attach) attach "$@" ;;
  down) down "$@" ;;  list) list ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
