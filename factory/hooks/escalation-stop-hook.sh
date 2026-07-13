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
#
# IT NEVER WAITS. It used to hold the turn open for 45s whenever an `await` flag said
# async work was outstanding, hoping the reply would land inside the window. That was
# a bad trade: the agents are working and their answer arrives as mail, which WAKES the
# orchestrator whenever it lands — so the wait bought nothing, while every stale flag
# (a crashed agent, a task queued for an agent that never came up, a sweep killed
# mid-run) cost a 45-second stall on EVERY idle turn thereafter. Deliver what has
# arrived; never sit and hope.
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

cat >/dev/null 2>&1                       # drain the Stop payload; we no longer read it

_m()      { env AF_AGENT="$SELF" AF_SLUG="$SLUG" AF_ROOT="$ROOT" bash "$MAIL" "$@"; }
_unread() { local n; n="$(_m unread --agent "$SELF" 2>/dev/null)"; echo "${n:-0}"; }

# `mail read` prints the unread mail AND advances the cursor, so what we deliver
# here is exactly what the agent never saw, exactly once.
_deliver() {
  local body reason rc=0
  body="$(_m read --agent "$SELF" 2>/dev/null)" || rc=$?
  [ -z "$body" ] && exit 0
  # `mail read` FAILED — the box is locked by the other reader (the doorbell the agent
  # itself just ran). Its cursor did not move, so nothing was consumed: let the session
  # stop and pick the message up next time. Blocking here would re-block on every Stop
  # until the lock cleared, and would hand the model an error string as if it were the
  # escalation.
  [ "$rc" -ne 0 ] && exit 0
  # Same for mail.sh's own status lines ("no new mail…"): the doorbell reader can win
  # the race between our unread count and our read, and a status line is not mail.
  case "$body" in "[mail]"*) exit 0 ;; esac
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
exit 0   # nothing waiting → stop for real, immediately. No polling, no hostage-taking.
