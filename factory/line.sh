#!/usr/bin/env bash
# line — bring up a whole production line of agents from one blueprint.
#
# The problem it solves: a line's design (who exists, who reports to whom, who
# may only delegate, who gets the cheap model) is the part that is easiest to get
# wrong and hardest to remember. Written as a prompt, it decays: you spawn five
# agents, tell each its role once, and thirty turns later nobody remembers they
# were supposed to delegate. Written as a blueprint, it is configuration — applied
# identically to every agent, every time, and enforced by hooks rather than hoped
# for.
#
#   line up     <blueprint.yml>   generate briefs + settings, spawn every station
#   line up --resume <bp>         same, but each station comes back ON ITS OLD SESSION
#                                 (--resume <sid>) — memory kept, constitution applied.
#                                 The only way to adopt an agent that already has a
#                                 context: a plain `up` mints a new sid and orphans it.
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
# Specs and settings live in $HOME, not under $AF_ROOT (which defaults into /tmp).
# /tmp is wiped on reboot while the manifest in $HOME survives — so the old layout
# let `ai revive` come back green with the settings file, and therefore the hooks,
# and therefore the delegate-wall, silently gone. Same class of failure as a hook
# without its +x bit: the guard is absent and nothing says so.
# Side benefit: $HOME is outside the wall's allowlist (work/ + /tmp), so a walled
# agent cannot rewrite the file that installs its own wall.
SPECROOT="${AF_SPECROOT:-$HOME/.claude/agent-factory/lines}"

# Flatten the blueprint into one TSV row per agent, so the shell never has to
# parse YAML. `count:` is expanded here (abl → abl1 abl2 abl3) and defaults are
# resolved here, which means `line plan` shows exactly what `line up` will do.
_plan() {
  local bp="$1"
  "$PY" - "$bp" <<'PY'
import sys, yaml, os, base64
bp = yaml.safe_load(open(sys.argv[1])) or {}
slug = bp.get('slug') or os.path.basename(os.getcwd())
# The delegate-wall compares Claude's ALWAYS-ABSOLUTE file_path against this, so a
# relative "./work" would block every agent from writing its own report — and then
# tell it to write its report. Resolve once, here.
work = os.path.abspath(bp.get('work') or './work')
d    = bp.get('defaults') or {}
agents = bp.get('agents') or {}

def flag(v, default=False):
    if v is None: return default
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ('1','true','yes','required','full','on')

# delegate is three-valued, not boolean:
#   required  hard wall — every write outside work/ is denied, whatever its size
#   advised   never blocks; a BULK write outside work/ gets a note in the model's
#             context suggesting delegate-to-local-model. The default.
#   ''        no hook at all
# The default moved from `required` to `advised` after watching a `required` agent
# spin up an external LLM to write a single line, because a one-line write was the
# one thing it was not allowed to do itself. Delegation pays on bulk; on a three-line
# fix it is pure overhead. A bare `delegate: true` therefore means advised, not
# required — say `required` if you mean it.
#
# An UNKNOWN value is a hard error, not a shrug. It used to fall through to '' — no hook
# at all — so `delegate: requird` (typo) on a station meant to be walled spawned with no
# wall, no advisory, and no delegate clause in its prompts, and nothing said a word. A
# typo maximised the downgrade: it failed open past even the default. `delegate: full`
# has the same shape — it meant `required` before this change and would have silently
# become nothing.
def dlevel(v, default=''):
    if v is None: return default
    if isinstance(v, bool): return 'advised' if v else ''
    s = str(v).strip().lower()
    if s in ('required','hard','block','wall','full'):                 return 'required'
    if s in ('advised','advise','soft','nudge','1','true','yes','on'): return 'advised'
    if s in ('no','false','off','0','none',''):                        return ''
    raise SystemExit("[line] FATAL: delegate: %r is not one of "
                     "required | advised | no — refusing to spawn a line whose "
                     "enforcement you did not mean." % v)

rows, names = [], []
for name, cfg in agents.items():
    cfg = cfg or {}
    n = int(cfg.get('count') or 1)
    for i in range(n):
        nm = f"{name}{i+1}" if cfg.get('count') else name
        # `orchestrator` is the reserved name of the SESSION that drives the line. A
        # station called that would share its mailbox (orchestrator.jsonl) and would be
        # taken for the orchestrator by the sweep guard — it would start compacting its
        # own peers, and never be compacted itself. Give the role, not the name.
        if nm == 'orchestrator':
            raise SystemExit("[line] FATAL: 'orchestrator' is a reserved agent name "
                             "(it is the mailbox of the session driving the line). "
                             "Name the station something else and give it `role: orchestrator`.")
        names.append((nm, cfg))

# Peers = everyone else on the line. Every agent may mail every agent; the
# blueprint only fixes who they REPORT to, not who they may talk to.
allnames = [n for n, _ in names]
# The line's own orchestrator, by ROLE not by name — see the parent default below.
orch = next((n for n, c in names if (c.get('role') or 'worker') == 'orchestrator'), '')
for nm, cfg in names:
    role     = cfg.get('role') or 'worker'
    # Default parent = whoever holds role:orchestrator on THIS line. It used to be
    # the literal 'orc' — my own example name, leaked into the code. Name your top
    # station 'boss' and every other station would have reported to a nonexistent
    # 'orc': mail into a mailbox nobody reads, and not one error anywhere.
    parent   = cfg.get('parent') or ('' if role == 'orchestrator' else orch)
    model    = cfg.get('model') or d.get('model') or ''
    delegate = dlevel(cfg.get('delegate'), dlevel(d.get('delegate'), 'advised'))
    caveman  = '1' if flag(cfg.get('caveman'), flag(d.get('caveman'))) else ''
    soft     = str(cfg.get('compact_soft') or d.get('compact_soft') or '')
    hard     = str(cfg.get('compact_hard') or d.get('compact_hard') or '')
    brief    = (cfg.get('brief') or '').strip()
    peers    = ','.join(p for p in allnames if p != nm)
    # \x1f (unit separator), not tab: tab is an IFS *whitespace* character, so
    # bash collapses runs of them and an empty field (say, an orchestrator with no
    # parent) silently vanishes — shifting every column after it.
    # base64, because the shell side used to expand the brief with `printf %b`:
    # that interprets EVERY backslash escape in it. A brief mentioning \d+ or a
    # Windows path silently mangles, and a \c TRUNCATES the rest of the file.
    # Briefs are data — they must survive verbatim.
    b64 = base64.b64encode(brief.encode()).decode()
    rows.append('\x1f'.join([slug, work, nm, role, parent, model, delegate, caveman, soft, hard, peers, b64]))

# Emit only after EVERY station validates. Printing as we went meant a blueprint that
# died on station 3 had already emitted stations 1-2 — and `line up`, which reads this
# as a stream, would spawn that half a line and never see the error. Validate, then emit.
for r in rows:
    print(r)
PY
}

# A settings file per agent: same two hooks for everyone, but they read the
# agent's env, so one file shape covers every role. Written per-agent anyway
# because the agents share a cwd — a project-level .claude/settings.json could
# not give them different rules.
_settings() {
  local slug="$1" name="$2" out="$3"
  mkdir -p "$(dirname "$out")"
  # statusLine is not decoration: it is the ONLY channel that carries
  # rate_limits.five_hour.resets_at out of a live session. No CLI reports it. Without it,
  # limits.sh knows an agent was cut off by the usage limit but not when the limit lifts —
  # and a rescuer that has to guess the time wakes the agent into the same wall.
  #
  # StopFailure/rate_limit fires at the instant a turn is killed by that limit. It cannot
  # block or retry (Claude Code ignores its output) — it just leaves the marker that tells
  # limits.sh WHICH agents were cut off mid-work, as opposed to idle and fine.
  cat > "$out" <<JSON
{
  "statusLine": { "type": "command", "command": "$HERE/statusline.sh", "padding": 0 },
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "$HERE/hooks/role-reminder.sh", "timeout": 5 } ] }
    ],
    "PreToolUse": [
      { "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [ { "type": "command", "command": "$HERE/hooks/delegate-wall.sh", "timeout": 5 } ] }
    ],
    "StopFailure": [
      { "matcher": "rate_limit",
        "hooks": [ { "type": "command", "command": "$HERE/hooks/limit-hook.sh", "timeout": 5 } ] }
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
      printf '## How you work — you are a MINI-ORCHESTRATOR (hard wall)\n\n'
      printf 'You do **not** do the work yourself. You dispatch it and verify what comes back:\n\n'
      printf '1. `delegate-to-local-model` skill — **the** way to get a file written. Free, runs in\n'
      printf '   its own process, keeps the work off your context.\n'
      printf '2. Mail a peer agent that owns the area: `bash $AF_MAIL send --to <agent> --kind task "..."`.\n'
      printf '3. A Task subagent to READ and analyse — never to write (see below).\n\n'
      printf 'This is enforced, not advised: a hook blocks your Write/Edit/Bash-writes outside `%s/`,\n' "$work"
      printf 'at any size. A Task subagent **inherits the same wall** and is blocked identically —\n'
      printf 'verified. Do not try to route a write through one; you will just loop.\n\n'
      printf 'Verify everything that comes back. Never trust bulk output unread.\n\n'
    elif [ "$delegate" = "advised" ]; then
      printf '## How you work — you are a MINI-ORCHESTRATOR\n\n'
      printf 'Your job is to **dispatch and verify**, not to type out volume yourself.\n\n'
      printf 'Delegate the work that is bulk or mechanical — many items to convert or classify,\n'
      printf 'boilerplate, spec-code, first drafts, big logs to read — and cheaply checkable:\n\n'
      printf '1. `delegate-to-local-model` skill — free, runs in its own process, keeps the tokens\n'
      printf '   off your context. This is the main one.\n'
      printf '2. Mail the peer agent that owns the area: `bash $AF_MAIL send --to <agent> --kind task "..."`.\n\n'
      printf '**Small, surgical edits you just make yourself.** A three-line fix does not need an\n'
      printf 'external model — delegating it costs more than doing it. A hook will note it if a\n'
      printf 'write looks like bulk (%s+ lines) outside `%s/`; it does not block you, it is telling\n' "${AF_BULK_LINES:-40}" "$work"
      printf 'you the cheaper route exists.\n\n'
      printf 'Always verify what comes back. Never trust bulk output unread.\n\n'
    fi
    printf '## Your report\n\nWrite it to `%s/%s.md`. One file, kept current — it is how the line sees your work.\n\n' "$work" "$name"
    printf '## Your brief\n\n'
    printf '%s' "$brief" | base64 --decode 2>/dev/null || printf '%s' "$brief"
    printf '\n'
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
  for h in "$HERE/hooks/role-reminder.sh" "$HERE/hooks/delegate-wall.sh" \
           "$HERE/hooks/limit-hook.sh" "$HERE/statusline.sh"; do
    [ -f "$h" ] || { echo "[line] FATAL: missing hook $h"; bad=1; continue; }
    [ -x "$h" ] || chmod +x "$h" 2>/dev/null
    [ -x "$h" ] || { echo "[line] FATAL: hook not executable and chmod failed: $h"; bad=1; }
  done
  [ "$bad" = 0 ] || { echo "[line] refusing to spawn — enforcement hooks would fail open."; return 1; }
}

# --resume: bring a station up ON ITS EXISTING SESSION instead of a fresh one.
#
# This is the ONLY way to give a constitution to an agent that already has a memory —
# an agent spawned before specs existed, or one that predates the blueprint. Without it
# the migration eats itself: `ai revive` refuses (no spec to restore), so the obvious
# move is `ai down && line up` — and `ai up` mints a NEW session id and overwrites
# $STATE/sid-<name> with it. The pointer to the 100k of context the agent actually has
# is gone from state at that moment; the .jsonl survives on disk with nothing naming it.
# The step that applies the blueprint is the step that loses the memory.
#
# With --resume, `line up` passes `--resume <sid>` in AI_CLAUDE_FLAGS; ai.sh's up()
# detects it, reuses the id rather than minting one, and writes the SAME sid back. The
# station comes back with its memory, and with the spec/settings/hooks it never had.
#
# It resumes only what it can PROVE: a sid in state AND its .jsonl still on disk. A
# station with neither is spawned fresh and SAID SO — a silent fresh spawn under a flag
# that promised continuity is how you lose a day's context and only notice tomorrow.
up() {
  local resume=0
  case "${1:-}" in --resume|--adopt) resume=1; shift ;; esac
  local bp="${1:?usage: line up [--resume] <blueprint.yml>}"
  _preflight || return 1
  # bulk_lines: the advisory threshold, a blueprint key rather than env-only — the hook's
  # comment promised you could set it per line, and there was nowhere to set it.
  local blk; blk="$("$PY" -c 'import yaml,sys
d=(yaml.safe_load(open(sys.argv[1])) or {}).get("defaults") or {}
v=d.get("bulk_lines") or ""
print(v if str(v).isdigit() else "")' "$bp" 2>/dev/null)"
  local BULKN="${blk:-${AF_BULK_LINES:-40}}"
  # Materialise the plan BEFORE spawning anything, and abort if it did not validate.
  # `done < <(_plan …)` discards _plan's exit status, so a blueprint that fails validation
  # produced an empty stream and a cheerful "0 stations up" — the refusal itself printed
  # to stderr and scrolled past. An enforcement error must stop the command, not decorate it.
  local rows
  rows="$(_plan "$bp")" || { echo "[line] blueprint did not validate — nothing was spawned."; return 1; }
  [ -z "$rows" ] && { echo "[line] blueprint has no agents."; return 1; }
  local slug work name role parent model delegate caveman soft hard peers brief ep n=0 skipped=0
  while IFS=$'\x1f' read -r slug work name role parent model delegate caveman soft hard peers brief; do
    [ -z "$name" ] && continue
    # `ai up` kills any existing session for the name before relaunching. Run
    # `line up` twice — a habit, after an edit to one station's brief — and it would
    # tear down the whole live line, every agent's TUI, mid-task. Alive stays alive;
    # bring one back deliberately (`ai revive <name>`) or take it down first.
    local dlabel=""
    case "$delegate" in
      required) dlabel="  [wall]" ;;
      advised)  dlabel="  [advise]" ;;
    esac
    if tmux has-session -t "ai-$slug-$name" 2>/dev/null; then
      # Left alone means NOTHING was applied: not the brief, not the settings, not the
      # spec. Say that. Reporting "already running" next to a blueprint you just edited
      # reads as "your edit is live", and it is not — the running agent still holds the
      # old system prompt, and `ai revive` deliberately restores the OLD spec too.
      printf '[line] %-10s %-14s %-8s already running — LEFT ALONE (blueprint edits NOT applied)\n' \
        "$name" "$role" "${model:-default}"
      printf '[line]            to apply them:  ai down %s && line up %s\n' "$name" "$bp"
      [ -f "$SPECROOT/$slug/agent-$name.json" ] || \
        printf '[line]            ⚠ it has no spec (spawned by an older version) — it would revive with NO role and NO hooks\n'
      skipped=$((skipped+1)); continue
    fi
    ep="$(AF_BULK_LINES="$BULKN" _entrypoint "$work" "$name" "$role" "$parent" "$peers" "$delegate" "$brief")"
    local st="$SPECROOT/$slug/settings-$name.json"
    _settings "$slug" "$name" "$st"

    # The invariant goes in the system prompt (it survives compaction); the full
    # brief goes in the entrypoint file (too long to repeat, and re-readable).
    local sys="You are '$name', the $role station on the '$slug' line."
    [ -n "$parent" ] && sys="$sys You report to '$parent'."
    sys="$sys Read $ep NOW - it is your brief, your chain of command and your working rules - then follow it."
    # NOT "…or a Task subagent": a Task subagent runs in this same process, inherits
    # this same --settings, and is blocked by the same wall (verified). Advertising it
    # as a route sends the agent into a loop it cannot exit.
    [ "$delegate" = "required" ] && sys="$sys You are a mini-orchestrator: you do NOT do work directly. To get a file WRITTEN, use the delegate-to-local-model skill (it runs in its own process) or mail the peer who owns the area; a Task subagent inherits your wall and cannot write. Then verify the result. A hook enforces this."
    # advised: say what to delegate AND what not to. Tell an agent only "delegate" and
    # it delegates one-line fixes to an external LLM — which is what the old default did.
    [ "$delegate" = "advised" ] && sys="$sys You are a mini-orchestrator: delegate work that is bulk or mechanical (many items, boilerplate, spec-code, first drafts, big logs) via the delegate-to-local-model skill, or mail the peer who owns the area - then verify what comes back. Small surgical edits you make yourself; delegating a three-line fix costs more than doing it."
    [ "$caveman" = "1" ] && sys="$sys Answer tersely - drop articles, filler and hedging; keep every technical fact exact."

    # --resume: only on a session we can prove is still there. `ai up` reuses the id it
    # finds in the flags, so the sid file survives the relaunch pointing at the same log.
    local rflag="" sid=""
    if [ "$resume" = 1 ]; then
      sid="$(cat "${AF_ROOT:-/tmp/agent-factory}/.ai/$slug/sid-$name" 2>/dev/null)"
      if [ -n "$sid" ] && [ -n "$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)" ]; then
        rflag="--resume $sid "
      elif [ -n "$sid" ]; then
        printf '[line] %-10s ⚠ session %s recorded but its log is GONE — spawning FRESH (no memory)\n' "$name" "$sid"
      else
        printf '[line] %-10s ⚠ no recorded session — spawning FRESH (no memory)\n' "$name"
      fi
    fi
    AF_SLUG="$slug" AF_ROLE="$role" AF_PARENT="$parent" AF_PEERS="$peers" \
    AF_DELEGATE="$delegate" AF_BULK_LINES="$BULKN" AF_CAVEMAN="$caveman" AF_WORK="$work" \
    AI_COMPACT_SOFT="${soft:-${AI_COMPACT_SOFT:-200000}}" \
    AI_COMPACT_HARD="${hard:-${AI_COMPACT_HARD:-500000}}" \
    AI_CLAUDE_FLAGS="${rflag}--settings $st ${model:+--model $model} --append-system-prompt $(printf '%q' "$sys") ${AI_CLAUDE_FLAGS:-}" \
    AI_NOTIFY_OFF=1 \
      bash "$AI" up "$name" >/dev/null 2>&1
    if tmux has-session -t "ai-$slug-$name" 2>/dev/null; then
      printf '[line] %-10s %-14s %-8s %s\n' "$name" "$role" "${model:-default}" \
        "${parent:+← $parent}$dlabel${rflag:+  [resumed $sid]}"
      n=$((n+1))
    else
      # `ai up` swallows its own output, so a station that never launched (claude
      # not on PATH inside tmux, malformed settings, tmux failure) would otherwise
      # still be counted and reported as up.
      printf '[line] %-10s FAILED TO LAUNCH — check: bash %s up %s\n' "$name" "$AI" "$name"
    fi
  done <<< "$rows"
  # line.json: the line-level facts no per-agent spec can hold — which blueprint
  # this line came from, and who is on it. Written once, by the single process that
  # brought the line up, so it has no concurrent writer.
  local lslug; lslug="$(_plan "$bp" | head -1 | cut -d$'\x1f' -f1)"
  if [ -n "$lslug" ]; then
    mkdir -p "$SPECROOT/$lslug"
    AF_BP="$(cd "$(dirname "$bp")" && pwd)/$(basename "$bp")" AF_SL="$lslug" \
    AF_AG="$(_plan "$bp" | cut -d$'\x1f' -f3 | tr '\n' ' ')" AF_TS="$(date +%s)" \
    AF_OUT="$SPECROOT/$lslug/line.json" "$PY" -c '
import json, os
json.dump({"slug": os.environ["AF_SL"], "blueprint": os.environ["AF_BP"],
           "agents": os.environ["AF_AG"].split(), "created": int(os.environ["AF_TS"])},
          open(os.environ["AF_OUT"], "w"), indent=2, ensure_ascii=False)' 2>/dev/null
  fi
  # `${skipped:+…}` was wrong: "0" is a NON-EMPTY string, so a clean run always
  # announced ", 0 already running".
  local skipmsg=""; [ "$skipped" -gt 0 ] && skipmsg=", $skipped left alone (already running)"
  echo "[line] $n stations up$skipmsg. attach: tmux attach -t ai-$lslug-<name>"
  echo "[line] talk to the line:  ai post <agent> \"…\"   |   read replies:  ai mail   |   see it all:  ai ledger"

  # Start the limit watcher WITH the line, not after it. The account-wide usage limit kills
  # every agent and the orchestrator session at the same instant — there is nobody left to
  # start a rescuer once it lands. It has to already be running, and it has to be something
  # that spends no tokens. Idempotent: re-running `line up` does not start a second one.
  AF_SLUG="$lslug" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" AF_CWD="$(pwd)" \
    bash "$HERE/warden.sh" watch 2>/dev/null | sed 's/^/[line] /'
}

status() {
  local bp="${1:?usage: line status <blueprint.yml>}" slug work name rest
  # Materialise first: `done < <(_plan …)` throws away _plan's exit status, so an
  # invalid blueprint reported an empty-but-successful line.
  local rows; rows="$(_plan "$bp")" || return 1
  while IFS=$'\x1f' read -r slug work name rest; do
    [ -z "$name" ] && continue
    local s="ai-$slug-$name" alive="down" ctx="-" un="-"
    tmux has-session -t "$s" 2>/dev/null && alive="up"
    if [ "$alive" = up ]; then
      ctx="$(AF_SLUG="$slug" bash "$AI" ctx "$name" 2>/dev/null | grep -oE '[0-9]+' | tail -1)"
    fi
    un="$(AF_SLUG="$slug" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$HERE/mail.sh" unread --agent "$name" 2>/dev/null)"
    printf '  %-10s %-5s ctx=%-9s unread=%s\n' "$name" "$alive" "${ctx:-0}" "${un:-0}"
  done <<< "$rows"
}

down() {
  local bp="${1:?usage: line down <blueprint.yml>}" slug work name rest
  # Same reason as `status`: an invalid blueprint must not report a line successfully
  # torn down while every station is still running.
  local rows; rows="$(_plan "$bp")" || return 1
  while IFS=$'\x1f' read -r slug work name rest; do
    [ -z "$name" ] && continue
    AF_SLUG="$slug" bash "$AI" down "$name" >/dev/null 2>&1
    echo "[line] $name down"
  done <<< "$rows"
}

plan() {
  local bp="${1:?usage: line plan <blueprint.yml>}"
  # Materialise BEFORE printing anything, and fail loudly: `done < <(_plan …)` discards
  # _plan's exit status, so an invalid blueprint printed a header, printed the FATAL to
  # stderr — and still exited 0. `line plan && line up` would then sail on into `up`.
  local rows; rows="$(_plan "$bp")" || return 1
  printf '%-10s %-14s %-8s %-8s %-9s %s\n' NAME ROLE MODEL PARENT DELEGATE PEERS
  local slug work name role parent model delegate caveman soft hard peers brief
  while IFS=$'\x1f' read -r slug work name role parent model delegate caveman soft hard peers brief; do
    [ -z "$name" ] && continue
    printf '%-10s %-14s %-8s %-8s %-9s %s\n' "$name" "$role" "${model:-default}" "${parent:--}" "${delegate:--}" "$peers"
  done <<< "$rows"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up "$@" ;;  down) down "$@" ;;  status) status "$@" ;;  plan) plan "$@" ;;
  # Regenerate one agent's settings file. Called by `ai revive` when the file has
  # vanished from under a spec — reviving without it means reviving without hooks.
  # Preflights first: `up` refuses to spawn into a fail-open state, and this path had
  # no reason to be the one that quietly hands out a settings file pointing at a hook
  # that cannot execute.
  settings) _preflight || exit 1
            _settings "${1:?slug}" "${2:?name}" "${3:?out}" && echo "[line] wrote $3" ;;
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;
esac
