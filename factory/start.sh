#!/usr/bin/env bash
# Launch the visible demo: a tmux session with two panes —
#   left  = worker agent (real headless claude)
#   right = orchestrator REPL
# They talk over two FIFOs. You watch both shells live.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${AF_DIR:-/tmp/agent-factory}"
SESSION="${AF_SESSION:-agent-factory}"

mkdir -p "$DIR"
[ -p "$DIR/a2b" ] || mkfifo "$DIR/a2b"
[ -p "$DIR/b2a" ] || mkfifo "$DIR/b2a"

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n floor

# Left pane: worker
tmux send-keys -t "$SESSION" "AF_DIR='$DIR' bash '$HERE/worker.sh' worker" Enter

# Right pane: orchestrator
tmux split-window -h -t "$SESSION"
tmux send-keys -t "$SESSION" "AF_DIR='$DIR' bash '$HERE/orchestrator.sh' orchestrator" Enter

# Focus orchestrator so you can type immediately
tmux select-pane -t "$SESSION" -R
tmux attach -t "$SESSION"
