# Squads — a whole team, not one agent

Read this when the user wants a **team** of agents (a hierarchy, roles, parallel
experiments, ablations) rather than a single peer. Everything in `SKILL.md` about
driving one agent (`up`/`ask`/`say`/`ctx`/`compact`/`revive`/…) still applies to
**each station** in a squad — this file only covers what changes when there is more
than one.

## Invoked as `/agent-factory plan …` — run the blueprint wizard

If the skill was invoked with `plan` (with or without a goal after it), or the user
asks to design a team *with* you rather than hand you a finished blueprint: **read
`$SKILL/scripts/blueprint-wizard.md` and follow it.** It is an interview protocol —
you ask, they decide, and a `blueprint.json` exists only after they approve the
resolved plan. Do not improvise a blueprint from the schema snippet below; the wizard
carries the briefs, the traps, and the validation step.

`$SKILL/scripts/blueprint.example.json` is a complete team to copy from.

## `af squad` — plan, spawn, watch, tear down

```bash
af squad plan   blueprint.json   # resolved roles/hierarchy/models — no spawn. Show this first.
af squad up     blueprint.json   # generate briefs + settings, spawn every station (+ start the daemons)
af squad status blueprint.json   # alive? context size? unread mail?
af squad down   blueprint.json   # stop the team
af squad add    blueprint.json <name>   # spawn ONE new station already in the blueprint — rest left alone
af squad remove blueprint.json <name>   # kill it, drop it from the roster AND the blueprint
af sweep                         # the context guard, on demand — the daemons also run it on a clock
```

(`af squad up --resume blueprint.json` brings each station back **on its old session**,
so a team keeps its memory across a respawn.)

To grow or shrink a live team: edit `agents:` in the blueprint (add a station, or delete
one by hand) then `af squad add blueprint.json <name>` / `af squad remove blueprint.json
<name>`. `squad up` would also pick up a newly-added station on a re-run — it skips
anyone already alive — but `add`/`remove` do it without touching, or printing a line
about, every other station on the team.

The blueprint is plain JSON — no third-party parser, so `af` stays stdlib-only:

```json
{
  "slug": "rlhf-exp",
  "work": "./work",
  "defaults": {
    "model": "sonnet",
    "caveman": true,
    "bulk_lines": 40,
    "compact_soft": 200000
  },
  "agents": {
    "orc":  { "role": "orchestrator", "model": "opus", "delegate": "no", "brief": "Own the experiment. Dispatch to eval/abl*." },
    "eval": { "role": "evaluation", "parent": "orc", "brief": "Own metrics, baselines, eval scripts." },
    "abl":  { "count": 3, "role": "ablation", "parent": "orc", "model": "haiku", "brief": "Test exactly ONE hypothesis." }
  }
}
```

`work` is each agent's own scratch/report dir; `caveman` is terse output, enforced by a hook;
`bulk_lines` is what counts as a BULK write (default 40); `compact_soft` is the context guard.

`bulk_lines` / `compact_soft` / `compact_hard` are read **from `defaults:`** (and the
compact pair also per-agent). A top-level `bulk_lines:` is silently ignored.

**`squad up` leaves a station that is already running ALONE — it does not apply
blueprint edits.** Editing a brief and re-running `squad up` changes nothing for a
live agent; `squad down` it (or the whole team) first, then `up`.

Roles are arbitrary — `role:` is a free-text string, `orc`/`eval`/`abl` above are
just an example. Two keys are load-bearing: the station whose `role:` is
`orchestrator` is the default `parent` for everyone else, and `count: N` expands a
station into `abl1..ablN` sharing one brief.

## Topology — only the orchestrator spawns full agents

**One hard invariant: below the root, a station delegates — it does not spawn its
own sub-team.** A worker three levels down running `af up` to grow its own squad
defeats the whole point of a tree with one root; "go deeper via a Task subagent or
delegate-to-local-model" only actually happens if the alternative doesn't work.

`spawn-gate` (`PreToolUse` on `Bash`, matching `af up`) enforces it: a station whose
`AF_ROLE` is `orchestrator` may spawn; every other role is denied, with a message
naming the two sanctioned routes (Task subagent, `delegate-to-local-model`) and a
third (mail the orchestrator if a new full station is genuinely needed). Like every
other hook here it is a **router, not a sandbox** — it catches the ordinary ways an
agent invokes its own CLI, not an agent that goes out of its way to obscure the
command.

**The human is not gated.** Talking to the orchestrator is the normal path, but `af
post <any-station>` stays open — no access control on the human's own channel.

## Squad state — one roster, kept honest

`squad.json` (`~/.claude/agent-factory/specs/<slug>/squad.json`) is the durable,
flock-guarded record of who is on the team: role, parent, model, live session id,
status (`planned`/`alive`/`down`/`limited`), and unread-mail count — plus which
blueprint the team came from and when it was first brought up. It replaces the old
assumption that the blueprint file is the only truth — stations can come and go
after `squad up` and this stays current.

It is kept honest two ways: every `af up`/`af down`/session-start/heal path writes
its own station's row the moment something changes, and the **postmaster** daemon
(below) reconciles the whole roster against ground truth (tmux, `ps`, mailbox
counts) on a clock, so a station that died without anyone calling `af down` doesn't
sit marked "alive" forever. **This is internal state, not (yet) a user-facing
view** — for a human-readable status, `af ledger` and `af squad status` are still the
commands to run; they read specs and live probes directly, the same as always.

## Making delegation actually happen

Telling a mini-orchestrator to delegate decays like every other instruction stated
once in a prompt. No `af delegate` wrapper here on purpose — it would couple this
project to one skill's exact CLI shape instead of just naming the routing rule:

- **simple or bulk/mechanical work** (many items, boilerplate, spec-code, first
  drafts, big logs) → the `delegate-to-local-model` skill, called directly.
- **tests and review** → a Haiku subagent.
- **anything needing real judgment, architecture, or a big change** → Sonnet/Opus
  (yourself, or a Task subagent on that tier).

Two levers move the incentive to the tool boundary instead of relying on the
instruction alone:
- **`Context: NN%`** is appended to the `role-reminder` line on every prompt — the
  same number Claude Code's own UI shows, read straight off the pane. The cost of
  *not* delegating stops being invisible turn-to-turn.
- **`read-wall`** (`PreToolUse` on `Read`) denies an unbounded read (no `limit`) of a
  file over `AF_READ_WALL_LINES` lines (default 500) — the single biggest context
  sink otherwise has nothing standing in its way. Bounded reads (`offset`/`limit`)
  always pass. The escape hatch is `af read-force <path>`: a **one-shot** override
  for a read that genuinely needs the whole file, consumed the moment it's used —
  never a standing allowlist entry.

`delegate:` (per-station, in the blueprint) is the OTHER lever — the write-side wall
described below — and is unrelated to the read-wall above; the two guard opposite
directions (writing out vs. reading in).

### `delegate:` — three levels

| value | behaviour |
|---|---|
| `no` | no wall, no advice. For the top orchestrator, who may act directly. |
| `advised` | **the default.** Never blocks. A **bulk** write (≥ `bulk_lines`) to a walled path gets a note in the model's context suggesting `delegate-to-local-model`. Small surgical edits it just makes itself. |
| `required` | hard wall: a direct Write/Edit/Bash-write to a walled path is **denied**. The agent must route through the `delegate-to-local-model` skill (a separate process — the one write route the hook cannot see) or mail the peer who owns the area. |

Default is `advised` because a `required` agent farms out a two-line fix to an
external model — pure overhead. Reach for `required` only when a station genuinely
must not touch code itself. A bare `delegate: true` means `advised`. (Aliases are
accepted — `hard`/`block`/`wall`/`full` → required; `advise`/`soft`/`nudge`/`1`/`true`/`yes`/`on`
→ advised; `0`/`false`/`off`/`none` → no. Write the canonical three.)

**An unknown value is fatal** — `squad up` refuses to spawn the team. (`delegate:
requird` used to mean "no hook at all", i.e. a typo silently produced an unwalled
agent — failing open past even the default.)

**What "walled path" means, exactly.** Freely writable: the agent's own `work/` dir,
and scratch (`/tmp/**`, `/var/folders/**`) — walling scratch off would block the very
delegation the wall demands, since that's where a delegating agent stages prompts and
inspects output. Carved back OUT of that scratch allowance: `$AF_ROOT` (default
`/tmp/agent-factory`), because the agent's own `--settings` file lives there and
writing to it would let the agent **disarm its own wall**. Everything else — the repo
included — is walled.

**Know what the wall does and doesn't do:**

- It bounds **who writes, not where the bytes land.** The delegated local model
  writes wherever it is told, including outside `work/`. The wall enforces the
  *route*, not the *location*.
- **An agent's claim that it delegated is not evidence that it did.** Check the
  transcript, not the report.
- Conversely, **a file appearing is not evidence the wall broke.** The sanctioned
  route produces files too.

Roles/wall/`caveman: true` are all **enforced by hooks, not requested in a prompt** —
a prompt decays, a hook checked at the tool boundary does not:

- **`role-reminder`** (`UserPromptSubmit`) re-states identity, chain of command,
  standing orders, and `Context: NN%` on **every** prompt, for ~30 tokens.
- **`delegate-wall`** / **`spawn-gate`** / **`read-wall`** (`PreToolUse`) decide what
  a station may write, spawn, and read.
- **`caveman: true`** keeps output terse — a team's token cost is dominated by
  agents talking to each other.

## Mail — how agents talk

```bash
af post <agent> [--kind K] "text"   # mail an agent + ring its doorbell — THE way to talk to one
af mail                             # read YOUR mailbox (advancing the cursor acks it)
af mailstat                         # unread per mailbox — the ack/retry signal
af register-self                    # run INSIDE the orchestrator's tmux: let agents wake you
```

Kinds: `task`, `blocked`, `question`, `result`, `done`, `fyi`. **`af post`
defaults to `task`** (pass `--kind` to override); the agent-to-agent form
`bash $AF_MAIL send` defaults to `fyi`.

Three kinds have side effects for the `af ledger` display: `task` marks the
recipient **busy**, and a `done`/`result` marks it **idle** again — but only when
addressed back to **the agent that tasked it**. This busy/idle signal is purely
informational (`af ledger` display only); it does not affect compaction at all.

**The push carries a doorbell, not the letter.** The transport (module
`af.mailbox`, reachable by agents as `bash $AF_MAIL send/read`) appends the message
to the recipient's mailbox and types one fixed, path-free command into their pane:
`!bash $AF_MAIL read`. The payload never goes through the keyboard, so quotes,
backslashes, regexes and newlines arrive intact.

1. `!cmd` **runs the command and triggers a model turn** — one keystroke burst both
   delivers and wakes.
2. Typed at a **busy** agent it **queues and fires at the turn boundary** — mail can
   never interrupt work in progress.
3. `$AF_MAIL` expands in shell mode, so the text has no slash and the
   file-autocomplete popup (which silently swallows the Enter) never opens.

**The cursor is the ack.** `mail read` advances it, so a sender can see its message
is still unread and ring again. The mailbox is guarded by `fcntl.flock` — bounded
wait, no steal-on-timeout: a reader that can't get the lock within the wait budget
raises rather than barging in on a live holder.

**Agents escalate to you.** Every spawned agent gets its name, its parent, `$AF_MAIL`
and standing orders: on a real blocker it can't resolve, it mails instead of stalling
silently. Agents also mail **each other** (`bash $AF_MAIL send --to <peer>`), so work
moves between stations without going through you.

**To be woken directly, run `af register-self` once from inside the orchestrator's
own tmux session.** Launch yourself with all three vars — without
`AF_SLUG`/`AF_MAILROOT` the orchestrator reads the **wrong mailbox**:

```bash
tmux new -s <slug>-lead 'AF_MAIL=<skill>/scripts/mail.sh AF_MAILROOT=/tmp/agent-factory/.ai/<slug>/mail AF_SLUG=<slug> claude'
```

**Unregistered, you are not woken at all.** With no registered pane, a send resolves
the orchestrator to a session that doesn't exist for a human-launched session, so it
**QUEUES**. You then only see mail when you go looking (`af mail`), via the unread
nudge printed after `ask`/`list`, or via the Stop hook
(`hooks/escalation-stop-hook.sh`: on `Stop`, if mail is waiting your turn continues
with `{"decision":"block"}`; otherwise you stop for real — it never holds your turn
open waiting for work that hasn't arrived).

Mailboxes are per-slug (`$AF_ROOT/.ai/<slug>/mail/`), so one project's escalations
can't wake a session in another.

## The two daemons that watch an unattended team

Two separate daemons, two separate jobs, both started **with** the team by `af squad
up` (idempotent — re-running it does not start a second one of either):

### The warden — `af warden` (context + the 5-hour limit)

```bash
af warden watch                 # squad-wide (default): every station of THIS slug
af warden watch --target <sess> # standalone: guard ONE tmux session outside any squad
af warden status [--target …]
af warden stop    [--target …]
```

**Context.** `sweep` only ever fired from `af post` / `af mail` / `af sweep` — i.e.
only while the *driving session* was speaking. A team working autonomously never
goes through those (the agents mail each other), so it was never compacted at all —
observed: a station reached 767k tokens against a 500k hard threshold overnight,
with nothing to trip it. The warden runs the same context guard on a clock instead,
compacting any sweepable agent past its threshold, busy or idle — no task-boundary
prediction, purely measured context size (see `SKILL.md`'s "Context" section).

**The 5-hour limit is account-wide.** It kills every agent *and* the orchestrator
session driving them, at the same instant — so the rescuer can't be a Claude, there
is no Claude left. The warden is a Python loop that spends no tokens: a `StopFailure`
hook (matcher `rate_limit`) marks who was cut off mid-work, `statusline.sh` is the
only channel that carries the exact reset time out of a live session, and the warden
mails each cut-off agent once the window reopens. In squad-wide mode it now pokes
**only the orchestrator** directly — every other cut-off station just has its
recovery watched (no keystrokes sent to it); once the orchestrator itself recovers,
it re-drives the rest of the team by mail, same as any other unattended recovery.

**Standalone mode** (`--target <tmux-session-name>`) guards a single bare tmux
session that isn't part of any squad at all — e.g. a human's own `claude` running
inside tmux. It is **tmux-only, no exceptions**: a target that doesn't resolve to a
live tmux session is refused outright, never silently "watched" some other way,
because both compaction and limit-rescue require typing into the pane. It also
refuses if that pane's `claude` process carries no `--session-id`/`--resume` in its
own argv — there is genuinely no ground truth to bind to for a bare invocation, and
the fix is `claude --session-id $(uuidgen)`, not a guess (this codebase never infers
a session by log mtime or similar — ground truth over prophecy, always).

### The postmaster — `af postmaster` (squad state + a mail safety net)

```bash
af postmaster watch
af postmaster status   # pid, last reconcile error (if any), last ring-catch
af postmaster stop
```

On a much shorter clock than the warden's, it reconciles `squad.json` against ground
truth (see "Squad state" above) and catches missed mail doorbells — a message that
arrived but whose `!bash $AF_MAIL read` doorbell got lost is picked up on the next
tick rather than sitting unread forever. This is a safety net, not a replacement for
the synchronous send-and-ring path, which still has to work standalone.

## Timers — `af polling` (the same message, on a clock)

```bash
af polling start <agent> <minutes> "<message>" [--times N] [--kind K]
af polling stop  [agent]        # no agent = $AF_AGENT: an agent switching ITS OWN timer off
af polling list                 # every timer on this team
af polling status <agent>
```

For an agent that must keep checking something: a deploy that takes twenty minutes,
a queue that drains, a colleague's branch that will eventually land. Instead of a
human re-poking it, a timer re-pokes it.

**It delivers by MAIL, never by typing** — appending to a mailbox is always safe
regardless of what the agent's pane is doing; typing on a clock is not.

Three ways a timer turns on you, and what stops each:

- **It outlives its agent.** The loop records the agent's `sid` and re-checks it
  every tick; a changed sid means a *different* agent took the name, so the timer
  exits rather than hand a ghost's orders to a stranger.
- **It outruns its agent.** A tick is skipped while the previous one is still
  unread — never more than one outstanding message. Floor is 1 minute
  (`AI_POLL_MIN` to override).
- **Everyone forgets it exists.** `--times N` bounds it, `polling list` shows every
  live one, and the agent can switch itself off (`bash $AF_POLL stop`, no argument).
