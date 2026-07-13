#!/usr/bin/env bash
# notify — thin alias for `mail.sh send`. Kept ONLY as a stable entry point.
#
# This used to be the transport: it appended to a shared TSV inbox and typed the
# whole message into the recipient's TUI. mail.sh replaced it (payload in a file,
# doorbell in the pane, cursor as ack — see mail.sh for why that is the reliable
# design). All of that logic is GONE from here; this file now just forwards.
#
# Why keep the file at all: agents spawned by earlier versions have
# `bash $AF_NOTIFY …` baked into their system prompt, and their resumed context is
# full of past calls to it. Deleting the file would make their escalations fail
# with "No such file" — silently, exactly when they need to be heard. So the entry
# point stays and routes to the real channel; the duplicated transport does not.
#
#   notify.sh "<what I need>"          # → orchestrator, kind "blocked"
#   notify.sh --to <agent> "<text>"    # → a peer agent
#   notify.sh --kind question "<text>" # kind: question | blocked | result | done | fyi
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
to="orchestrator"
kind="blocked"
while [ "${1:-}" ] ; do
  case "$1" in
    --to)   to="${2:-orchestrator}"; shift 2 || break ;;
    --kind) kind="${2:-blocked}";    shift 2 || break ;;
    *) break ;;
  esac
done
[ -z "$*" ] && { echo "usage: notify.sh [--to <agent>] [--kind K] <message>"; exit 1; }

exec bash "$HERE/mail.sh" send --to "$to" --kind "$kind" --from "${AF_AGENT:-unknown}" "$*"
