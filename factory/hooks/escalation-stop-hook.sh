#!/usr/bin/env bash
# escalation-stop-hook — Claude Code `Stop` hook for the agent-factory.
#
# Problem: a fully-stopped Claude session can't be woken by an external event.
# Trick: this runs when the ORCHESTRATOR tries to stop. If a spawned agent has
# mailed it, the hook returns {"decision":"block","reason":<the mail>} — the
# session auto-continues and handles it, with NO human input. If nothing is
# waiting, it exits 0 and the session stops for real.
#
# READS ONLY THIS PROJECT'S MAILBOX. The old version polled one global inbox
# shared by every project on the machine, so a session in repo A would be woken
# with — and told to answer — escalations belonging to repo B's agents. (That
# actually happened.) Mailboxes are per-slug: $AF_ROOT/.ai/<slug>/mail/.
#
# The mailbox CURSOR gives exactly-once delivery: `mail read` advances it as it
# hands the message over, so re-firing after a block (stop_hook_active) cannot
# loop on the same message.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIL="$HERE/../mail.sh"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
CWD="${AF_CWD:-$(pwd)}"
# Same slug derivation as ai.sh — the same cwd must yield the same mailbox, or
# the hook would watch a box nobody writes to.
SLUG="${AF_SLUG:-$(basename "$CWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g' | cut -c1-12)}"
[ -z "$SLUG" ] && SLUG="proj"
SELF="${AF_AGENT:-orchestrator}"          # unset in an orchestrator session
AWAIT="$ROOT/.ai/$SLUG/await"             # arm flag: poll ONLY while awaiting async work
POLL="${AF_STOP_POLL:-45}"                # seconds to hold the turn open for new mail
POLL_ACTIVE="${AF_STOP_POLL_ACTIVE:-3}"   # shorter re-check after we already delivered

payload="$(cat 2>/dev/null)"              # Stop hooks get JSON on stdin
active=0
printf '%s' "$payload" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true' && active=1

_m()      { env AF_AGENT="$SELF" AF_SLUG="$SLUG" AF_ROOT="$ROOT" bash "$MAIL" "$@"; }
_unread() { local n; n="$(_m unread --agent "$SELF" 2>/dev/null)"; echo "${n:-0}"; }

# Gate: hold the session open ONLY when the orchestrator explicitly armed the wait
# (it delegated async work and touched $AWAIT). Otherwise every idle turn would
# hang for POLL seconds. Mail already waiting is delivered regardless — the gate
# only decides whether we POLL for more.
_outstanding() { [ -f "$AWAIT" ]; }

# `mail read` prints the unread mail AND advances the cursor, so what we deliver
# here is exactly what the agent never saw, exactly once.
_deliver() {
  local body reason
  body="$(_m read --agent "$SELF" 2>/dev/null)"
  [ -z "$body" ] && exit 0
  reason="⚡ A spawned agent mailed you while you were idle:

${body}

Handle it now: reply by mail (ai post <agent> --kind result \"…\"), or drive them with ai say/ask. Then you may stop."
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg r "$reason" '{decision:"block", reason:$r}'
  else
    printf '{"decision":"block","reason":%s}\n' \
      "\"$(printf '%s' "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n",$0}')\""
  fi
  exit 0
}

[ -f "$MAIL" ] || exit 0
[ "$(_unread)" -gt 0 ] && _deliver

# Nothing waiting. Don't hold the session hostage unless work is genuinely out.
_outstanding || exit 0
[ "$active" = 1 ] && POLL="$POLL_ACTIVE"

for ((i=0; i<POLL; i++)); do
  sleep 1
  [ "$(_unread)" -gt 0 ] && _deliver
  _outstanding || exit 0        # work finished while we waited → let it stop
done
exit 0   # poll window closed with nothing new → allow the stop
