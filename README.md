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
| **`factory/ai.sh`** | A real interactive Claude **TUI** in a visible Terminal.app window, driven via `tmux send-keys` and read via `tmux capture-pane`. | You want to *see* a real Claude working, or a tool-using agent. Default. |
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
- **delegate-wall** (`PreToolUse` on Write/Edit) *denies* a mini-orchestrator's
  direct edits outside its `work/` dir and names the way out. Observed in
  testing: the agent hit the wall and immediately re-routed to
  `delegate-to-local-model` on its own. Delegation stops being a preference.

⚠️ **A hook that can't execute fails OPEN.** Claude Code reports a hook error and
runs the tool anyway — so a `delegate-wall` missing its `+x` bit is a wall-shaped
hole, and nothing in the agent's output says so. `line up` preflights the hooks
and refuses to spawn rather than hand out enforcement that isn't there.

## Quick start

```bash
# Interactive agent in a visible window
bash factory/ai.sh up neo                       # opens a Terminal window
bash factory/ai.sh ask neo "Summarize the README in one line"
bash factory/ai.sh ask neo "What did you just say?"   # remembers — same session
bash factory/ai.sh down neo                      # quit + close the window

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
up [name]            launch interactive Claude TUI in a Terminal window
say [name] "text"    type text + submit (don't wait)
ask [name] "text"    say + wait until the agent finishes + print its screen
approve [name] [1|2|3]answer a tool-permission prompt (default 2 = allow & don't ask)
ctx [name]           estimated context size (tokens)
compact [name]       run /compact to shrink context (idle only; your call past ~200k)
remote [name]        (re)launch with Remote Control — drive it from the Claude web/app
revive [name] [id]   relaunch a downed agent with its memory (resumes its session)
revivable            list downed agents (surviving log) you can revive by name
screen [name]        dump the current TUI screen
keys [name] <keys>   send raw tmux keys (Escape, C-c, …)
attach [name]        print the tmux attach command for another viewer
down [name]          quit the agent + close its window
list                 list running interactive agents
```

## How it works, and why it's built this way

- **Long-lived sessions** come from native flags: `claude --session-id <uuid>`
  (so we know/record the id) + `--resume`. The factory supplies the transport
  and orchestration the CLI leaves to you.
- **Visibility is terminal-specific.** Terminal.app and iTerm expose AppleScript
  `do script`, so `up` auto-opens a window. **Warp, Linux, and ssh have no
  scriptable spawn and block keystroke injection**, so they fall back to a
  detached **tmux** session you attach to (`tmux attach -t ai-<name>`). tmux is
  the portable substrate underneath everything. Override with
  `AF_VIEW=macos|iterm|tmux`.
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
- **One writer at a time.** The agent shares its tmux session with the window;
  if a human types while the controller drives, keystrokes interleave. To watch
  only, attach read-only: `tmux attach -r -t ai-<name>`.
- **Clean teardown.** `down` closes the window by killing its tty process, not
  AppleScript `close` (which pops a modal "terminate?" sheet that can't be
  dismissed headlessly). Re-`up`/`revive` of the same name closes that name's old
  window first, so relaunching never leaves an orphaned bare-shell window behind.
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
- **Escape clears the input line, it does not just close a popup.** Anything that
  types into a TUI must Escape *before* typing, never after: a post-typing Escape
  wipes the command you just sent.

## Packaged as a skill

The toolkit is wrapped as a Claude Code skill at
`~/.claude/skills/agent-factory/` (its `scripts/` symlinks to `factory/`), so
phrases like "spawn an agent", "open a second claude", "talk to it", or "clean
up the agent logs" invoke it automatically.

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
