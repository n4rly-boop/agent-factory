#!/usr/bin/env bash
# ai — spawn a REAL interactive `claude` TUI in a detached tmux session, then let
# THIS agent type into it and read its screen.
#
# How: interactive claude runs inside a tmux session, which gives it the real TTY
# the TUI needs. The controlling agent drives it with `tmux send-keys` and reads
# with `tmux capture-pane` — no FIFOs, no headless mode. The actual interactive app.
#
# NO WINDOW IS EVER OPENED. The agent is a detached tmux session and nothing else;
# a human who wants to watch runs `tmux attach -t ai-<slug>-<name>` (add -r to
# watch read-only, which is what you want while this session is driving it — two
# writers on one pane interleave keystrokes and corrupt the input).
#
#   ai up    [name]          launch interactive claude in a detached tmux session
#   ai say   [name] <text>   type text into it and submit (Enter)
#   ai keys  [name] <args>   send raw tmux keys (e.g. Escape, C-c, /model)
#   ai screen[name]          print the current TUI screen
#   ai ask   [name] <text>   say, wait for the turn to finish, print its result
#   ai wait  [name]          block until the agent is idle or needs input
#   ai result[name]          print the last completed turn's text (from the log)
#   ai approve[name] [1|2|3] answer a tool-permission prompt (default 2)
#   ai ctx   [name]          print the agent's estimated context size (tokens)
#   ai compact[name]         run /compact in the agent (only when idle — safe point)
#   ai remote[name]          (re)launch with Remote Control so you drive it from the Claude web/app
#   ai revive[name] [id]     relaunch a killed agent with its memory AND its role/hooks
#   ai revivable             list downed agents (with a surviving log) you can revive by name
#   ai ledger                one view of the whole line: role, model, ctx, mail, alive?
#   ai mail                  read YOUR mailbox (mail agents sent you) and ack it
#   ai post  <agent> [--kind K] <text>   send mail to an agent + ring its doorbell
#   ai mailstat              unread count per mailbox (the ack/retry signal)
#   ai sweep                 compact idle agents past their threshold; reap stale busy flags
#                            (runs automatically on `ai post` and `ai mail`; AI_SWEEP_OFF=1 to stop that)
#   ai inbox                 old name for `ai mail` (forwards to it)
#   ai register-self         (run inside the orchestrator's tmux) let agents WAKE you by mail
#   ai unregister-self       stop agents from waking this session
#   ai attach[name]          print the command to attach another viewer
#   ai down  [name]          quit claude + kill the session
#   ai list                  list running interactive agents
#
# Sessions are named ai-<slug>-<name> (slug = short project tag from the dir,
# or $AF_SLUG) so agents across projects don't collide: ai-linkai-worker,
# ai-inna-scout. `ai slug` prints the current slug.
#
# Compaction runs on TASK boundaries, not turn boundaries — a task spans many
# turns, and compacting mid-task is what throws away the working state the agent
# still needs. The mail protocol already says which is which (a task goes out, a
# done/result comes back), so that gates it:
#   AI_COMPACT_SOFT (200k)  compact only BETWEEN tasks; mid-task, just report.
#   AI_COMPACT_HARD (500k)  compact at the next TURN boundary regardless — losing
#                           some working state beats running out of context.
# Absolute token counts; override per agent (a 200k-window model needs far lower
# numbers than a 1M one). Set either to 0 to disable it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # this script's dir
CWD="${AF_CWD:-$(pwd)}"                           # where claude runs
FLAGS="${AI_CLAUDE_FLAGS:-}"                      # extra claude flags
# Every agent's tmux session is ai-<slug>-<name>. <slug> is a short per-project
# tag so agents from different projects are distinguishable in `tmux ls`
# (ai-linkai-worker vs ai-inna-scout). Derived from the project dir name;
# override with AF_SLUG. It also namespaces state, so a "worker" in one project
# never collides with a "worker" in another.
SLUG="${AF_SLUG:-$(basename "$CWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g' | cut -c1-12)}"
[ -z "$SLUG" ] && SLUG="proj"
S() { echo "ai-${SLUG}-${1:-claude}"; }          # tmux session name: ai-<slug>-<name>
STATE="${AF_ROOT:-/tmp/agent-factory}/.ai/${SLUG}"   # per-slug: session-id tracking
ORCHDIR="${AF_ROOT:-/tmp/agent-factory}/.ai"     # slug-keyed orchestrator registry (orch-<slug>)
INBOX="${AF_ROOT:-/tmp/agent-factory}/inbox.tsv"  # agent→orchestrator escalations (legacy log)
MAILROOT="${AF_ROOT:-/tmp/agent-factory}/.ai/${SLUG}/mail"  # per-slug mailboxes (the real channel)
NOTIFY="$HERE/notify.sh"                          # helper a spawned agent runs to escalate
MAIL="$HERE/mail.sh"                              # reliable agent↔agent transport (see mail.sh)
MANIFEST="$HOME/.claude/agent-factory/manifest.tsv"  # registry of spawned agents
# An agent's SPEC — the constitution it was spawned with: role env, model, flags,
# system prompt, settings (the file that installs its hooks). Everything needed to
# recreate it identically.
#
# It lives in $HOME, NOT under $AF_ROOT (which defaults into /tmp): /tmp is wiped
# on reboot, but the manifest in $HOME survives — so a post-reboot `ai revive` used
# to succeed and hand back an agent stripped of its role, its delegate-wall and its
# reminder hook, with nothing in its output saying so. The usual failure shape here:
# the safety mechanism is simply absent and no one is told. The spec is what makes
# revive restore the agent rather than a nameless twin of it.
#
# One file per agent, so each file has exactly ONE writer (the process spawning that
# name) and there is no read-modify-write race to lose. The single-pane-of-glass view
# is `ai ledger`, which JOINs the specs against the live world (tmux, session logs,
# mailboxes) — a derived view can't drift from reality the way a cached one does.
SPECROOT="${AF_SPECROOT:-$HOME/.claude/agent-factory/lines}"
SPECDIR="$SPECROOT/$SLUG"

# Record a spawned agent so its session log can be filtered/purged later.
# Columns: epoch  tool  name  session_id  cwd
_manifest() {
  mkdir -p "$(dirname "$MANIFEST")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "ai" "$1" "$2" "$CWD" >> "$MANIFEST"
}

# --- spec: the durable identity of an agent -------------------------------------

# The session id is per-LAUNCH, the spec is per-AGENT: storing --session-id/--resume
# inside the saved flags would make every revive resume the revive-before-last.
#
# NOT sed. The flags carry the appended system prompt, which contains an em dash —
# and BSD sed on macOS, under a non-UTF-8 locale, aborts on a byte it cannot decode
# ("illegal byte sequence") and prints NOTHING. That empty string became the saved
# flags: a spec with no model, no system prompt and no --settings, i.e. an agent that
# would revive with no hooks and therefore no delegate-wall — silently, since the
# error went to /dev/null and every other field looked right. Bytes in, bytes out,
# no locale in the middle.
_stripsid() {
  printf '%s' "$1" | python3 -c '
import sys, re
b = sys.stdin.buffer.read()
# Eat the whitespace that PRECEDES the flag, so removing it leaves no double space
# behind. A global "collapse runs of whitespace" would have reached inside the
# %q-quoted --append-system-prompt and silently rewritten the operators own prompt.
b = re.sub(rb"\s*--(?:session-id|resume)\s+[0-9a-fA-F-]{36}", b"", b)
sys.stdout.buffer.write(b.strip())
'
}
# agent-<name>.json, not <name>.json: an agent named `line` would otherwise share a
# path with the fleet file line.json. `line up` wrote the spec, then overwrote it with
# the fleet metadata — and `ai revive line` read a JSON with no flags and no env, i.e.
# revived it with no role and no wall while reporting success.
_specfile() { echo "$SPECDIR/agent-$1.json"; }

# Do the hooks this settings file installs actually EXIST and EXECUTE?
# A hook that cannot execute fails OPEN — Claude Code reports an error and runs the
# tool anyway. `line up` has always preflighted this; `revive`, the command whose
# entire job is restoring the wall, did not. Neither did the settings regeneration
# path. So the check lives here, next to the thing that needs it.
_hooks_ok() {
  local st="$1" h ok=0
  [ -f "$st" ] || return 1
  while IFS= read -r h; do
    [ -z "$h" ] && continue
    ok=1
    [ -x "$h" ] || chmod +x "$h" 2>/dev/null
    [ -x "$h" ] || { echo "[ai] ⚠ hook not executable: $h — it would FAIL OPEN (tool runs anyway)" >&2; return 1; }
  done <<< "$(python3 -c '
import json, sys
try: s = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
for grp in (s.get("hooks") or {}).values():
    for m in grp:
        for h in (m.get("hooks") or []):
            c = h.get("command", "")
            if c: print(c.split()[0])
' "$st" 2>/dev/null)"
  [ "$ok" = 1 ]   # a settings file that installs NO hooks is not a wall either
}

# Write the spec. Called on every `up`, so it always reflects how the agent is
# ACTUALLY running (not how a blueprint once said it should).
# python3 for the encoding: the flags string carries a %q-quoted --append-system-prompt
# with newlines and quotes inside it, and hand-rolled JSON escaping is how you lose it.
_writespec() {
  local name="$1" sid="$2" flags="$3"
  mkdir -p "$SPECDIR"
  local v env_kv="" ai_kv=""
  for v in AF_ROLE AF_PARENT AF_PEERS AF_DELEGATE AF_BULK_LINES AF_CAVEMAN AF_WORK; do
    [ -n "${!v:-}" ] && env_kv="$env_kv$v=${!v}"$'\x1f'
  done
  for v in AI_COMPACT_SOFT AI_COMPACT_HARD AI_NOTIFY_OFF AI_SKIP_PERMS; do
    [ -n "${!v:-}" ] && ai_kv="$ai_kv$v=${!v}"$'\x1f'
  done
  AF_S="$SLUG" AF_N="$name" AF_C="$CWD" AF_ID="$sid" AF_F="$flags" \
  AF_ENV="$env_kv" AF_AIENV="$ai_kv" AF_TS="$(date +%s)" AF_SPECFILE="$(_specfile "$name")" \
  python3 -c '
import json, os, re
def kv(s):
    out = {}
    for item in s.split("\x1f"):
        if "=" in item:
            k, _, v = item.partition("=")
            out[k] = v
    return out
flags = os.environ["AF_F"]
m = re.search(r"--model\s+(\S+)", flags)
st = re.search(r"--settings\s+(\S+)", flags)
env = kv(os.environ["AF_ENV"])
work = env.get("AF_WORK", "")
spec = {
    "slug": os.environ["AF_S"], "name": os.environ["AF_N"], "cwd": os.environ["AF_C"],
    "sid": os.environ["AF_ID"], "spawned": int(os.environ["AF_TS"]),
    "model": m.group(1) if m else "",
    # RAW, inline: without these the agent cannot be recreated, only approximated.
    "flags": flags, "env": env, "ai_env": kv(os.environ["AF_AIENV"]),
    # LINKS: big, live, or regenerable. Copying them here would just let the copy rot.
    "settings":   st.group(1) if st else "",
    "entrypoint": os.path.join(work, "entrypoint-%s.md" % os.environ["AF_N"]) if work else "",
    "work": work,
}
with open(os.environ["AF_SPECFILE"], "w", encoding="utf-8", errors="surrogateescape") as fh:
    # ensure_ascii: the flags may carry bytes that bash printf %q mangled on the way
    # here. Escaped, they at least keep the file valid JSON that the parser can read;
    # written raw, one bad byte makes the whole spec unloadable — and an unloadable
    # spec is an agent that revives with no role and no wall.
    fh.write(json.dumps(spec, indent=2, ensure_ascii=True) + "\n")
'
  # The system prompt reaches this script through bash's printf %q, and bash 3.2
  # mangles non-ASCII there: an em dash came out as one raw byte followed by two
  # escaped ones. Prompts are ours, so they are ASCII — but if one ever isn't, say so
  # rather than write a spec that reads back as garbage.
  case "$flags" in
    *[![:print:][:space:]]*|*[^[:ascii:]]*)
      echo "[ai] ⚠ non-ASCII in '$name' launch flags — bash %q may have mangled them; check $(_specfile "$name")" >&2 ;;
  esac
  # A spec that failed to write, or wrote without its flags, is not a small loss: it
  # is a revive that comes back with no role, no system prompt and no hooks, looking
  # exactly like a healthy one. Say so at spawn, when it can still be fixed.
  local sf; sf="$(_specfile "$name")"
  if [ ! -s "$sf" ]; then
    echo "[ai] ⚠ could not write the spec for '$name' ($sf) — it will revive WITHOUT its role or hooks" >&2
  elif [ -n "$flags" ] && [ -z "$(_spec_get "$sf" flags)" ]; then
    echo "[ai] ⚠ spec for '$name' recorded no launch flags — revive would drop its model/settings/system prompt" >&2
  fi
}

# Restore a spec into the environment: emits `export` lines that revive() eval's.
#
# Values are shlex.quote'd — a work path with a space in it would otherwise re-enter
# the shell as two words. KEYS ARE VALIDATED, not quoted: they land on the left of an
# `=` inside an eval, where quoting cannot save you. A spec with the key
# `AF_ROLE=w; rm -rf ~; X` would have emitted that verbatim into the operator's shell.
# Specs live under $HOME, outside the delegate-wall's allowlist — but the wall only
# exists for AF_DELEGATE=required agents, and the top orchestrator is deliberately
# unwalled. It could plant a spec for a peer and wait for the human to revive it.
#
# Exits non-zero and says why on a spec it cannot use, rather than printing nothing:
# empty output used to be read as "no spec, carry on" and the agent revived bare.
_spec_env() {
  python3 -c '
import json, re, shlex, sys
p = sys.argv[1]
try:
    s = json.load(open(p))
except Exception as e:
    sys.stderr.write("spec %s is unreadable (%s)\n" % (p, e)); sys.exit(1)
if not isinstance(s, dict):
    sys.stderr.write("spec %s is not an object\n" % p); sys.exit(1)
flags = s.get("flags") or ""
if not flags:
    sys.stderr.write("spec %s records no launch flags (no model, no --settings, no system prompt)\n" % p)
    sys.exit(1)
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
for group in ("env", "ai_env"):
    for k, v in (s.get(group) or {}).items():
        if not KEY.match(str(k)):
            sys.stderr.write("spec %s has a bogus env key %r — refusing\n" % (p, k)); sys.exit(1)
        if v != "": print("export %s=%s" % (k, shlex.quote(str(v))))
print("export AI_CLAUDE_FLAGS=%s" % shlex.quote(flags))
' "$1"
}
_spec_get() { python3 -c '
import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], "") or "")
except Exception: pass' "$1" "$2" 2>/dev/null; }

# An agent used to be able to own a Terminal.app window, which had to be closed by
# hand on `down` (and re-`up`) or it was left behind as an empty login shell. That
# whole path is gone: an agent is a detached tmux session, `tmux kill-session` ends
# it completely, and there is nothing else to clean up. Any human viewer is an
# `attach`ed client, which tmux detaches on its own when the session dies.
#
# Stale win-/tty- files from that era are removed on `down` so they can't outlive
# the code that understood them.
_rmwinstate() { rm -f "$STATE/win-$1" "$STATE/tty-$1" "$STATE/log-$1" 2>/dev/null || true; }

# When resuming, claude pauses on a chooser for large/old sessions:
#   ❯ 1. Resume from summary   2. Resume full session as-is   3. Don't ask again
# Auto-answer it so revive lands on a ready agent, not a stuck prompt. Default 2
# (full session — revive means "bring back the whole memory"); AI_RESUME_MODE=1
# for the cheaper summary. No-op (after a short watch) if no chooser appears.
_answer_resume() {
  local name="$1" mode="${AI_RESUME_MODE:-2}" i
  for ((i=0; i<24; i++)); do
    if tmux capture-pane -t "$(S "$name")" -p 2>/dev/null | grep -qE 'Resume full session as-is|Resume from summary'; then
      tmux send-keys -t "$(S "$name")" -l "$mode"; tmux send-keys -t "$(S "$name")" Enter
      echo "[ai] '$name' resume chooser → option $mode (set AI_RESUME_MODE to change)"
      return 0
    fi
    sleep 0.5
  done
}

up() {
  # The agent always lands in a detached tmux session. -w/--window used to also
  # open a Terminal.app window; it is accepted and ignored so an old call site (or
  # an agent working from a stale brief) doesn't die on an unknown argument — but
  # it must not be swallowed as the NAME, or `ai up -w` would spawn an agent
  # literally called "-w".
  local name="" a
  for a in "$@"; do
    case "$a" in
      -w|--window) echo "[ai] note: -w/--window is gone — agents are tmux-only now." >&2 ;;
      *) [ -z "$name" ] && name="$a" ;;
    esac
  done
  name="${name:-claude}"
  # `orchestrator` is the mailbox of the SESSION that drives the agents. An agent by
  # that name would share it, would be skipped by every sweep (so never compacted),
  # and would pass the sweep guard — it would start compacting its peers. line.sh
  # already refuses the name in a blueprint; the direct route must refuse it too.
  if [ "$name" = orchestrator ]; then
    echo "[ai] 'orchestrator' is a reserved name (it is the driving session's mailbox). Pick another." >&2
    return 1
  fi
  local s; s="$(S "$name")"
  tmux kill-session -t "$s" 2>/dev/null || true
  _rmwinstate "$name"
  # Give the agent a known identity so its session log is filterable later:
  # --session-id <uuid> sets the log filename (<uuid>.jsonl); we record that
  # uuid in the manifest. (Note: --append-system-prompt is NOT written to the
  # transcript, so the manifest — not an in-log marker — is the durable link.)
  # If resuming, reuse the existing id instead of minting a new one.
  local id launchflags
  if [[ "$FLAGS" == *"--resume"* ]]; then
    id="$(printf '%s' "$FLAGS" | grep -oiE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
    launchflags="$FLAGS"
  else
    id="$(uuidgen | tr 'A-Z' 'a-z')"
    launchflags="--session-id $id $FLAGS"
  fi
  # Spawned agents run unattended, so by default skip permission prompts — else
  # they'd silently stall on the first tool gate with no one to approve. Opt out
  # with AI_SKIP_PERMS=0; suppressed if the caller already set a permission flag.
  if [ "${AI_SKIP_PERMS:-1}" != 0 ] \
     && [[ "$launchflags" != *"--dangerously-skip-permissions"* ]] \
     && [[ "$launchflags" != *"--permission-mode"* ]]; then
    launchflags="$launchflags --dangerously-skip-permissions"
  fi
  _manifest "$name" "$id"
  # Persist the constitution BEFORE launching: role env, model, settings path and the
  # appended system prompt. `revive` reads it back, so an agent returns with its wall
  # and its chain of command, not just its memory.
  local basefl; basefl="$(_stripsid "$launchflags")"
  if [ -n "$launchflags" ] && [ -z "$basefl" ]; then
    echo "[ai] ⚠ launch flags for '$name' could not be recorded — reviving it later would drop its model, hooks and system prompt" >&2
  fi
  _writespec "$name" "$id" "$basefl"
  # Give the agent an IDENTITY and a back-channel to its orchestrator: env vars
  # (AF_AGENT/AF_INBOX/AF_NOTIFY) so notify.sh knows who it is + where to post,
  # plus an appended system prompt telling it to escalate real blockers instead
  # of stalling silently. Opt out with AI_NOTIFY_OFF=1. printf %q keeps the whole
  # thing safe through the sh -c that tmux runs the command under.
  # AF_MAIL is what makes an agent reachable by the RELIABLE channel: with it in
  # the env, a sender can ring its doorbell by typing the path-free `!bash $AF_MAIL`
  # (no slash → no autocomplete popup to swallow the Enter). The cap- marker below
  # is how senders know this agent understands it. Agents spawned by older versions
  # have neither, and senders fall back to the legacy payload-typing push for them.
  local full envpfx sysprompt
  envpfx="$(printf 'AF_AGENT=%q AF_SLUG=%q AF_INBOX=%q AF_NOTIFY=%q AF_MAIL=%q AF_MAILROOT=%q AF_ROOT=%q' \
            "$name" "$SLUG" "$INBOX" "$NOTIFY" "$MAIL" "$MAILROOT" "${AF_ROOT:-/tmp/agent-factory}")"
  # Role vars, when set (by `ai line` from a blueprint), travel into the agent's
  # env so its hooks can enforce the role without any per-agent config file: the
  # reminder hook reads them to restate the chain of command, the delegate-wall
  # reads them to decide what it may write. Unset ⇒ a plain unmanaged agent.
  local v
  for v in AF_ROLE AF_PARENT AF_PEERS AF_DELEGATE AF_BULK_LINES AF_CAVEMAN AF_WORK; do
    [ -n "${!v:-}" ] && envpfx="$envpfx $(printf '%s=%q' "$v" "${!v}")"
  done
  mkdir -p "$MAILROOT"; : > "$MAILROOT/cap-$name"
  if [ "${AI_NOTIFY_OFF:-0}" != 1 ]; then
    sysprompt="You are a spawned peer agent named '$name', launched by an orchestrator (another Claude) via the agent-factory skill. You run unattended with permissions skipped and no human necessarily watching.

MAIL - how you talk to the rest of the factory. Send: bash \$AF_MAIL send --to <agent> --kind <question|blocked|result|done|fyi> \"your message\". Read: bash \$AF_MAIL read (mail is also pushed to you automatically - when you see a MAIL block in your context, act on it and reply to the sender by mail). Your orchestrator is reachable as --to orchestrator.

When you hit a real blocker you cannot resolve on your own - a decision only the orchestrator or a human can make, a missing secret or access you lack, an irreversible or destructive action you should not take alone, or repeated failure on the same step - do NOT stall silently: mail the orchestrator (--kind blocked), then keep doing any work that does not depend on the answer. Escalate only genuine blockers, not routine progress. Mail --kind done with a summary when you finish a long task."
    full="$(printf '%s claude %s --append-system-prompt %q' "$envpfx" "$launchflags" "$sysprompt")"
  else
    full="$(printf '%s claude %s' "$envpfx" "$launchflags")"
  fi
  # Big virtual size so the TUI has room; start interactive claude as the cmd.
  tmux new-session -d -s "$s" -x 220 -y 50 -c "$CWD" "$full"
  # The spec is written before the launch (it has to be — it is built from the launch
  # flags). If the launch then failed, a spec left behind claims an agent that never
  # existed, and `ledger` would list it as merely "down (revivable)".
  if ! tmux has-session -t "$s" 2>/dev/null; then
    rm -f "$(_specfile "$name")"
    echo "[ai] ⚠ '$name' failed to launch — its spec was removed" >&2
    return 1
  fi
  echo "[ai] interactive claude launched (session=$s id=$id cwd=$CWD)"
  mkdir -p "$STATE"
  printf '%s' "$id" > "$STATE/sid-$name"   # jsonl-based completion tracking needs this
  echo "[ai] detached — watch it live:  tmux attach -r -t $s   (-r = read-only)"
  # If resuming, clear the "summary vs full" chooser so the agent is ready to drive.
  [[ "$launchflags" == *"--resume"* ]] && _answer_resume "$name"
  # Mail that arrived while this agent was down would otherwise sit unread forever:
  # nothing re-rings it, and the agent has no reason to go looking. Ring it now.
  local pending
  pending="$(AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$MAIL" unread --agent "$name" 2>/dev/null)"
  if [ "${pending:-0}" -gt 0 ]; then
    sleep 3   # let the TUI finish booting, or the doorbell types into nothing
    AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$MAIL" ring "$name" >/dev/null 2>&1 \
      && echo "[ai] '$name' had $pending unread message(s) — doorbell rung."
  fi
  echo "[ai] drive it:  ai say $name \"hello\"   |   watch screen:  ai screen $name"
}

say() {
  local name="${1:-claude}"; shift || true
  local s; s="$(S "$name")"; local msg="$*"
  [ -z "$msg" ] && { echo "[ai] usage: ai say $name <text>"; return 1; }
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name' — ai up $name"; return 1; }
  # NEVER type into a permission prompt. That prompt is a SELECT, not an input box:
  # C-u does not dismiss it, our text lands in the selector, and the Enter that follows
  # confirms the highlighted default — `❯ 1. Yes` — silently APPROVING a tool call no
  # human ever saw (and a message starting with a digit can pick an option outright).
  # `ring` and `compact` have always refused here; `say` was the one writer that did
  # not, and it got more dangerous when the clear stopped being an Escape: Escape at
  # least REJECTED the call. Answer prompts deliberately, with `ai approve`.
  _permission "$name" && {
    echo "[ai] '$name' is paused on a permission prompt — answer it first: ai approve $name"
    return 1
  }
  local try pending
  for try in 1 2; do
    # Clear the input box before typing: whatever is sitting there (a half-typed line,
    # a stale autocomplete popup) would otherwise CONCATENATE with our text —
    # `❯ leftover junkREAL MESSAGE`, verified live.
    #
    # C-u, NOT Escape. Escape clears the box too, but it also CANCELS a turn in progress
    # ("Interrupted · What should Claude do instead?"), and every `_busy` check that
    # guarded it is a screen read with a race in it: an agent that has just been rung
    # has not painted its timer yet, so it reads as idle and gets its work killed.
    # C-u clears the line and closes the popup while a running turn continues to
    # completion — verified live: mid-generation C-u, the agent finished its answer.
    tmux send-keys -t "$s" C-u
    sleep 0.2
    tmux send-keys -t "$s" -l "$msg"     # literal text (no key interpretation)
    sleep 0.2
    tmux send-keys -t "$s" Enter         # submit
    sleep 0.5
    # Verify against the LIVE input box only — the last `❯` line in the capture.
    # Success = the box no longer holds OUR message. An empty box OR a greyed
    # autosuggestion (which differs from our text) both count as submitted; only
    # our exact message still sitting there means a popup ate the Enter.
    # (Submitted prompts also appear as `❯ ...` in scrollback history; the
    # tail -1 isolates the live box, ignoring those.)
    pending="$(tmux capture-pane -t "$s" -p | grep '❯' | tail -1 | sed 's/.*❯[[:space:]]*//')"
    [ "$pending" != "$msg" ] && { echo "[ai] sent to '$name': $msg"; return 0; }
    echo "[ai] input box still holds our text (popup?), retrying…"
  done
  echo "[ai] WARN: '$name' may not have submitted — check: ai screen $name"; return 1
}

keys() {
  local name="${1:-claude}"; shift || true
  tmux send-keys -t "$(S "$name")" "$@"
  echo "[ai] keys -> '$name': $*"
}

screen() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  tmux capture-pane -t "$s" -p
}

# --- State signals --------------------------------------------------------
# Completion is read from the agent's STRUCTURED LOG, not the screen: the screen
# is a viewport snapshot (scrollback-limited, racy between tool calls), whereas
# the jsonl is the authoritative append-only event stream. A finished turn lands
# an assistant record with `stop_reason: end_turn`.
#
# Permission pauses, however, leave NO trace in the jsonl (the pending tool_use
# isn't even flushed while it waits), so "needs input" can ONLY be read from the
# TUI. Hence the split below: log for done, screen for needs-input.

_log() {  # path to this agent's session jsonl, via the id we recorded at `up`
  local sid cache p
  sid="$(cat "$STATE/sid-$1" 2>/dev/null)"; [ -z "$sid" ] && return 1
  # The find is a full walk of ~/.claude/projects (hundreds of MB of transcripts), and
  # it used to run on EVERY _ctx — which `sweep` now calls per agent, per `ai post`.
  # The path never moves once claude has created the log, so resolve it once and cache.
  cache="$STATE/log-$1"
  if [ -f "$cache" ]; then
    p="$(cat "$cache" 2>/dev/null)"
    case "$p" in *"/$sid.jsonl") [ -f "$p" ] && { printf '%s' "$p"; return 0; } ;; esac
  fi
  p="$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)"
  [ -z "$p" ] && return 1
  mkdir -p "$STATE"; printf '%s' "$p" > "$cache"
  printf '%s' "$p"
}
# Count completed turns. grep (not jq) so a half-written final line can't break
# it. Capture via $() because `grep -c` EXITS 1 on zero matches — an &&/|| chain
# would then emit a second "0" and yield the multiline "0\n0" that breaks -gt.
_endturns() {
  local f n; f="$(_log "$1")" || { echo 0; return; }
  [ -f "$f" ] || { echo 0; return; }
  n="$(grep -c '"stop_reason":"end_turn"' "$f" 2>/dev/null)"
  echo "${n:-0}"
}
# Estimate current context size in tokens: the full prompt the model last saw =
# input + cache-read + cache-creation tokens of the most recent assistant record.
# (output tokens aren't part of the next prompt; the cached buckets are.) Echoes 0
# if no usage yet. This is what we threshold for auto-compaction.
_ctx() {
  local f; f="$(_log "$1")" && [ -f "$f" ] || { echo 0; return; }
  # Only the LAST assistant record matters, so feed jq the tail instead of the whole
  # transcript. Sweep calls this per agent on every `ai post`/`ai mail`, and a mature
  # agent's jsonl runs to tens of MB — slurping all of it was seconds per agent, per
  # command. The leading fragment of a cut line fails `fromjson?` and is dropped, which
  # is exactly what we want; AI_CTX_TAIL raises the window if a single record is huge.
  tail -c "${AI_CTX_TAIL:-4000000}" "$f" 2>/dev/null | \
  jq -sRr 'split("\n") | map(fromjson? // empty)
           | map(select(.type=="assistant" and .message.usage)) | (last.message.usage // {})
           | ((.input_tokens//0)+(.cache_read_input_tokens//0)+(.cache_creation_input_tokens//0))' \
     2>/dev/null || echo 0
}
# Actively generating: a live "(Ns · …)" timer on screen.
_busy()      { tmux capture-pane -t "$(S "$1")" -p | grep -qE '\([0-9]+s · '; }
# Paused on a tool-permission decision.
_permission(){ tmux capture-pane -t "$(S "$1")" -p | grep -qE 'Do you want to proceed\?|❯ 1\. Yes'; }

# Block until the in-flight turn ends, the agent pauses for input, or timeout.
# DONE requires BOTH a new end_turn in the log AND the timer gone — so a spurious
# early end_turn (e.g. a thinking block) followed by more tool calls won't fool
# it. Echoes DONE | NEEDS_INPUT | TIMEOUT.
_wait_turn() {
  local name="$1" base="$2" to="${3:-300}" i
  for ((i=0; i<to*2; i++)); do
    _permission "$name" && { echo NEEDS_INPUT; return; }
    if [ "$(_endturns "$name")" -gt "$base" ] && ! _busy "$name"; then echo DONE; return; fi
    sleep 0.5
  done
  echo TIMEOUT
}

# Print the text of the most recent completed turn, straight from the log — no
# scraping, no scrollback limit. `fromjson?` tolerates a partial trailing line.
result() {
  local f; f="$(_log "$1")" && [ -f "$f" ] || { echo "[ai] no log for '$1' (not spawned by this skill?)"; return 1; }
  jq -sRr 'split("\n") | map(fromjson? // empty)
           | map(select(.type=="assistant" and .message.stop_reason=="end_turn")
                 | [.message.content[]? | select(.type=="text") | .text] | join("\n"))
           | map(select(length>0)) | (last // "(no text in last turn)")' "$f" 2>/dev/null
}

# Block until the agent is idle/paused now (ad-hoc, when you don't have a
# baseline). Echoes DONE | NEEDS_INPUT | TIMEOUT.
wait_() {
  local name="${1:-claude}" to="${2:-300}" i settle=0
  tmux has-session -t "$(S "$name")" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  for ((i=0; i<to*2; i++)); do
    _permission "$name" && { echo NEEDS_INPUT; return; }
    if _busy "$name"; then settle=0; else settle=$((settle+1)); [ "$settle" -ge 4 ] && { echo DONE; return; }; fi
    sleep 0.5
  done
  echo TIMEOUT
}

# say, then wait for THIS turn to finish, then print its result from the log.
# Surfaces a permission pause instead of hanging or guessing "done".
ask() {
  local name="${1:-claude}"; shift || true
  local base; base="$(_endturns "$name")"
  say "$name" "$@" || return 1
  case "$(_wait_turn "$name" "$base" "${AI_TIMEOUT:-300}")" in
    DONE)        result "$name"; _maybe_autocompact "$name"; _inbox_hint ;;
    NEEDS_INPUT) echo "[ai] ⚠ '$name' paused on a permission prompt — approve: ai approve $name [1|2|3]"
                 tmux capture-pane -t "$(S "$name")" -p | grep -vE '^[[:space:]]*$' | tail -8 ;;
    TIMEOUT)     echo "[ai] ⚠ '$name' still working past ${AI_TIMEOUT:-300}s — check: ai result $name / ai screen $name" ;;
  esac
}

# Print the agent's estimated context size in tokens.
ctx() { echo "[ai] '$1' context ≈ $(_ctx "${1:-claude}") tokens"; }

# Run /compact in the agent to shrink its context. SAFE ONLY between turns: it
# refuses while the agent is generating (would interrupt) or paused on a
# permission prompt (the keystrokes would answer the prompt, not compact).
# /compact preserves a summary, so no task context is lost — only raw history.
compact() {
  local name="${1:-claude}" mode="${2:-}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  _busy "$name"       && { echo "[ai] '$name' is mid-turn — refusing to compact (would interrupt). retry when idle."; return 1; }
  _permission "$name" && { echo "[ai] '$name' is on a permission prompt — answer it first (ai approve $name)."; return 1; }
  local before; before="$(_ctx "$name")"
  echo "[ai] compacting '$name' (ctx ≈ ${before} tok)…"
  say "$name" "/compact" || return 1
  # nowait (used by `sweep`): the keystrokes are sent, the agent compacts on its own.
  # Waiting is only to report the new size — and doing it inside an autosweep would
  # hang `ai post` for up to AI_TIMEOUT per over-threshold agent.
  if [ "$mode" = nowait ]; then
    echo "[ai] '$name' compacting in the background (was ≈ ${before} tok)."
    return 0
  fi
  wait_ "$name" "${AI_TIMEOUT:-300}" >/dev/null   # compaction runs the busy timer; wait it out
  # _ctx reads the usage of the last ASSISTANT record, and /compact does not write
  # one — the shrunk size only becomes visible after the agent's next turn. So
  # don't print a "now ≈ …" that is really the pre-compaction number in disguise.
  local after; after="$(_ctx "$name")"
  if [ "${after:-0}" -lt "${before:-0}" ]; then
    echo "[ai] '$name' compacted: ≈ ${before} → ${after} tok."
  else
    echo "[ai] '$name' compacted (was ≈ ${before} tok; the new size reads out after its next turn)."
  fi
}

# Is the agent in the middle of a TASK (not merely mid-turn)? The mail protocol
# records it: a task goes out, a done/result comes back. Absent any mail, treat
# the agent as idle — we'd rather compact a quiet agent than never compact one.
_mid_task() { [ "$(cat "$MAILROOT/state-$1" 2>/dev/null)" = "busy" ]; }

# Called after a turn finishes — the only safe point to compact, since /compact
# would otherwise interrupt generation. Two thresholds, because a turn boundary
# and a task boundary are different things:
#
#   SOFT (default 200k) — compact only at a TASK boundary. A task spans many
#     turns; compacting in the middle of one is what throws away working state.
#     So if the agent still owes us a `done`, we leave it alone and just report.
#   HARD (default 500k) — compact at the next TURN boundary regardless. Losing
#     some working state is bad; running out of context loses everything.
#
# Thresholds are absolute tokens. Override per agent (a 200k-window model needs
# far lower numbers than a 1M one): AI_COMPACT_SOFT / AI_COMPACT_HARD.
_maybe_autocompact() {
  local name="$1" c soft hard mode
  # Thresholds: explicit args (sweep passes the agent's own, from its spec) else this
  # session's env else the defaults.
  soft="${2:-}"; hard="${3:-}"; mode="${4:-}"
  soft="${soft:-${AI_COMPACT_SOFT:-200000}}"
  hard="${hard:-${AI_COMPACT_HARD:-500000}}"
  case "$soft" in ''|*[!0-9]*) soft=200000 ;; esac   # junk must not disable the guard
  case "$hard" in ''|*[!0-9]*) hard=500000 ;; esac
  c="$(_ctx "$name")"; [ "${c:-0}" -gt 0 ] || return

  if [ "$hard" != 0 ] && [ "$c" -gt "$hard" ]; then
    echo "[ai] context ≈ ${c} tok > hard ${hard} — compacting '$name' now (mid-task or not; running out would lose everything)…"
    compact "$name" "$mode"
    return
  fi
  if [ "$soft" != 0 ] && [ "$c" -gt "$soft" ]; then
    if _mid_task "$name"; then
      echo "[ai] context ≈ ${c} tok > soft ${soft}, but '$name' is mid-task — holding off (compacting now would drop its working state)."
    else
      echo "[ai] context ≈ ${c} tok > soft ${soft} and '$name' is between tasks — compacting…"
      compact "$name" "$mode"
    fi
  fi
}

# Milliseconds are not needed; portability is. BSD stat and GNU stat disagree on flags.
_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# An agent's own compaction thresholds, as recorded in its spec at spawn (line.sh puts
# compact_soft/compact_hard there per station). Empty when unknown — the caller falls
# back to this session's env.
_spec_thresholds() {
  python3 -c '
import json,sys
try:
    e = (json.load(open(sys.argv[1])).get("ai_env") or {})
    print("%s\x1f%s" % (e.get("AI_COMPACT_SOFT",""), e.get("AI_COMPACT_HARD","")))
except Exception:
    print("\x1f")' "$(_specfile "$1")" 2>/dev/null || printf '\x1f'
}

# (Re)launch the named agent with Claude Code's Remote Control enabled, so the
# human can monitor and drive it from the Claude web app / phone. Reuses the
# agent's recorded session (so memory survives the relaunch) when its log still
# exists; otherwise starts fresh. Requires being signed in to claude.ai.
remote() {
  local name="${1:-claude}" sid="${2:-}"
  [ -z "$sid" ] && sid="$(cat "$STATE/sid-$name" 2>/dev/null)"
  [ -z "$sid" ] && sid="$(awk -F'\t' -v n="$name" '$3==n{print $4}' "$MANIFEST" 2>/dev/null | tail -1)"
  tmux has-session -t "$(S "$name")" 2>/dev/null && down "$name"   # kill the old session first
  if [ -n "$sid" ] && [ -n "$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)" ]; then
    echo "[ai] launching '$name' with Remote Control, resuming session $sid"
    FLAGS="--resume $sid --remote-control $name $FLAGS"
  else
    echo "[ai] launching fresh '$name' with Remote Control"
    FLAGS="--remote-control $name $FLAGS"
  fi
  up "$name"
  echo "[ai] Remote Control on — open the Claude web app / phone to drive '$name' (sign-in required)."
}

# Answer a permission prompt (default 2 = allow & don't ask again), then wait for
# the resumed turn to finish and print its result.
approve() {
  local name="${1:-claude}" choice="${2:-2}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  local base; base="$(_endturns "$name")"
  tmux send-keys -t "$s" -l "$choice"; tmux send-keys -t "$s" Enter
  sleep 1   # let the prompt dismiss before we re-check state (else we'd read the stale prompt as NEEDS_INPUT)
  case "$(_wait_turn "$name" "$base" "${AI_TIMEOUT:-300}")" in
    DONE)        result "$name" ;;
    NEEDS_INPUT) echo "[ai] ⚠ another permission prompt for '$name' — ai approve $name" ;;
    TIMEOUT)     echo "[ai] ⚠ '$name' still working — ai result $name" ;;
  esac
}

# The only way a human ever sees a spawned agent. Read-only is offered first and
# on purpose: while this session is driving the agent with send-keys, a second
# writer on the same pane interleaves keystrokes and corrupts the input.
attach() {
  local s; s="$(S "${1:-claude}")"
  echo "tmux attach -r -t $s   # watch (read-only — safe while the orchestrator drives)"
  echo "tmux attach -t $s      # take over the keyboard (don't, while it's being driven)"
}

# Register THIS orchestrator's tmux pane so spawned agents can WAKE it directly
# (tmux send-keys) when they escalate — a true push into a live-but-idle session,
# no polling, no Stop-hook busy-wait. Must run from inside the orchestrator's own
# tmux session (launch it as: tmux new -s <slug>-lead 'claude'). Stored per-slug
# so each project's agents wake their own orchestrator.
register_self() {
  if [ -z "${TMUX:-}" ]; then
    echo "[ai] not inside tmux — can't register for send-keys wake."
    echo "[ai]   relaunch the orchestrator in tmux:  tmux new -s ${SLUG}-lead 'claude'"
    return 1
  fi
  local tgt; tgt="$(tmux display -p '#{session_name}:#{window_index}.#{pane_id}' 2>/dev/null)"
  [ -z "$tgt" ] && { echo "[ai] couldn't resolve this tmux pane"; return 1; }
  mkdir -p "$ORCHDIR" "$MAILROOT"
  printf '%s' "$tgt" > "$ORCHDIR/orch-$SLUG"     # legacy escalation push
  printf '%s' "$tgt" > "$MAILROOT/pane-orchestrator"   # mail doorbell target
  # An orchestrator started WITH AF_MAIL in its env gets the clean path-free
  # doorbell like any spawned agent. Started without it (a plain `claude` in
  # tmux), senders fall back to typing the literal path — still works, just
  # needs a popup-dismiss. Launch as: tmux new -s <slug>-lead 'AF_MAIL=<path> claude'
  if [ -n "${AF_MAIL:-}" ]; then
    : > "$MAILROOT/cap-orchestrator"
    echo "[ai] registered orchestrator for slug '$SLUG' → pane $tgt (mail-capable)"
  else
    rm -f "$MAILROOT/cap-orchestrator"
    echo "[ai] registered orchestrator for slug '$SLUG' → pane $tgt"
    echo "[ai]   note: AF_MAIL not in this session's env — doorbell falls back to typing a literal path."
    echo "[ai]   for the clean channel, launch the orchestrator as:"
    echo "[ai]     tmux new -s ${SLUG}-lead 'AF_MAIL=$MAIL AF_MAILROOT=$MAILROOT AF_SLUG=$SLUG claude'"
  fi
  echo "[ai] agents can now WAKE this session with mail (no polling needed)."
}
unregister_self() {
  rm -f "$ORCHDIR/orch-$SLUG" "$MAILROOT/pane-orchestrator" "$MAILROOT/cap-orchestrator"
  echo "[ai] unregistered orchestrator for slug '$SLUG'"
}

# --- Mail (the reliable channel) ------------------------------------------
# Thin front-ends onto mail.sh so the orchestrator drives the same transport its
# agents use. `ai mail` reads THIS session's mailbox (as 'orchestrator'); `ai post`
# sends to an agent, appending to its mailbox and ringing its doorbell.
mail_() {
  local rc=0
  AF_AGENT="${AF_AGENT:-orchestrator}" AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$MAIL" read "$@" || rc=$?
  _autosweep
  # `mail read` can genuinely fail (the box is locked by another reader), and that
  # verdict is this command's. Sweep's last act is a conditional `rm`, whose status is
  # not — returning it made `ai mail` look failed on every turn work was outstanding.
  return "$rc"
}
post()  {
  local to="${1:-}"; shift || true
  [ -z "$to" ] && { echo "[ai] usage: ai post <agent> [--kind K] <text>"; return 1; }
  # Default kind is `task`, because that is what posting to an agent MEANS. It is also
  # the signal that marks the agent busy — without it, `_mid_task` is never true and
  # compaction happily runs in the middle of a multi-turn task.
  # Only the LEADING flag position is scanned: a message body containing the word
  # --kind must not be read as one.
  local kind=""
  [ "${1:-}" = --kind ] && kind="${2:-}"
  # A dangling `--kind` with no value is the one input the two sides read differently:
  # mail.sh's parser breaks on the orphan flag, files the message as `fyi` (marking
  # nobody busy) and puts the literal word "--kind" in the body. Refuse it rather than
  # half-obey it.
  if [ "${1:-}" = --kind ] && [ -z "$kind" ]; then
    echo "[ai] --kind needs a value (task|question|blocked|result|done|fyi)" >&2
    return 1
  fi

  # SWEEP FIRST, SEND SECOND — and skip the recipient.
  #
  # Sweeping AFTER the send let the sweep's reaper delete the state-$to/tasker-$to that
  # `send` had just written for a task QUEUED to a down agent, reading them as garbage
  # from a crashed one. Sweeping first only ever sees state that predates this command.
  #
  # Skipping $to: a /compact typed at the same moment we hand the agent a task is a race
  # with nothing to win. Both are keystrokes into one input box; the compaction would
  # eat the turn we are trying to start, or land after it, and either way the agent is
  # about to be busy with the very task we just gave it.
  _autosweep "$to"

  # Supply --kind only when the caller didn't; passing it twice would send two flags.
  local kindflag=(); [ -z "$kind" ] && kindflag=(--kind task)
  AF_AGENT="${AF_AGENT:-orchestrator}" AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" \
    bash "$MAIL" send --to "$to" ${kindflag+"${kindflag[@]}"} "$@" || return 1

  # NO GATE. There used to be an `await` flag here: post a task, and the orchestrator's
  # Stop hook would hold its turn open for 45s hoping the reply landed inside it. It
  # bought nothing — the agents are working, and whatever they produce arrives as mail
  # that WAKES the orchestrator whenever it lands — while costing a 45s stall on every
  # idle turn whose flag was stale (a crashed agent, a queued task, an interrupted
  # sweep). Waiting is not how this system hears back; the doorbell is.
  return 0
}

# The context guard only fires where ai.sh happens to hold control at the end of an
# agent's turn — and a mail-driven fleet gives it no such moment (see `sweep`). That
# left `sweep` needing to be REMEMBERED, and "the model will remember" is exactly the
# guarantee that has silently failed in this system before. So every command that
# touches the fleet sweeps first: `post` (handing out work), `mail` (collecting it).
# Turn it off with AI_SWEEP_OFF=1.
#
# Only an ORCHESTRATOR sweeps: a worker running `ai mail` in its own session must not
# start compacting its peers. Orchestrator means either this top session (no AF_AGENT)
# or an agent whose ROLE is orchestrator — a line's own orc is named whatever the
# blueprint called it, and testing the NAME left the autonomous fleet (orc drives the
# workers by mail; the human never touches ai.sh) with no sweeps at all — exactly the
# case this was built for.
_autosweep() {
  [ "${AI_SWEEP_OFF:-0}" = 1 ] && return 0
  case "${AF_AGENT:-orchestrator}" in
    orchestrator) ;;
    *) [ "${AF_ROLE:-}" = orchestrator ] || return 0 ;;
  esac
  sweep "${1:-}" || true
  _self_ctx_warn
  return 0
}

# A sweeper cannot compact ITSELF: the sweep runs inside its own agent's Bash tool, so
# /compact would land in its own pane, mid-turn — its own turn. Whenever a human sweeps
# from the top session, a line's orc is just another station and gets compacted with
# the rest. The gap is the AUTONOMOUS line — orc drives the workers by mail and nobody
# up top ever runs ai.sh — where orc is the only sweeper and therefore the one agent
# nothing guards, while being the longest-lived on the line. We cannot act for it; we
# can tell it, in output it is already reading, to act for itself. (The top session has
# no AF_AGENT and no agent log of its own, so it says nothing there.)
_self_ctx_warn() {
  local me="${AF_AGENT:-}"; [ -z "$me" ] && return 0
  local c soft; c="$(_ctx "$me" 2>/dev/null)"; [ "${c:-0}" -gt 0 ] || return 0
  soft="${AI_COMPACT_SOFT:-200000}"
  case "$soft" in ''|*[!0-9]*) soft=200000 ;; esac
  [ "$soft" != 0 ] && [ "$c" -gt "$soft" ] && \
    echo "[ai] ⚠ YOUR OWN context is ≈ ${c} tok (> ${soft}). Nothing can compact you — run /compact yourself at your next safe point."
  return 0
}

# Compaction for a MAIL-DRIVEN fleet. `_maybe_autocompact` only ever ran from
# `ask`, but a line's agents are driven by mail, not by `ask` — so the context
# guard never applied to the very fleet it was built for. `sweep` walks every
# agent with a mailbox and applies the same two thresholds at a safe point.
sweep() {
  local skip="${1:-}"   # an agent the caller is about to touch — see `post`
  mkdir -p "$MAILROOT" "$STATE"
  # One sweep at a time. Two concurrent ones would each decide the same agent is idle
  # and type /compact into the same pane twice. A lock older than 10 min is a corpse
  # (the holder was killed mid-sweep) — take it.
  local lock="$STATE/sweep.lock"
  if ! mkdir "$lock" 2>/dev/null; then
    local age; age="$(( $(date +%s) - $(_mtime "$lock") ))"
    if [ "$age" -lt 600 ]; then
      # Silence here would make an explicit `ai sweep` report success having done
      # nothing at all.
      echo "[ai] another sweep is running (lock ${age}s old) — skipped."
      return 0
    fi
    rmdir "$lock" 2>/dev/null; mkdir "$lock" 2>/dev/null || return 0
  fi
  # Released at the end AND on a signal: a sweep killed by Ctrl-C or a tool timeout
  # (each `_ctx` reads a whole transcript, so a slow sweep is real) would otherwise
  # leave the lock behind and silently disable EVERY sweep — and with it the gate
  # disarm — for the next 10 minutes.
  # $STATE is a global, so this trap body is safe under `set -u`, which a `trap …
  # RETURN` on the local $lock was NOT: it ran after the function returned, when the
  # local was already gone, and killed the whole `ai post`.
  # EXIT releases the lock. INT/TERM must ALSO exit: a handler that only cleans up lets
  # bash resume the loop, so a Ctrl-C'd sweep could not be aborted AND kept running with
  # its lock already released — a second sweep could start and type /compact into the
  # same pane twice, which is the one thing the lock exists to prevent.
  trap 'rmdir "$STATE/sweep.lock" 2>/dev/null || true' EXIT
  trap 'rmdir "$STATE/sweep.lock" 2>/dev/null || true; exit 130' INT TERM

  local box name soft hard
  shopt -s nullglob
  for box in "$MAILROOT"/*.jsonl; do
    name="$(basename "$box" .jsonl)"
    [ "$name" = orchestrator ] && continue
    [ "$name" = "${AF_AGENT:-}" ] && continue   # never compact yourself: /compact would
                                                # land in your own pane, mid-turn
    [ -n "$skip" ] && [ "$name" = "$skip" ] && continue
    tmux has-session -t "$(S "$name")" 2>/dev/null || continue
    _busy "$name" && continue          # mid-turn: not a safe point, skip
    _permission "$name" && continue    # waiting on a human: don't touch
    # The agent's OWN thresholds, from its spec — not the orchestrator's env. A station
    # on a 200k-window model is configured `compact_soft: 80000`; judging it by this
    # session's 200000 means it is never compacted until it dies.
    IFS=$'\x1f' read -r soft hard <<< "$(_spec_thresholds "$name")"
    # nowait: /compact is keystrokes, and the agent compacts on its own time. Blocking
    # here would put a 300s wait_ inside every `ai post` — per over-threshold agent.
    _maybe_autocompact "$name" "$soft" "$hard" nowait
  done
  shopt -u nullglob

  # The reaper. A `busy` flag says "this agent owes a done/result", and SOFT compaction
  # refuses to touch a busy agent — so a flag whose agent no longer exists silently
  # exempts that NAME from compaction forever (the next agent to take the name inherits
  # it). Reap it.
  #
  # UNLESS the dead agent still has UNREAD mail: then the flag is not garbage, it is a
  # task legitimately QUEUED for an agent that is down (mail.sh promises "it will be
  # rung on next ai up/revive"). Reaping it would also destroy the tasker record its
  # eventual `done` needs in order to clear the busy state.
  local f who un
  shopt -s nullglob
  for f in "$MAILROOT"/state-*; do
    [ "$(cat "$f" 2>/dev/null)" = busy ] || continue
    who="$(basename "$f")"; who="${who#state-}"
    tmux has-session -t "$(S "$who")" 2>/dev/null && continue
    un="$(AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" \
          bash "$MAIL" unread --agent "$who" 2>/dev/null)"
    [ "${un:-0}" -gt 0 ] && continue            # still owed: leave the bookkeeping alone
    rm -f "$f" "$MAILROOT/tasker-$who"
  done
  shopt -u nullglob
  rmdir "$lock" 2>/dev/null || true
  trap - EXIT INT TERM
  return 0
}
# Unread count per mailbox — the retry/ack signal. A sender that sees its message
# still unread after a while can ring again (or conclude the agent is dead).
mailstat() {
  mkdir -p "$MAILROOT"
  local box who n
  shopt -s nullglob
  for box in "$MAILROOT"/*.jsonl; do
    who="$(basename "$box" .jsonl)"
    n="$(AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$MAIL" unread --agent "$who")"
    printf '  %-14s %s unread\n' "$who" "$n"
  done
  shopt -u nullglob
}

# Relaunch a previously-`down`ed agent WITH its full memory, by resuming its
# recorded session. `down` keeps the log; only `afctl purge` deletes it — so
# revive works any time the log still exists. Resolves the session id from (in
# order) an explicit arg, the sid state file, or the last manifest entry.
#
# It REFUSES rather than reviving an agent it cannot fully restore. Every degraded
# path here — no spec, corrupt spec, spec without flags, settings whose hooks won't
# execute — produces an agent that looks healthy and has no wall. A refusal is
# recoverable; a silently unwalled mini-orchestrator writing to your repo is not.
# Override with AI_FORCE=1 when you genuinely want the memory back without the role.
revive() {
  local name="${1:-claude}" sid="${2:-}" force="${AI_FORCE:-0}"
  local sf; sf="$(_specfile "$name")"
  local opflags="$FLAGS"     # anything the operator passed on this command line

  [ -z "$sid" ] && sid="$(cat "$STATE/sid-$name" 2>/dev/null)"
  [ -z "$sid" ] && sid="$(_spec_get "$sf" sid)"
  # Manifest fallback is keyed on NAME ONLY — it does not know about slugs. Run
  # `ai revive orc` from the wrong directory and it happily resurrects the real orc's
  # memory into session ai-<wrongslug>-orc: no role, no wall, and a fresh mailbox
  # nobody reads. So it is only allowed to answer when a spec confirms the identity.
  if [ -z "$sid" ]; then
    sid="$(awk -F'\t' -v n="$name" '$3==n{print $4}' "$MANIFEST" 2>/dev/null | tail -1)"
    if [ -n "$sid" ] && [ ! -f "$sf" ] && [ "$force" != 1 ]; then
      echo "[ai] refusing: found a session for '$name' in the manifest, but no spec under $SPECDIR."
      echo "[ai]   Either you are in the wrong directory (slug='$SLUG' — set AF_SLUG), or this agent"
      echo "[ai]   predates specs. Reviving it now would restore its memory with NO role and NO hooks."
      echo "[ai]   Deliberate? AI_FORCE=1 ai revive $name"
      return 1
    fi
  fi
  [ -z "$sid" ] && { echo "[ai] no recorded session for '$name' — see: ai revivable"; return 1; }
  [ -z "$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)" ] \
    && { echo "[ai] session $sid log is gone (purged?) — can't revive '$name'"; return 1; }

  # Memory without a constitution is the wrong agent. Restore the spec: role env,
  # model, appended system prompt, compaction thresholds, and the --settings file
  # that installs the hooks. The SPEC — not the blueprint — is the source of truth:
  # the agent's 100k of context was built under these rules, and reviving it under
  # rules that have since changed hands it a system prompt its own history contradicts.
  # To adopt an edited blueprint, respawn deliberately (`line up`), don't revive.
  if [ -f "$sf" ]; then
    local restored err
    err="$(_spec_env "$sf" 2>&1 >/dev/null)"
    restored="$(_spec_env "$sf" 2>/dev/null)"
    if [ -z "$restored" ]; then
      echo "[ai] refusing to revive '$name': ${err:-its spec could not be read}"
      echo "[ai]   A spec that won't load means no role, no hooks, no model, no system prompt."
      echo "[ai]   Fix or delete $sf, or respawn the line. Deliberate? AI_FORCE=1 ai revive $name"
      [ "$force" != 1 ] && return 1
    else
      eval "$restored"
      FLAGS="$AI_CLAUDE_FLAGS${opflags:+ $opflags}"   # spec first; operator flags win by coming last
      # The settings file lives outside the repo and can be deleted from under us. A
      # --settings pointing at nothing is not an error claude refuses — it is an agent
      # with no hooks, i.e. no delegate-wall, and nothing says so. Same for a settings
      # file whose hooks lost their +x bit: those fail OPEN.
      local st; st="$(_spec_get "$sf" settings)"
      if [ -n "$st" ]; then
        if [ ! -f "$st" ]; then
          echo "[ai] settings file for '$name' was gone — regenerating (its hooks would have been silently absent)"
          bash "$HERE/line.sh" settings "$SLUG" "$name" "$st" >/dev/null 2>&1 || true
        fi
        if ! _hooks_ok "$st"; then
          # Only a `required` station is REFUSED. Its whole point is that the wall is
          # load-bearing, and a fail-open hook silently removes it. An `advised` station
          # loses a nudge, not a guarantee — worth saying, not worth blocking on.
          if [ "${AF_DELEGATE:-}" = "required" ]; then
            echo "[ai] refusing to revive '$name': it is delegate:required, and its hooks are missing or not executable, so they would FAIL OPEN"
            echo "[ai]   (Claude Code runs the tool anyway on a hook error — the wall would be a wall-shaped hole.)"
            echo "[ai]   settings: $st"
            [ "$force" != 1 ] && return 1
            echo "[ai] AI_FORCE=1 — reviving '$name' UNWALLED anyway."
          else
            echo "[ai] ⚠ '$name' hooks are not executable — its role-reminder and delegate advice will be missing"
          fi
        fi
      fi
      echo "[ai] restored spec: role=${AF_ROLE:-none} parent=${AF_PARENT:-none} delegate=${AF_DELEGATE:-no} model=$(_spec_get "$sf" model)"
    fi
  else
    echo "[ai] ⚠ no spec for '$name' — reviving with memory but NO role and NO hooks (spawned before specs, or spec deleted)"
    [ "$force" != 1 ] && { echo "[ai]   Deliberate? AI_FORCE=1 ai revive $name"; return 1; }
  fi

  echo "[ai] reviving '$name' from session $sid"
  FLAGS="--resume $sid $FLAGS"   # up() detects --resume and reuses this id
  up "$name"
}

# The one place to look. A JOIN of the durable specs against the LIVE world — tmux
# for aliveness, the session log for context size, the mailbox for unread and for
# busy/idle. Derived on read, so it cannot drift from reality the way a cached
# status table would; the specs supply only what the live world cannot say (role,
# chain of command, which model, which wall).
ledger() {
  local d="$SPECDIR"
  [ -d "$d" ] || { echo "[ai] no line on '$SLUG' yet (no specs in $d)"; return 0; }
  local specs; specs="$(find "$d" -maxdepth 1 -name 'agent-*.json' 2>/dev/null | sort)"
  [ -z "$specs" ] && { echo "[ai] no agents recorded for '$SLUG'"; return 0; }
  local bp; bp="$(_spec_get "$d/line.json" blueprint)"
  echo "[ai] line '$SLUG'${bp:+   blueprint: $bp}   specs: $d"
  printf '\n%-10s %-14s %-8s %-8s %8s %5s %-6s %s\n' NAME ROLE MODEL PARENT CTX MAIL STATE SESSION
  local f name role model parent delegate settings sess alive ctx unread state wall
  # `ledger` is a LOOK, so it does not compact: silently shrinking an agent's memory
  # out from under someone who came to inspect it is not what "show me the line" means.
  # It reports what a sweep would do instead. (post/mail sweep for real — they act.)
  local fat="" soft asoft
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    name="$(_spec_get "$f" name)"
    # A spec that will not parse used to be skipped silently — so the one agent whose
    # wall is gone was the one agent missing from the view meant to reveal that. Show it.
    if [ -z "$name" ]; then
      name="$(basename "$f" .json)"; name="${name#agent-}"
      printf '%-10s %s\n' "$name" "!! SPEC CORRUPT ($f) — will refuse to revive; fix or delete it"
      continue
    fi
    role="$(python3 -c '
import json,sys
s=json.load(open(sys.argv[1])); e=(s.get("env") or {})
print("%s\x1f%s\x1f%s\x1f%s" % (e.get("AF_ROLE",""), e.get("AF_PARENT",""),
                                e.get("AF_DELEGATE",""), s.get("settings","")))' "$f" 2>/dev/null)"
    IFS=$'\x1f' read -r role parent delegate settings <<< "$role"
    model="$(_spec_get "$f" model)"
    sess="$(S "$name")"
    if tmux has-session -t "$sess" 2>/dev/null; then
      alive="● alive"; ctx="$(_ctx "$name" 2>/dev/null)"
      _busy "$name" && state="busy" || { _mid_task "$name" && state="task" || state="idle"; }
    else
      alive="○ down"; ctx=""; state=""
      [ -n "$(find "$HOME/.claude/projects" -type f -name "$(_spec_get "$f" sid).jsonl" 2>/dev/null | head -1)" ] \
        && alive="○ down (revivable)"
    fi
    # The wall column is a LIVE CHECK, not an echo of AF_DELEGATE. It used to print
    # [wall] for any agent whose spec merely SAID delegate:required — including one
    # with no settings file at all. The single place an operator would look to notice
    # a missing wall was reading the wrong column, and always said the wall was there.
    wall=""
    case "$delegate" in
      required)
        if [ -n "$settings" ] && _hooks_ok "$settings" 2>/dev/null; then wall="  [wall]"
        else wall="  !! NO WALL (hooks missing/not executable)"; fi ;;
      advised)
        if [ -n "$settings" ] && _hooks_ok "$settings" 2>/dev/null; then wall="  [advise]"
        else wall="  [advise: hooks broken]"; fi ;;
    esac
    unread="$(AF_SLUG="$SLUG" AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$MAIL" unread --agent "$name" 2>/dev/null)"
    # Each agent judged by ITS OWN soft threshold (from its spec), like sweep does.
    IFS=$'\x1f' read -r asoft _ <<< "$(_spec_thresholds "$name")"
    soft="${asoft:-${AI_COMPACT_SOFT:-200000}}"
    case "$soft" in ''|*[!0-9]*) soft=200000 ;; esac
    [ "$soft" != 0 ] && [ "${ctx:-0}" -gt "$soft" ] 2>/dev/null && fat="$fat $name"
    printf '%-10s %-14s %-8s %-8s %8s %5s %-6s %s\n' \
      "$name" "${role:--}" "${model:-default}" "${parent:--}" "${ctx:--}" "${unread:-0}" "${state:--}" \
      "$alive$wall"
  done <<< "$specs"
  echo
  [ -n "$fat" ] && echo "[ai] ⚠ past their soft threshold:$fat — 'ai sweep' will compact the idle ones."
  return 0
}

# List agents that CAN be revived by name: every ai-spawned agent in the manifest
# whose session log still exists (not purged) and which isn't currently running.
# `list` shows only live sessions, so this is how you find a downed agent's name.
revivable() {
  [ -f "$MANIFEST" ] || { echo "[ai] no manifest — nothing spawned yet"; return; }
  local out
  # latest manifest row per ai/<name>, scoped to THIS project (cwd col), then
  # keep only downed-with-surviving-log. cwd-scoping keeps slug/session names
  # consistent (same cwd → same slug → S() resolves correctly).
  out="$(awk -F'\t' -v cwd="$CWD" '$2=="ai" && $5==cwd {seen[$3]=$4} END{for(n in seen) print n"\t"seen[n]}' "$MANIFEST" \
        | while IFS=$'\t' read -r name id; do
            [ -z "$id" ] && continue
            tmux has-session -t "$(S "$name")" 2>/dev/null && continue   # running → not "revivable"
            [ -n "$(find "$HOME/.claude/projects" -type f -name "$id.jsonl" 2>/dev/null | head -1)" ] || continue
            printf '  %s\t(%s)\n' "$name" "$id"
          done)"
  if [ -z "$out" ]; then echo "[ai] no revivable agents (none downed with a surviving log)"
  else echo "[ai] revivable (run: ai revive <name>):"; printf '%s\n' "$out"; fi
}

down() {
  local name="${1:-claude}" s; s="$(S "$name")"
  # A `busy` state that outlives its agent silently disables soft compaction for
  # the next agent to take that name — it inherits the stale flag and never
  # compacts again.
  rm -f "$MAILROOT/state-$name" "$MAILROOT/tasker-$name" 2>/dev/null
  tmux kill-session -t "$s" 2>/dev/null || true
  _rmwinstate "$name"
  echo "[ai] '$name' down — session killed."
}

list() { tmux ls 2>/dev/null | grep '^ai-' || echo "[ai] none"; _inbox_hint; }

# --- Escalations ----------------------------------------------------------
# `inbox` is the old name for "show me what agents sent"; mail is where that
# lives now, so it just forwards. The shared TSV it used to read is dead — no
# code writes it any more.
inbox() { mail_; }

# One-line nudge appended to ask/list output when mail is waiting, so the
# orchestrator notices an agent that blocked while it was doing other work.
_inbox_hint() {
  local n; n="$(AF_AGENT="${AF_AGENT:-orchestrator}" AF_SLUG="$SLUG" \
                AF_ROOT="${AF_ROOT:-/tmp/agent-factory}" bash "$MAIL" unread 2>/dev/null)"
  [ "${n:-0}" -gt 0 ] || return 0
  echo "[ai] ⚠ $n unread message(s) from spawned agents — ai mail"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up "$@" ;;  say) say "$@" ;;  keys) keys "$@" ;;
  screen) screen "$@" ;;  ask) ask "$@" ;;  approve) approve "$@" ;;  attach) attach "$@" ;;
  ctx) ctx "$@" ;;  compact) compact "$@" ;;  remote) remote "$@" ;;
  wait) wait_ "$@" ;;  result) result "$@" ;;  inbox) inbox "$@" ;;
  mail) mail_ "$@" ;;  post) post "$@" ;;  mailstat) mailstat ;;  sweep) sweep ;;
  register-self) register_self ;;  unregister-self) unregister_self ;;  slug) echo "$SLUG" ;;
  revive) revive "$@" ;;  revivable) revivable ;;  ledger) ledger ;;
  down) down "$@" ;;  list) list ;;
  # Help = the header comment, printed up to the first non-comment line. Derived,
  # not a hardcoded line range — so editing the header can't silently truncate it.
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;
esac
