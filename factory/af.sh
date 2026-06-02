#!/usr/bin/env bash
# af — Agent Factory controller.
# Lets THIS interactive claude (via the Bash tool) spawn a second, *visible*
# claude agent in its own shell window and then talk to it freely.
#
#   af up   [name]         spawn a visible worker agent (new window/pane)
#   af say  [name] <text>  send a task, block, print the reply  <-- I call this
#   af ask  [name] <text>  alias for `say`
#   af log  [name]         show the worker's transcript so far
#   af down [name]         stop the worker, clean up FIFOs
#   af list                list running workers
#
# Visibility (AF_VIEW): macos (new Terminal.app window, default on darwin) |
#                       tmux (detached session you `tmux attach` to) |
#                       iterm.
set -uo pipefail

ROOT="${AF_ROOT:-/tmp/agent-factory}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER="$HERE/worker.sh"
MANIFEST="$HOME/.claude/agent-factory/manifest.tsv"  # registry of spawned agents

_dir()  { echo "$ROOT/$1"; }
# Pick a viewer. Terminal.app + iTerm have AppleScript `do script` (no special
# perms). Warp does NOT, and keystroke injection is blocked by Accessibility —
# so Warp (and Linux, SSH, anything else) falls back to tmux: portable, works
# everywhere. Override with AF_VIEW=macos|iterm|tmux.
_view() {
  if [ -n "${AF_VIEW:-}" ]; then echo "$AF_VIEW"; return; fi
  case "${TERM_PROGRAM:-}" in
    Apple_Terminal) echo macos ;;
    iTerm.app)      echo iterm ;;
    *)              echo tmux ;;   # Warp, Linux, ssh, tmux-in-anything
  esac
}
b64enc() { base64 | tr -d '\n'; }

up() {
  local name="${1:-worker}" dir; dir="$(_dir "$name")"
  mkdir -p "$dir"
  [ -p "$dir/a2b" ] || mkfifo "$dir/a2b"
  [ -p "$dir/b2a" ] || mkfifo "$dir/b2a"
  : > "$dir/transcript.log"
  # Assign + record a known session id so the agent's log is filterable later.
  local id; id="$(uuidgen | tr 'A-Z' 'a-z')"
  mkdir -p "$(dirname "$MANIFEST")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "af" "$name" "$id" "$(pwd)" >> "$MANIFEST"
  local cmd="AF_DIR='$dir' AF_NAME='$name' AF_SID='$id' bash '$WORKER' '$name' | tee -a '$dir/transcript.log'"

  case "$(_view)" in
    macos)
      osascript >/dev/null 2>&1 <<OSA
tell application "Terminal"
  activate
  do script "clear; echo '== agent: $name =='; $cmd"
end tell
OSA
      echo "[af] spawned '$name' in a new Terminal window (visible)." ;;
    iterm)
      osascript >/dev/null 2>&1 <<OSA
tell application "iTerm"
  create window with default profile
  tell current session of current window to write text "clear; echo '== agent: $name =='; $cmd"
end tell
OSA
      echo "[af] spawned '$name' in a new iTerm window." ;;
    tmux|*)
      tmux kill-session -t "af-$name" 2>/dev/null || true
      tmux new-session -d -s "af-$name" -n "$name" "$cmd"
      echo "[af] spawned '$name' in a detached tmux session (portable — Warp/any terminal)."
      echo "[af] SEE IT: open a Warp pane (Cmd+D / Cmd+T) and run:"
      echo "             tmux attach -t af-$name" ;;
  esac
  echo "[af] dir=$dir  — now drive it with:  af say $name \"...\""
}

say() {
  local name="${1:-worker}"; shift || true
  local msg="$*" dir; dir="$(_dir "$name")"
  [ -p "$dir/a2b" ] || { echo "[af] no worker '$name' — run: af up $name"; return 1; }
  [ -z "$msg" ] && { echo "[af] usage: af say $name <text>"; return 1; }
  exec 4<>"$dir/b2a"
  printf '%s\n' "$(printf '%s' "$msg" | b64enc)" > "$dir/a2b"
  local enc; IFS= read -r enc <&4
  printf '%s' "$enc" | base64 -d
  echo
}

logs() {
  local name="${1:-worker}" dir; dir="$(_dir "$name")"
  [ -f "$dir/transcript.log" ] && cat "$dir/transcript.log" || echo "[af] no transcript for '$name'"
}

down() {
  local name="${1:-worker}" dir; dir="$(_dir "$name")"
  tmux kill-session -t "af-$name" 2>/dev/null || true
  pkill -f "worker.sh $name" 2>/dev/null || true
  rm -rf "$dir"
  echo "[af] '$name' down."
}

list() {
  [ -d "$ROOT" ] || { echo "[af] none"; return; }
  for d in "$ROOT"/*/; do [ -d "$d" ] && echo "  $(basename "$d")"; done
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up)   up   "$@" ;;
  say|ask) say "$@" ;;
  log|logs) logs "$@" ;;
  down) down "$@" ;;
  list) list ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
