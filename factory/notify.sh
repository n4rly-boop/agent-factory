#!/usr/bin/env bash
# notify — a SPAWNED agent calls this to escalate a blocker to its orchestrator
# (the Claude session that launched it via agent-factory). It's the agent→orch
# back-channel: when a spawned agent gets stuck on something it can't resolve
# alone, instead of stalling silently it posts a one-line message to a shared
# inbox that the orchestrator reads with `ai inbox`.
#
#   notify.sh "<what I need>"                 # kind defaults to "blocked"
#   notify.sh --kind question "<what I need>" # e.g. question | blocked | done | fyi
#
# Identity + inbox path are injected into the agent's environment at spawn time
# (AF_AGENT, AF_INBOX), so the agent doesn't need to know its own name or where
# the inbox lives — it just runs this. Each line is: epoch \t name \t kind \t msg.
set -uo pipefail

name="${AF_AGENT:-unknown}"
inbox="${AF_INBOX:-${AF_ROOT:-/tmp/agent-factory}/inbox.tsv}"
kind="blocked"
if [ "${1:-}" = "--kind" ]; then kind="${2:-blocked}"; shift 2 || true; fi
msg="$*"
[ -z "$msg" ] && { echo "usage: notify.sh [--kind K] <message>"; exit 1; }

# One physical line per notification (tabs/newlines in msg would corrupt the TSV).
msg="$(printf '%s' "$msg" | tr '\t\n' '  ')"
mkdir -p "$(dirname "$inbox")"
printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$name" "$kind" "$msg" >> "$inbox"
echo "[notify] escalated to orchestrator as '$name' ($kind): $msg"
