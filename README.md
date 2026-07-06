# agent-factory

An experiment in agent creation, orchestration, and autonomous communication:
let one Claude Code session **spawn long-lived, independent Claude agents in
their own shells and talk to them** across many turns. These are real peer
agents — their own context window and session, persistent until killed — not
ephemeral subagents.

## Two flavors

| Tool | What it is | Use when |
|------|-----------|----------|
| **`factory/ai.sh`** | A real interactive Claude **TUI** in a visible Terminal.app window, driven via `tmux send-keys` and read via `tmux capture-pane`. | You want to *see* a real Claude working, or a tool-using agent. Default. |
| **`factory/af.sh`** | A **headless worker** (`claude -p` loop) driven over a FIFO message bus, persistent `--resume` session. | Programmatic request→reply, autonomous loops, agent-to-agent chains. |
| **`factory/afctl.sh`** | Cleanup of spawned agents' session logs via a session-id manifest. | Purge factory logs without touching your manual sessions. |

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
  run them. Two are wired in for timing: `compact` (a judgment call you make once
  context grows past ~200k, only if it won't drop needed info and the agent will
  keep being used; `ask` reports the size, the command refuses mid-turn, no
  auto-trigger unless you lower `AI_COMPACT_AT` from its 1m default) and `remote`
  (relaunch with `--remote-control`, resuming the session, so the human drives it
  from the Claude web app/phone).
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

## Packaged as a skill

The toolkit is wrapped as a Claude Code skill at
`~/.claude/skills/agent-factory/` (its `scripts/` symlinks to `factory/`), so
phrases like "spawn an agent", "open a second claude", "talk to it", or "clean
up the agent logs" invoke it automatically.

## Files

```
factory/
├── ai.sh           interactive TUI agents (primary)
├── af.sh           headless FIFO-bus workers
├── worker.sh       the worker loop af.sh launches
├── afctl.sh        session-log cleanup (manifest-based)
├── orchestrator.sh REPL for the af.sh demo
├── send.sh         one-shot send to an af.sh worker
├── start.sh        two-pane tmux demo (worker + orchestrator)
└── README.md       factory-level docs
```
