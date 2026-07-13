#!/usr/bin/env bash
# af — Agent Factory controller.
# Lets THIS interactive claude (via the Bash tool) spawn a second claude agent in
# its own shell and then talk to it freely.
#
#   af up   [name]         spawn a worker agent (detached tmux session)
#   af say  [name] <text>  send a task, block, print the reply  <-- I call this
#   af ask  [name] <text>  alias for `say`
#   af log  [name]         show the worker's transcript so far
#   af down [name]         stop the worker, clean up FIFOs
#   af list                list running workers
#
# The worker lives in a detached tmux session (af-<name>) and NOTHING pops up. A
# human who wants to watch runs `tmux attach -t af-<name>`. Spawning a Terminal.app
# window used to be the default on macOS; it is gone. It only ever worked on
# Terminal.app and iTerm (AppleScript `do script`) — never in Warp, over ssh, or on
# Linux — so it was a second, less portable code path for the same job, and the one
# that needed macOS Automation permission to not silently fail.
set -uo pipefail

ROOT="${AF_ROOT:-/tmp/agent-factory}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER="$HERE/worker.sh"
MANIFEST="$HOME/.claude/agent-factory/manifest.tsv"  # registry of spawned agents

_dir()  { echo "$ROOT/$1"; }
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

  # AF_VIEW used to pick between a Terminal.app window, an iTerm window and tmux.
  # tmux is now the only one. Say so rather than quietly ignoring a value someone
  # has exported in their shell profile and still believes in.
  [ -n "${AF_VIEW:-}" ] && [ "${AF_VIEW}" != tmux ] && \
    echo "[af] note: AF_VIEW=$AF_VIEW is ignored — workers are tmux-only now." >&2

  tmux kill-session -t "af-$name" 2>/dev/null || true
  tmux new-session -d -s "af-$name" -n "$name" "$cmd"
  echo "[af] spawned '$name' in a detached tmux session (no window pops up)."
  echo "[af] SEE IT: open a pane in your terminal and run:  tmux attach -t af-$name"
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
  # Help = the header comment, printed up to the first non-comment line. Derived,
  # not a hardcoded line range: the old `sed -n 2,20p` outlived the header it was
  # measured against and printed `set -uo pipefail` as if it were help text.
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;
esac
