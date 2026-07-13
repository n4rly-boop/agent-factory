#!/usr/bin/env bash
# line — bring up a whole production line of agents from one blueprint.
#
# The problem it solves: a fleet's design (who exists, who reports to whom, who
# may only delegate, who gets the cheap model) is the part that is easiest to get
# wrong and hardest to remember. Written as a prompt, it decays: you spawn five
# agents, tell each its role once, and thirty turns later nobody remembers they
# were supposed to delegate. Written as a blueprint, it is configuration — applied
# identically to every agent, every time, and enforced by hooks rather than hoped
# for.
#
#   line up     <blueprint.yml>   generate briefs + settings, spawn every station
#   line status <blueprint.yml>   who's alive, context size, unread mail
#   line down   <blueprint.yml>   stop every station on the line
#   line plan   <blueprint.yml>   print the resolved blueprint without spawning
#
# BLUEPRINT
#   slug: rlhf-exp                 # namespaces tmux sessions + mailboxes
#   work: ./work                   # every agent's report lands here
#   defaults:
#     model: sonnet
#     caveman: true                # enforce terse output (hook, not a request)
#     delegate: required           # mini-orchestrator: may not edit code itself
#   agents:
#     orc:
#       role: orchestrator         # the one YOU talk to
#       model: opus
#       delegate: no               # the top orchestrator may act directly
#       brief: |
#         Own the experiment end to end...
#     eval:
#       role: evaluation
#       parent: orc
#       brief: |
#         Write eval scripts, compute metrics...
#     abl:
#       count: 3                   # → abl1, abl2, abl3, same brief
#       role: ablation
#       parent: orc
#       model: haiku
#       brief: |
#         Test exactly ONE hypothesis...
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI="$HERE/ai.sh"
PY="${PYTHON:-python3}"

# Flatten the blueprint into one TSV row per agent, so the shell never has to
# parse YAML. `count:` is expanded here (abl → abl1 abl2 abl3) and defaults are
# resolved here, which means `line plan` shows exactly what `line up` will do.
_plan() {
  local bp="$1"
  "$PY" - "$bp" <<'PY'
import sys, yaml, os
bp = yaml.safe_load(open(sys.argv[1])) or {}
slug = bp.get('slug') or os.path.basename(os.getcwd())
work = bp.get('work') or './work'
d    = bp.get('defaults') or {}
agents = bp.get('agents') or {}

def flag(v, default=False):
    if v is None: return default
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ('1','true','yes','required','full','on')

rows, names = [], []
for name, cfg in agents.items():
    cfg = cfg or {}
    n = int(cfg.get('count') or 1)
    for i in range(n):
        nm = f"{name}{i+1}" if cfg.get('count') else name
        names.append((nm, cfg))

# Peers = everyone else on the line. Every agent may mail every agent; the
# blueprint only fixes who they REPORT to, not who they may talk to.
allnames = [n for n, _ in names]
for nm, cfg in names:
    role     = cfg.get('role') or 'worker'
    parent   = cfg.get('parent') or ('' if role == 'orchestrator' else 'orc')
    model    = cfg.get('model') or d.get('model') or ''
    delegate = 'required' if flag(cfg.get('delegate'), flag(d.get('delegate'))) else ''
    caveman  = '1' if flag(cfg.get('caveman'), flag(d.get('caveman'))) else ''
    soft     = str(cfg.get('compact_soft') or d.get('compact_soft') or '')
    hard     = str(cfg.get('compact_hard') or d.get('compact_hard') or '')
    brief    = (cfg.get('brief') or '').strip()
    peers    = ','.join(p for p in allnames if p != nm)
    # \x1f (unit separator), not tab: tab is an IFS *whitespace* character, so
    # bash collapses runs of them and an empty field (say, an orchestrator with no
    # parent) silently vanishes — shifting every column after it.
    print('\x1f'.join([slug, work, nm, role, parent, model, delegate, caveman, soft, hard, peers,
                     brief.replace('\t', '  ').replace('\n', '\\n')]))
PY
}

# A settings file per agent: same two hooks for everyone, but they read the
# agent's env, so one file shape covers every role. Written per-agent anyway
# because the agents share a cwd — a project-level .claude/settings.json could
# not give them different rules.
_settings() {
  local slug="$1" name="$2" out="$3"
  mkdir -p "$(dirname "$out")"
  cat > "$out" <<JSON
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "$HERE/hooks/role-reminder.sh", "timeout": 5 } ] }
    ],
    "PreToolUse": [
      { "matcher": "Write|Edit|NotebookEdit",
        "hooks": [ { "type": "command", "command": "$HERE/hooks/delegate-wall.sh", "timeout": 5 } ] }
    ]
  }
}
JSON
}

_entrypoint() {
  local work="$1" name="$2" role="$3" parent="$4" peers="$5" delegate="$6" brief="$7"
  mkdir -p "$work"
  local f="$work/entrypoint-$name.md"
  {
    printf '# %s — %s\n\n' "$name" "$role"
    printf '## Who you are\n\nYou are `%s`, the **%s** station on this line.\n\n' "$name" "$role"
    [ -n "$parent" ] && printf 'You report to `%s`. Send it your results; escalate blockers to it.\n\n' "$parent"
    printf '## Who you can reach\n\nPeers: %s\n\n```bash\nbash $AF_MAIL send --to <agent> --kind <question|blocked|result|done|fyi> "..."\nbash $AF_MAIL read      # your inbox (mail is also pushed to you automatically)\n```\n\n' "${peers:-none}"
    if [ "$delegate" = "required" ]; then
      printf '## How you work — you are a MINI-ORCHESTRATOR\n\n'
      printf 'You do **not** do the work yourself. You dispatch it and verify what comes back:\n\n'
      printf '1. `delegate-to-local-model` skill — preferred. Free, and keeps the work off your context.\n'
      printf '2. A subagent via the Task tool — for anything needing judgment.\n\n'
      printf 'This is enforced, not advised: a hook blocks your Write/Edit outside `%s/`.\n' "$work"
      printf 'Verify everything that comes back. Never trust bulk output unread.\n\n'
    fi
    printf '## Your report\n\nWrite it to `%s/%s.md`. One file, kept current — it is how the line sees your work.\n\n' "$work" "$name"
    printf '## Your brief\n\n%b\n' "$brief"
  } > "$f"
  echo "$f"
}

# A hook that cannot execute FAILS OPEN: Claude Code reports "hook error … status
# code" and runs the tool anyway. So a delegate-wall without its +x bit is not a
# wall at all — it is a wall-shaped hole, and nothing in the agent's output says
# so. (Observed: an agent sailed straight through a chmod-less wall and wrote the
# file it was supposed to be denied.) Refuse to bring the line up in that state
# rather than hand out enforcement that silently isn't there.
_preflight() {
  local h bad=0
  for h in "$HERE/hooks/role-reminder.sh" "$HERE/hooks/delegate-wall.sh"; do
    [ -f "$h" ] || { echo "[line] FATAL: missing hook $h"; bad=1; continue; }
    [ -x "$h" ] || chmod +x "$h" 2>/dev/null
    [ -x "$h" ] || { echo "[line] FATAL: hook not executable and chmod failed: $h"; bad=1; }
  done
  [ "$bad" = 0 ] || { echo "[line] refusing to spawn — enforcement hooks would fail open."; return 1; }
}

up() {
  local bp="${1:?usage: line up <blueprint.yml>}"
  _preflight || return 1
  local slug work name role parent model delegate caveman soft hard peers brief ep n=0
  while IFS=$'\x1f' read -r slug work name role parent model delegate caveman soft hard peers brief; do
    [ -z "$name" ] && continue
    ep="$(_entrypoint "$work" "$name" "$role" "$parent" "$peers" "$delegate" "$brief")"
    local st="${AF_ROOT:-/tmp/agent-factory}/.ai/$slug/settings-$name.json"
    _settings "$slug" "$name" "$st"

    # The invariant goes in the system prompt (it survives compaction); the full
    # brief goes in the entrypoint file (too long to repeat, and re-readable).
    local sys="You are '$name', the $role station on the '$slug' line."
    [ -n "$parent" ] && sys="$sys You report to '$parent'."
    sys="$sys Read $ep NOW — it is your brief, your chain of command and your working rules — then follow it."
    [ "$delegate" = "required" ] && sys="$sys You are a mini-orchestrator: you do NOT do work directly, you delegate it (delegate-to-local-model skill, or a Task subagent) and verify the result. A hook enforces this."
    [ "$caveman" = "1" ] && sys="$sys Answer tersely — drop articles, filler and hedging; keep every technical fact exact."

    AF_SLUG="$slug" AF_ROLE="$role" AF_PARENT="$parent" AF_PEERS="$peers" \
    AF_DELEGATE="$delegate" AF_CAVEMAN="$caveman" AF_WORK="$work" \
    AI_COMPACT_SOFT="${soft:-${AI_COMPACT_SOFT:-200000}}" \
    AI_COMPACT_HARD="${hard:-${AI_COMPACT_HARD:-500000}}" \
    AI_CLAUDE_FLAGS="--settings $st ${model:+--model $model} --append-system-prompt $(printf '%q' "$sys") ${AI_CLAUDE_FLAGS:-}" \
    AI_NOTIFY_OFF=1 \
      bash "$AI" up "$name" >/dev/null 2>&1
    printf '[line] %-10s %-14s %-8s %s\n' "$name" "$role" "${model:-default}" \
      "${parent:+← $parent}${delegate:+  [delegate-only]}"
    n=$((n+1))
  done < <(_plan "$bp")
  echo "[line] $n stations up. attach: tmux attach -t ai-$(_plan "$bp" | head -1 | cut -d$'\x1f' -f1)-<name>"
  echo "[line] talk to the line:  ai post <agent> \"…\"   |   read replies:  ai mail"
}

status() {
  local bp="${1:?usage: line status <blueprint.yml>}" slug work name rest
  while IFS=$'\x1f' read -r slug work name rest; do
    [ -z "$name" ] && continue
    local s="ai-$slug-$name" alive="down" ctx="-" un="-"
    tmux has-session -t "$s" 2>/dev/null && alive="up"
    if [ "$alive" = up ]; then
      ctx="$(AF_SLUG="$slug" bash "$AI" ctx "$name" 2>/dev/null | grep -oE '[0-9]+' | tail -1)"
    fi
    un="$(AF_SLUG="$slug" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$HERE/mail.sh" unread --agent "$name" 2>/dev/null)"
    printf '  %-10s %-5s ctx=%-9s unread=%s\n' "$name" "$alive" "${ctx:-0}" "${un:-0}"
  done < <(_plan "$bp")
}

down() {
  local bp="${1:?usage: line down <blueprint.yml>}" slug work name rest
  while IFS=$'\x1f' read -r slug work name rest; do
    [ -z "$name" ] && continue
    AF_SLUG="$slug" bash "$AI" down "$name" >/dev/null 2>&1
    echo "[line] $name down"
  done < <(_plan "$bp")
}

plan() {
  local bp="${1:?usage: line plan <blueprint.yml>}"
  printf '%-10s %-14s %-8s %-8s %-9s %s\n' NAME ROLE MODEL PARENT DELEGATE PEERS
  local slug work name role parent model delegate caveman soft hard peers brief
  while IFS=$'\x1f' read -r slug work name role parent model delegate caveman soft hard peers brief; do
    [ -z "$name" ] && continue
    printf '%-10s %-14s %-8s %-8s %-9s %s\n' "$name" "$role" "${model:-default}" "${parent:--}" "${delegate:--}" "$peers"
  done < <(_plan "$bp")
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up "$@" ;;  down) down "$@" ;;  status) status "$@" ;;  plan) plan "$@" ;;
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;
esac
