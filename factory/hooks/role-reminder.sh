#!/usr/bin/env bash
# role-reminder — UserPromptSubmit hook. Prints the agent's identity, chain of
# command and standing orders; the output is prepended to the model's context for
# that turn.
#
# WHY A HOOK AND NOT AN INSTRUCTION. A brief in a file gets forgotten. A rule
# stated once in the system prompt survives compaction but competes with 200k
# tokens of recent work for attention — after thirty turns of debugging, "delegate
# instead of writing code yourself" is not what the model is thinking about. A
# hook re-states it on EVERY prompt, at the position models weight most (closest
# to the task), for ~25 tokens. It cannot drift.
#
# Everything comes from the env injected at spawn, so this file is generic: one
# hook, any role.
set -uo pipefail
cat >/dev/null 2>&1   # drain stdin (hook payload); we don't need it

role="${AF_ROLE:-}"
[ -z "$role" ] && exit 0          # not a role-managed agent → say nothing

agent="${AF_AGENT:-unknown}"
parent="${AF_PARENT:-orchestrator}"
peers="${AF_PEERS:-}"
work="${AF_WORK:-work}"

printf 'ROLE: you are %s (%s). Report to: %s.' "$agent" "$role" "$parent"
[ -n "$peers" ] && printf ' Peers you may mail: %s.' "$peers"
printf ' Mail: bash $AF_MAIL send --to <agent> --kind <question|blocked|result|done|fyi> "…".'

# The two rules that decay fastest under load, so they are the two we repeat.
case "${AF_DELEGATE:-}" in
  required)
    printf ' You are a MINI-ORCHESTRATOR: do not do the work yourself — dispatch it via the delegate-to-local-model skill (the only route that can WRITE; a Task subagent inherits your wall and cannot), or mail the peer who owns the area. Then verify the result. Your own writes are confined to %s/.' "$work" ;;
  advised)
    # Both halves, always. "Delegate" on its own is how you get an agent farming out a
    # two-line fix to an external model — the rule has to carry its own boundary.
    printf ' You are a MINI-ORCHESTRATOR: delegate BULK/mechanical work (many items, boilerplate, spec-code, first drafts, big logs) via delegate-to-local-model, or mail the peer who owns it, then verify. Small surgical edits: just make them yourself.' ;;
esac
[ "${AF_CAVEMAN:-}" = "1" ] && printf ' Answer in caveman: drop articles/filler/hedging, keep every technical fact exact.'
printf '\n'
