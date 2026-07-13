#!/usr/bin/env bash
# limit-hook — Claude Code `StopFailure` hook, matcher `rate_limit`.
#
# Fires at the exact moment a turn is killed by the subscription usage limit. It is
# INFORMATIONAL ONLY: Claude Code ignores its exit code and its output, so it cannot
# block, cannot retry, cannot save the turn. All it can do is leave a note — and that is
# all we need, because the party that acts on the note is not a Claude at all.
#
# THE THING THAT MAKES THIS AWKWARD: the limit is ACCOUNT-WIDE. When it lands it kills
# every agent on the machine AND the orchestrator session driving them. There is nobody
# left with tokens to notice, so the only possible rescuer is a plain shell process that
# spends none: limits.sh. This hook is how it learns which agents were cut off mid-turn
# (as opposed to merely idle), and the statusline is how it learns when the limit lifts.
#
# The marker holds the agent's SID. An agent respawned fresh under the same name is a
# different agent, and must not inherit a "you were interrupted, carry on" message meant
# for its predecessor — limits.sh checks it.
set -uo pipefail

payload="$(cat 2>/dev/null)"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
SLUG="${AF_SLUG:-proj}"
SELF="${AF_AGENT:-orchestrator}"
STATE="$ROOT/.ai/$SLUG"
mkdir -p "$STATE" 2>/dev/null

# The matcher should already have narrowed this to rate_limit, but a hook that assumes its
# matcher is a promise is a hook that one day writes "limited" because the disk was full.
et="$(printf '%s' "$payload" | python3 -c 'import sys,json
try: print((json.load(sys.stdin) or {}).get("error_type") or "")
except Exception: print("")' 2>/dev/null)"
case "$et" in
  rate_limit|"") ;;                     # empty: some versions may not send it — trust the matcher
  *) exit 0 ;;                          # some other failure: not ours
esac

sid="$(cat "$STATE/sid-$SELF" 2>/dev/null)"
printf '%s\t%s\t%s\n' "$(date +%s)" "$sid" "$(printf '%s' "$payload" | tr '\n' ' ' | cut -c1-400)" \
  > "$STATE/limited-$SELF"
exit 0
