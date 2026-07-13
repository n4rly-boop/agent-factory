#!/usr/bin/env bash
# statusline — Claude Code pipes a JSON blob to this on every render. We are not here
# for the pretty line; we are here for ONE field.
#
#   "rate_limits": { "five_hour":{"used_percentage":16,"resets_at":1783993800},
#                    "seven_day":{"used_percentage":48,"resets_at":1784034000} }
#
# resets_at is the exact unix epoch at which the subscription limit lifts. That number is
# the whole reason limits.sh can wait instead of guess. It is only available INSIDE a live
# session (there is no CLI that will tell you from outside), so every agent drops it on
# disk here, and the external watcher — which spends no tokens and therefore survives the
# limit — reads it from there.
#
# Written by whichever agent rendered last; the limit is account-wide, so one file is the
# truth for the whole machine. Cheap: a few writes per turn, no network, no model.
set -uo pipefail

IN="$(cat)"
ROOT="${AF_ROOT:-/tmp/agent-factory}"
SLUG="${AF_SLUG:-proj}"
STATE="$ROOT/.ai/$SLUG"
mkdir -p "$STATE" 2>/dev/null
export AF_STATE="$STATE"      # the python below reads it from the env, not from argv

# The status line must still print something, whatever happens below — an empty status
# line is a broken-looking TUI, and a crash here would show up as a mysterious blank bar
# rather than as an error anyone can trace.
line="$(printf '%s' "$IN" | python3 -c '
import sys, json, os, time
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit

rl = d.get("rate_limits") or {}
state = os.environ.get("AF_STATE") or ""
if rl and state:
    try:
        # Atomic-ish: write then rename, so the watcher never reads a half-written file.
        tmp = os.path.join(state, ".limits.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"rate_limits": rl, "seen": int(time.time())}, f)
        os.replace(tmp, os.path.join(state, "limits.json"))
    except Exception:
        pass

agent = os.environ.get("AF_AGENT") or "agent"
role  = os.environ.get("AF_ROLE") or ""
model = ((d.get("model") or {}).get("display_name")) or ""
ctx   = (d.get("context_window") or {})
used  = ctx.get("used_tokens") or ctx.get("used") or 0
fh    = (rl.get("five_hour") or {})
pct   = fh.get("used_percentage")
resets= fh.get("resets_at")

bits = [f"{agent}" + (f" ({role})" if role else "")]
if model: bits.append(model)
if used:  bits.append(f"{int(used)//1000}k")
if pct is not None:
    s = f"5h {int(pct)}%"
    if resets:
        left = max(0, int(resets) - int(time.time()))
        s += f" · {left//3600}h{(left%3600)//60:02d}m left"
    bits.append(s)
print(" | ".join(bits))
' 2>/dev/null)"

printf '%s' "${line:-agent}"
