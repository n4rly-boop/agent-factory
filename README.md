# agent-factory

An experiment in agent creation, orchestration, and autonomous communication:
let one Claude Code session **spawn long-lived, independent Claude agents in
their own shells and talk to them** across many turns. These are real peer
agents — their own context window and session, persistent until killed — not
ephemeral subagents.

## Two flavors

| Tool | What it is | Use when |
|------|-----------|----------|
| **`factory/line.sh`** | A whole **production line** of agents from one `blueprint.yml`: roles, chain of command, per-agent model, enforced delegation. | You want a team, not an agent. |
| **`factory/ai.sh`** | A real interactive Claude **TUI** in a detached tmux session, driven via `tmux send-keys` and read via `tmux capture-pane`. | You want a real, tool-using Claude you can watch and drive. Default. |
| **`factory/mail.sh`** | The **channel between agents**: mailbox + doorbell + cursor-as-ack. | Agents talking to each other, reliably. |
| **`factory/af.sh`** | A **headless worker** (`claude -p` loop) driven over a FIFO message bus, persistent `--resume` session. | Programmatic request→reply, autonomous loops, agent-to-agent chains. |
| **`factory/afctl.sh`** | Cleanup of spawned agents' session logs via a session-id manifest. | Purge factory logs without touching your manual sessions. |

## A line, from a blueprint

A fleet's design — who exists, who reports to whom, who may only delegate, who
gets the cheap model — is the part that decays fastest when you carry it in a
prompt. Spawn five agents, tell each its role once, and thirty turns later
nobody remembers they were supposed to delegate. So it goes in a file, and hooks
enforce it:

```yaml
# blueprint.yml
slug: rlhf-exp
work: ./work
defaults:
  model: sonnet
  caveman: true          # terse output — a hook, not a request
  delegate: required     # mini-orchestrator: may not edit code itself
agents:
  orc:
    role: orchestrator   # the one YOU talk to
    model: opus
    delegate: no         # the top orchestrator may act directly
    brief: |
      Own the experiment end to end. Dispatch to eval/abl*, collect their reports.
  eval:
    role: evaluation
    parent: orc
    brief: |
      Own metrics, baselines, eval scripts.
  abl:
    count: 3             # → abl1, abl2, abl3
    role: ablation
    parent: orc
    model: haiku
    brief: |
      Test exactly ONE hypothesis. Report to work/<you>.md, mail orc when done.
```

```bash
bash factory/line.sh plan   blueprint.yml   # resolved roles, no spawn
bash factory/line.sh up     blueprint.yml   # briefs + settings + spawn
bash factory/line.sh status blueprint.yml   # alive? context size? unread mail?
bash factory/line.sh down   blueprint.yml
```

Each station gets a generated `work/entrypoint-<name>.md` (the full brief) and a
private settings file wiring two hooks. **Roles are enforced, not requested:**

- **role-reminder** (`UserPromptSubmit`) restates identity, chain of command and
  standing orders on *every* prompt, for ~25 tokens. A rule stated once in the
  system prompt survives compaction but loses to 200k tokens of recent work for
  attention; a rule restated next to the task does not.
- **delegate-wall** (`PreToolUse` on Write/Edit/NotebookEdit/**Bash**) *denies* a
  mini-orchestrator's direct edits outside its `work/` dir and names the way out.
  Observed in testing: the agent hit the wall and re-routed to
  `delegate-to-local-model` on its own, and the work still got done. Delegation
  stops being a preference.

Three things the wall had to learn the hard way, all found by review + a live test:

- **`Bash` is the first thing an agent reaches for after a denied `Write`.**
  Matching only Write/Edit leaves `echo > file`, `tee`, `sed -i` wide open — the
  wall was decorative. Bash commands are now tokenised with `shlex` and only the
  **write target** is judged (the redirect operand, `of=`, `tee`'s file, `curl -o`,
  `sed -i`'s file…). Judging *any path in the command* — the first attempt — was
  wrong in both directions: `echo pwned > ai.sh` passed (a bare filename has no
  slash, so no path token was found at all) while `grep -rn foo /abs 2>/dev/null`
  was **blocked** (`2>` read as a write, `/abs` as its target). The false positives
  are the worse half: an agent told to "delegate" a `grep` just loops.
- **A `Task` subagent inherits the same wall** (verified: it gets the identical
  block). So "delegate to a subagent" is *not* an escape hatch — an agent told to
  do that just loops. The wall says so explicitly now, and points at
  `delegate-to-local-model`, which runs in its own process and genuinely can write.
- **The agent's own settings file lives under `$AF_ROOT`, which is under `/tmp`** —
  and `/tmp` was allowlisted so the agent could stage scratch work. That let it
  overwrite the very file that installs the wall. `$AF_ROOT` is now carved out.

It is **not a sandbox.** It is a routing enforcer against an agent that forgets,
not a jail against one trying to get out. If you need containment, use permissions.

**It enforces the route, not the location — and those are not the same thing.**
Observed, on a live walled agent asked to write outside its zone: the wall blocked
the Bash write, the agent said *"Wall blocked direct write. Using
delegate-to-local-model"*, and the local model — running in its own process, which
is the entire point of it being the sanctioned route — wrote the file exactly where
it had been asked to. Outside `work/`. So "this agent's writes are confined to
`work/`" is true of the agent and false of the system: anything it delegates can land
anywhere the delegate is told to put it. What the wall reliably buys you is that the
mini-orchestrator *dispatches and verifies* instead of doing the work itself. What it
does not buy you is a boundary on the filesystem.

⚠️ **A hook that can't execute fails OPEN.** Claude Code reports a hook error and
runs the tool anyway — so a `delegate-wall` missing its `+x` bit is a wall-shaped
hole, and nothing in the agent's output says so. `line up` preflights the hooks
and refuses to spawn rather than hand out enforcement that isn't there.

## An agent's spec: what makes `revive` bring back the *same* agent

`--resume` restores an agent's memory. It does not restore its **constitution** —
the role env, the model, the appended system prompt, and the `--settings` file that
installs its hooks. Those lived only in the environment of the process that spawned
it, and died with it. So `ai revive eval` used to return an agent that remembered
being `eval` and had no wall, no role-reminder, and possibly the wrong model —
reporting success, saying nothing.

Every `ai up` now writes a **spec** — one file per agent, in `$HOME` (not `/tmp`,
which a reboot wipes while the manifest in `$HOME` survives — that combination is
exactly how you get a green revive with the guards gone):

```
~/.claude/agent-factory/lines/<slug>/
├── line.json            which blueprint this line came from, who is on it
├── orc.json             ┐ raw:   sid, model, flags, system prompt, role env
├── eval.json            │ links: settings, entrypoint, work, mailbox
└── abl1.json            ┘
├── settings-orc.json    the file that installs the hooks
└── settings-eval.json
```

Raw vs link is one rule: **inline whatever is needed to recreate the agent
identically** (small, immutable — lose it and the agent comes back wrong); **link
whatever is big, live, or regenerable** (copy it and the copy rots). `ai revive`
reads the spec back, and if the settings file has gone missing it regenerates it
rather than reviving an agent whose hooks would simply be absent.

**The spec, not the blueprint, is the source of truth on revive.** The agent's 100k
of context was built under those rules; handing it a system prompt that its own
history contradicts is worse than an out-of-date one. To adopt an edited blueprint,
respawn deliberately (`ai down <name> && line up` — `line up` alone leaves a running
station untouched, and says so).

**`revive` refuses rather than half-restoring.** No spec, a spec that won't parse, a
spec with no launch flags, or a settings file whose hooks can't execute — every one of
those produces an agent that looks healthy and has no wall, so every one of them is a
refusal with a reason, not a warning you'd scroll past. `AI_FORCE=1` if you really do
want the memory back without the role. And the `[wall]` column in `ai ledger` is a live
check of the hooks on disk, not an echo of the spec's own `delegate: required` — the one
view meant to reveal a missing wall used to be reading the wrong column, and always said
the wall was there.

One file per agent, so each file has exactly one writer and there is no
read-modify-write race to lose — the same race that once cost us mail. The single
view is a *derived* one, `ai ledger`, which joins the specs against the live world
(tmux for aliveness, the session log for context size, the mailbox for unread):

```
$ ai ledger
NAME       ROLE           MODEL    PARENT        CTX  MAIL STATE  SESSION
orc        orchestrator   opus     -           142k     0 idle   ● alive
eval       evaluation     sonnet   orc          38k     2 busy   ● alive  [wall]
abl1       ablation       haiku    orc          91k     0 idle   ● alive  [wall]
abl2       ablation       haiku    orc            -     1 -      ○ down (revivable)
```

`line up` is also idempotent now: a station that is already running is left alone.
It used to tear down every live agent's TUI, mid-task, if you re-ran it after
editing one brief.

## Quick start

```bash
# Interactive agent (detached tmux session — nothing pops up)
bash factory/ai.sh up neo
bash factory/ai.sh ask neo "Summarize the README in one line"
bash factory/ai.sh ask neo "What did you just say?"   # remembers — same session
tmux attach -r -t ai-<slug>-neo                  # watch it live, read-only
bash factory/ai.sh down neo                      # quit + kill the session

# Headless worker over a FIFO bus
bash factory/af.sh up worker
bash factory/af.sh say worker "list the risks in worker.sh"
bash factory/af.sh down worker

# Clean up the session logs the agents created
bash factory/afctl.sh purge --dry               # preview
bash factory/afctl.sh purge                      # delete + clear manifest
```

## `ai.sh` commands

```
up [name]            launch interactive Claude TUI in a detached tmux session
say [name] "text"    type text + submit (don't wait)
ask [name] "text"    say + wait until the agent finishes + print its screen
approve [name] [1|2|3]answer a tool-permission prompt (default 2 = allow & don't ask)
ctx [name]           estimated context size (tokens)
compact [name]       run /compact to shrink context (idle only; your call past ~200k)
remote [name]        (re)launch with Remote Control — drive it from the Claude web/app
revive [name] [id]   relaunch a downed agent with its memory AND its role/hooks/model
revivable            list downed agents (surviving log) you can revive by name
ledger               one view of the line: role, model, ctx, unread mail, alive?
screen [name]        dump the current TUI screen
keys [name] <keys>   send raw tmux keys (Escape, C-c, …)
attach [name]        print the tmux attach command for a human viewer
down [name]          quit the agent + kill its session
list                 list running interactive agents
```

## How it works, and why it's built this way

- **Long-lived sessions** come from native flags: `claude --session-id <uuid>`
  (so we know/record the id) + `--resume`. The factory supplies the transport
  and orchestration the CLI leaves to you.
- **An agent is a detached tmux session, and nothing else pops up.** To watch one,
  attach: `tmux attach -r -t ai-<slug>-<name>` (`-r` = read-only, which is what you
  want while this session is driving it — two writers on one pane interleave
  keystrokes and corrupt the input). `ai attach <name>` prints both commands.
  Earlier versions auto-opened a Terminal.app window via AppleScript `do script`.
  That path is **gone**: it only ever worked on Terminal.app and iTerm — never in
  Warp, over ssh, or on Linux — needed macOS Automation permission to not fail
  silently, and left a window to clean up on `down` (an orphaned window drops to a
  bare login shell when the session under it dies). tmux was already the substrate
  underneath it; now it is the only one. `-w/--window` is accepted and ignored.
- **`ask` knows when the agent is done** by watching its live generation timer
  (`✻ Computing… (4s · …)`) appear then vanish — not by diffing the whole screen
  (the footer/token-counter churn would never settle).
- **Spawned agents skip permission prompts by default.** `up`/`revive`/`remote`
  add `--dangerously-skip-permissions` so an unattended agent doesn't stall on
  the first tool gate. Set `AI_SKIP_PERMS=0` (or pass your own `--permission-mode`)
  to restore prompting; then `ask` surfaces the pause and `approve` answers it.
- **Slash commands work** — the agent is a real TUI, so `say <name> "/model"` etc.
  run them. Two are wired in for timing: `compact` and `remote` (relaunch with
  `--remote-control`, resuming the session, so the human drives it from the Claude
  web app/phone).
- **Compaction distinguishes a turn boundary from a task boundary.** A task spans
  many turns; compacting in the middle of one is exactly what throws away the
  working state the agent still needs. The mail protocol already carries the
  signal — a `task` goes out, a `done`/`result` comes back — so that is what gates
  it, rather than a second mechanism invented for the purpose:
  - `AI_COMPACT_SOFT` (200k): compact only *between* tasks. Still owes a `done`?
    Leave it alone and just report the size.
  - `AI_COMPACT_HARD` (500k): compact at the next *turn* boundary regardless.
    Losing some working state is bad; running out of context loses everything.

  Both are absolute token counts — override per agent, since a 200k-window model
  needs far lower numbers than a 1M one.
- **One writer at a time.** An attached human and the controller type into the
  same pane; keystrokes interleave and corrupt the input. To watch only, attach
  read-only: `tmux attach -r -t ai-<slug>-<name>`.
- **Clean teardown.** `down` is `tmux kill-session` — the whole agent, gone. Any
  attached viewer is detached by tmux itself. Re-`up`/`revive` of the same name
  kills the old session first, so relaunching is idempotent.
- **Resume chooser handled.** Resuming a large/old session, claude pauses on
  `1. Resume from summary / 2. Resume full session as-is`; `revive` auto-answers
  it (default 2 = full memory; `AI_RESUME_MODE=1` for the summary).
- **Filterable logs.** Every spawned agent's `--session-id` is recorded in
  `~/.claude/agent-factory/manifest.tsv`, so `afctl purge` removes exactly the
  factory logs and never your manual sessions.
- **Mail is the channel between agents — the push carries a doorbell, not the
  letter.** `mail.sh` appends the message to the recipient's mailbox
  (`.ai/<slug>/mail/<agent>.jsonl`) and types one fixed, path-free command into
  their pane: `!bash $AF_MAIL read`. The payload never goes through the keyboard,
  so quotes, backslashes, regexes and newlines survive intact. Three properties
  fall out of the TUI's own behaviour, each verified on a live agent:
  - `!cmd` runs the command **and** triggers a model turn, so one keystroke burst
    both delivers and wakes — no tool-call spent deciding to go look.
  - Typed at a **busy** agent it queues and fires exactly at the turn boundary, so
    mail can never interrupt work in progress.
  - `$AF_MAIL` expands (shell mode inherits the agent's env), so the typed text
    holds no slash and the file-autocomplete popup — which silently swallows the
    Enter — never opens.

  The mailbox **cursor is the ack**: `mail read` advances it, so a sender can see
  its message is still unread and ring again. Nothing is lost if the recipient was
  dead, busy, or restarted mid-delivery.
- **Mailboxes are per-slug.** They used to be one global inbox shared by every
  project on the machine, which meant a session in one repo could be woken with —
  and told to answer — another repo's escalations. (It happened.) `.ai/<slug>/mail/`
  makes that impossible by construction.
- **Clear the input box with `C-u`, never with `Escape`.** Both clear the line (and a
  stray autocomplete popup with it) — and you must clear it, or your text concatenates
  with whatever is sitting there (`❯ leftover junkREAL MESSAGE`). But **Escape also
  cancels the turn in progress** ("Interrupted · What should Claude do instead?"),
  while `C-u` leaves a running turn alone: verified live, an agent hit with `C-u`
  mid-generation finished its answer. Everything that types into an agent now sends
  `C-u` first, unconditionally — which also retired a whole class of bug, since the
  old code could only Escape when a *screen read* said the agent was idle, and a
  just-rung agent has not painted its timer yet: it read as idle, and its work died.
  Clear *before* typing, never after — clearing after wipes the command you just sent.
- **Keep generated system prompts ASCII.** They travel through bash's `printf %q`,
  and bash 3.2 (what macOS ships) mangles non-ASCII there — an em dash came out as
  one raw byte plus two escaped ones. Downstream, BSD `sed` hit that byte, aborted
  with *illegal byte sequence*, and printed **nothing**; the empty result became the
  agent's saved flags. The spec looked fine — every other field was right — and the
  agent would have revived with no model, no system prompt and no hooks. One em dash
  in a prompt, and the wall is gone. (Both ends are fixed: the prompts are ASCII, and
  the flag parsing is byte-level with no locale in the middle.)

## Packaged as a skill

The toolkit is wrapped as a Claude Code skill, so phrases like "spawn an agent",
"open a second claude", "talk to it", or "clean up the agent logs" invoke it
automatically. `SKILL.md` — the file the model actually reads — lives **here, in
the repo**; the installed skill is two symlinks into it:

```bash
mkdir -p ~/.claude/skills/agent-factory
ln -s "$PWD/SKILL.md" ~/.claude/skills/agent-factory/SKILL.md
ln -s "$PWD/factory"  ~/.claude/skills/agent-factory/scripts
```

Both point back at the repo on purpose. A skill whose instructions live only in
`~/.claude` drifts away from the code it describes and is lost on reinstall — and a
doc that lies about the code is a defect, since a model reads it and then runs the
commands it names.

## Files

```
factory/
├── line.sh         a whole fleet from one blueprint.yml (roles, hierarchy, models)
├── ai.sh           interactive TUI agents (primary)
├── mail.sh         agent↔agent transport: mailbox + doorbell + cursor-as-ack
├── notify.sh       thin alias for `mail.sh send` (stable entry point for older agents)
├── hooks/
│   ├── role-reminder.sh          restates role + chain of command every prompt
│   ├── delegate-wall.sh          denies a mini-orchestrator's direct edits
│   └── escalation-stop-hook.sh   wakes an idle orchestrator when mail arrives
├── af.sh           headless FIFO-bus workers
├── worker.sh       the worker loop af.sh launches
├── afctl.sh        session-log cleanup (manifest-based)
├── orchestrator.sh REPL for the af.sh demo
├── send.sh         one-shot send to an af.sh worker
├── start.sh        two-pane tmux demo (worker + orchestrator)
└── README.md       factory-level docs
```
