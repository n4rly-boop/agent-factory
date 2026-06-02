# agent-factory

Experiment: one shell spawns a **real** `claude` agent in another shell and
talks to it. Visible (tmux), message bus = FIFOs, glue = bash.

## Pieces

| File | Role |
|------|------|
| `worker.sh`       | Worker agent. Reads tasks from `a2b` FIFO, runs headless `claude -p` with a persistent session, writes reply to `b2a`. Prints everything to its pane. |
| `orchestrator.sh` | REPL. You type tasks; they go to the worker; reply comes back. |
| `send.sh`         | One-shot send from any third shell / script — enables autonomous loops. |
| `start.sh`        | Launches a tmux session: worker pane + orchestrator pane. |

## Run

```bash
bash factory/start.sh
```

tmux opens. Right pane = orchestrator (cursor here). Type:

```
orchestrator> what files are in this repo?
```

Watch the **left** pane: the worker agent receives the task, thinks, replies.
Reply also returns to the orchestrator pane.

Detach tmux: `Ctrl-b d`. Reattach: `tmux attach -t agent-factory`.

## Talk to it from a third shell

```bash
AF_DIR=/tmp/agent-factory bash factory/send.sh "list risks in worker.sh"
```

## Let the worker DO things (not just answer)

By default the worker only answers. To let it edit files / run tools:

```bash
AF_CLAUDE_FLAGS="--permission-mode acceptEdits" bash factory/start.sh
```

## How the bus works

Two named pipes under `$AF_DIR` (default `/tmp/agent-factory`):

```
orchestrator --(a2b)--> worker
orchestrator <--(b2a)-- worker
```

Messages are base64'd to one line each (survives newlines). Worker keeps a
`--resume` session id, so it remembers context across tasks. FIFOs are opened
read+write (`exec 3<>fifo`) so they never EOF when a writer closes.

## Where to take it next

- **N workers**: one request FIFO per worker (`a2b-1`, `a2b-2`…); orchestrator
  routes / load-balances.
- **Agent-to-agent**: give the *worker* a `send.sh` to a third agent — chains.
- **Autonomous protocol**: replace the REPL with a loop that feeds the worker's
  reply back as the next task until a stop token appears.
- **Beyond demo**: swap FIFOs for sockets, or move to the Agent SDK
  (`claude-agent-sdk`) for real routing without screen glue.
```
