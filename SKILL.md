---
name: agent-factory
description: >-
  Spawn and talk to long-lived SECONDARY Claude agents from this session — real
  peer agents in their own tmux sessions, not ephemeral subagents/Tasks. Use this
  whenever the user wants to launch/spawn/open another agent or "a second
  Claude", drive or chat with a peer/worker agent, run agents that talk to each
  other, revive a killed agent, or list/clean up previously spawned agents and
  their session logs. ALSO use it to stand up a whole TEAM of agents — a LINE — with
  roles and a hierarchy (orchestrator + workers, parallel multi-agent experiments,
  ablations) — that is `line.sh` with a blueprint.yml. Invoked as `/agent-factory
  plan <goal>` it runs an INTERACTIVE blueprint wizard: it interviews the user,
  proposes the stations and their briefs, and writes the blueprint.yml with them.
  Trigger even if the user just says "spawn an agent", "open another claude", "talk
  to it", "kill that agent", "design a team of agents", or "clean up the agent
  logs" without naming this skill.
---

# Agent Factory

Lets THIS Claude session spawn independent, long-lived Claude agents and converse
with them across many turns. They are NOT subagents: each has its own context
window and session, persists across your turns until explicitly `down`ed, and can
be watched live by the human.

- **`line.sh` — a whole line from one `blueprint.yml`.** Roles, chain of command,
  per-agent model, enforced delegation. Use this the moment the user wants a *team*
  rather than an agent. See "Lines".
- **`ai.sh` — a real interactive Claude TUI** in a detached tmux session. You drive
  it with `tmux send-keys` and read it with `tmux capture-pane`. **Default for a
  single agent.**
- **`mail.sh` — the channel between agents.** Mailbox + doorbell + cursor-as-ack.
  Never type a message into another agent's pane by hand; send it as mail.

Scripts live in `scripts/` next to this file. Reference them by absolute path:
`bash "$SKILL/scripts/ai.sh" ...`, where `$SKILL` is this skill's dir.

## Invoked as `/agent-factory plan …` — run the blueprint wizard

If the skill was invoked with `plan` (with or without a goal after
it), or the user asks to design a line *with* you rather than hand you a finished
blueprint: **read `$SKILL/scripts/blueprint-wizard.md` and follow it.** It is an
interview protocol — you ask, they decide, and a `blueprint.yml` exists only after they
approve the resolved plan. Do not improvise a blueprint from this file's schema snippet;
the wizard carries the briefs, the traps, and the validation step.

`$SKILL/scripts/blueprint.example.yml` is a complete, annotated line to copy from.

## Nothing pops up: an agent is a tmux session

`up` creates a **detached tmux session** `ai-<slug>-<name>` and nothing else. No
window opens. A human who wants to watch runs:

```bash
tmux attach -r -t ai-<slug>-<name>    # -r = READ-ONLY — what you want while you drive it
```

Tell the human that command when they ask to see an agent; `ai.sh attach <name>`
prints it. **Read-only matters:** you drive the agent by typing into its pane with
`send-keys`, so a human typing into the same pane interleaves keystrokes and
corrupts the input. One writer at a time.

(Older versions auto-opened a Terminal.app window. That is gone — `-w/--window` is
accepted and ignored.)

**`<slug>`** is a short per-project tag (from the working-dir name, or `$AF_SLUG`),
so agents from different projects never collide in `tmux ls` or in state:
`ai-linkai-worker`, `ai-inna-scout`. You address agents by the short `<name>`; the
slug is applied for you. `ai.sh slug` prints it.

## Single agent — `ai.sh`

```bash
ai.sh up     [name]          # launch a Claude TUI in a detached tmux session
ai.sh ask    [name] "text"   # say + wait for the turn to finish + print the result  ← use this
ai.sh say    [name] "text"   # type + submit, don't wait
ai.sh wait   [name]          # block until the agent is idle or needs input
ai.sh screen [name]          # dump the current TUI screen
ai.sh result [name]          # last completed turn's text (from the session log)
ai.sh ctx    [name]          # estimated context size (tokens)
ai.sh compact[name]          # run /compact (idle only)
ai.sh sweep                  # compact idle agents past their threshold; reap stale
                             #   busy flags. `post`/`mail` run it for you.
ai.sh remote [name]          # relaunch with Remote Control so the human drives from the Claude app
ai.sh approve[name] [1|2|3]  # answer a tool-permission prompt (default 2)
ai.sh keys   [name] <keys>   # raw tmux keys (Escape, C-c, …)
ai.sh attach [name]          # print the tmux attach command for a human viewer
ai.sh down   [name]          # quit the agent + kill its session
ai.sh list                   # running agents
ai.sh slug                   # the current project slug
ai.sh ledger                 # THE status view — see "Spec and revive"
ai.sh revive [name]          # bring a killed agent back with memory AND role/hooks
ai.sh revivable              # who can be revived
```

Mail (see "Mail" below): `ai.sh post <agent> [--kind K] "text"`, `ai.sh mail`
(`inbox` is an alias), `ai.sh mailstat`, `ai.sh register-self`,
`ai.sh unregister-self`.

Typical flow — always `ask`, not `say`, when you want the reply:

```bash
SKILL="$HOME/.claude/skills/agent-factory"
bash "$SKILL/scripts/ai.sh" up neo
bash "$SKILL/scripts/ai.sh" ask neo "Summarize the README in one line"
# ... more turns; the agent remembers (same session) ...
bash "$SKILL/scripts/ai.sh" down neo
```

### What you must know to drive it well

- **`ask` returns when the agent is actually done** — it waits for a new `end_turn`
  record in the agent's session log AND for the live generation timer
  (`✻ Computing… (4s · …)`) to vanish. The log is the authority precisely because
  the screen isn't: the footer and token counter churn constantly, so polling the
  screen for "stability" never settles. Use `ask`; it already does all of this.
- **`ask` gives up after `AI_TIMEOUT` seconds (default 300)** and tells you the
  agent is still working — it does not kill the turn. Raise it for long tasks
  (`AI_TIMEOUT=1800 ai.sh ask …`) or come back with `ai.sh wait` / `ai.sh result`.
- **The first turn is slow** (~5–6s of session init after `up`). `ask` waits.
- **Spawned agents skip permission prompts** (`--dangerously-skip-permissions` on
  `up`/`revive`/`remote`): they run unattended, so otherwise they'd stall forever
  on the first tool gate with nobody to approve. They can read/write/run without
  asking — only spawn them on work you'd let run on its own. `AI_SKIP_PERMS=0`
  restores prompting; then `ask` surfaces `⚠ paused on a permission prompt` and
  `ai.sh approve <name>` answers it.
- **After `up`, confirm the agent is alive** (`screen` shows the TUI, or `ask`
  returns) before reporting success.
- **Don't tear agents down unless asked** — they're meant to be long-lived. Agents
  you spawned for a demo/test: track the names and `down` them when the user says
  you're done.

### Slash commands inside a spawned agent

It's a real Claude Code TUI, so it takes slash commands — `say` one (e.g.
`ai.sh say neo "/model"`). Two are wired in as first-class commands because timing
matters:

- **`/compact` — `ai.sh compact <name>`.** It refuses **mid-generation or on a
  permission prompt** (keystrokes would interrupt the turn or wrongly answer the
  prompt), so it only ever runs at the idle point between turns. Check context size
  anytime with `ai.sh ctx` or `ai.sh ledger`. Mostly you don't need to call it — see
  "Context" below, compaction is automatic.
- **`/remote-control` — hand the human live control from the Claude app or phone.**
  `ai.sh remote <name>` relaunches the agent with `--remote-control <name>`,
  **resuming its session so memory survives**, then tells the user to open the
  Claude app (sign-in required). After that the human drives; you can still read
  along with `ai.sh screen`.

### Context — compaction is AUTOMATIC, on task boundaries

Compaction runs on **task** boundaries, not turn boundaries: a task spans many
turns, and compacting mid-task throws away the working state the agent still needs.
The mail protocol says which is which (a `task` goes out, a `done`/`result` comes
back), and that gates it:

| knob | default | meaning |
|---|---|---|
| `AI_COMPACT_SOFT` | 200000 | past this, compact only **between** tasks; mid-task, just report |
| `AI_COMPACT_HARD` | 500000 | compact at the next **turn** boundary regardless — losing some working state beats running out of context |

Absolute token counts; set either to `0` to disable. Per-station in a blueprint:
`compact_soft:` / `compact_hard:` (in `defaults:` or on one agent).

**Where it fires matters.** `ai.sh` is a script, not a daemon: it can only check an
agent's context at a moment when it happens to hold control right after that agent's
turn. `ask` is such a moment (it waited for the turn). A **line** agent is driven by
*mail* and takes its turns while `ai.sh` isn't even running — so there is no hook
point, and nothing guards it. **`ai.sh sweep` is that guard**: it walks every agent
with a mailbox and compacts the ones past threshold, skipping any that is
mid-generation or paused on a permission prompt (keystrokes would interrupt the turn
or answer the prompt for the human).

**You do not have to remember to call it.** `ai.sh post` and `ai.sh mail` — the two
commands that touch a line — run a sweep themselves (`AI_SWEEP_OFF=1` disables that).
`post` sweeps **before** it sends, and skips the recipient — compacting an agent in the
same breath as handing it a task is a race with nothing to gain. Each agent is judged
by **its own** thresholds, taken from its spec, not the orchestrator's env.

`ai.sh ledger` deliberately does **not** sweep: it's a look, and silently shrinking an
agent's memory out from under someone who came to inspect it isn't what "show me the
line" means. It prints which agents a sweep would compact.

**A sweeper cannot compact itself.** A sweep runs inside the sweeper's own shell, so
typing `/compact` into its own pane would land mid-turn — its own turn. So `sweep`
skips whoever is running it.

For a line's orchestrator station (`orc`) that is usually harmless: when **you** sweep
from the top session, `orc` is just another station and gets compacted like the rest.
The gap is the fully autonomous line — `orc` drives the workers by mail and nobody up
top ever runs `ai.sh`. Then `orc` is the only sweeper, so it is the one agent nothing
guards, and it is the longest-lived agent on the line. For that case `post`/`mail`
print a warning to `orc` when its own context is past threshold: it has to run
`/compact` itself, at a safe point of its choosing.

## Lines — `line.sh` (when the user wants a team)

**No blueprint yet? Do not write one from the snippet below — run the wizard**
(`$SKILL/scripts/blueprint-wizard.md`): interview the human, propose the stations, write
the briefs, validate with `line plan`, then stop. See the top of this file.

```bash
line.sh plan   blueprint.yml   # resolved roles/hierarchy/models — no spawn. Show this first.
line.sh up     blueprint.yml   # generate briefs + settings, spawn every station
line.sh status blueprint.yml   # alive? context size? unread mail?
line.sh down   blueprint.yml   # stop the line
ai.sh   sweep                  # run periodically while the line works — the context guard
```

(`line.sh settings <slug> <name> <out>` also exists; it's internal — `ai revive`
calls it to regenerate a settings file that vanished from under a spec.)

```yaml
slug: rlhf-exp
work: ./work                   # each agent's own scratch/report dir
defaults:
  model: sonnet
  caveman: true                # terse output, enforced by a hook
  bulk_lines: 40               # what counts as a BULK write (default 40)
  compact_soft: 200000         # context guard — see "Context"
agents:
  orc:  { role: orchestrator, model: opus, delegate: no, brief: "Own the experiment. Dispatch to eval/abl*." }
  eval: { role: evaluation, parent: orc, brief: "Own metrics, baselines, eval scripts." }
  abl:  { count: 3, role: ablation, parent: orc, model: haiku, brief: "Test exactly ONE hypothesis." }
```

`bulk_lines` / `compact_soft` / `compact_hard` are read **from `defaults:`** (and
the compact pair also per-agent). A top-level `bulk_lines:` is silently ignored.

**`line up` leaves a station that is already running ALONE — it does not apply
blueprint edits.** Editing a brief and re-running `line up` changes nothing for a
live agent; `line down` it (or the whole line) first, then `up`.

Roles are arbitrary — `role:` is a free-text string, `orc`/`eval`/`abl` above are
just an example. Two keys are load-bearing: the station whose `role:` is
`orchestrator` is the default `parent` for everyone else, and `count: N` expands a
station into `abl1..ablN` sharing one brief.

Roles are **enforced by hooks, not requested in a prompt.** A prompt decays: you
tell an agent its role once, and thirty turns into a debugging session it is not
what the model is thinking about. So:

- **`role-reminder`** (`UserPromptSubmit`) re-states identity, chain of command and
  standing orders on **every** prompt, for ~25 tokens. It cannot drift.
- **`delegate-wall`** (`PreToolUse` on Write/Edit/MultiEdit/NotebookEdit/Bash)
  decides what a mini-orchestrator may write.
- **`caveman: true`** keeps output terse — a line's token cost is dominated by
  agents talking to each other.

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

**An unknown value is fatal** — `line up` refuses to spawn the line. (`delegate:
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
- **An agent's claim that it delegated is not evidence that it did.** One agent
  reported "Delegated to local model. Verified" while its transcript showed
  `delegate.py` had failed and it wrote the file itself with a heredoc. Check the
  transcript, not the report.
- Conversely, **a file appearing is not evidence the wall broke.** The sanctioned
  route produces files too.

## Spec and revive — an agent's constitution

`--resume` restores an agent's **memory**. It does not restore its role, model,
system prompt or hooks. So every spawn writes a **spec** —
`~/.claude/agent-factory/lines/<slug>/agent-<name>.json` — holding everything
needed to recreate it identically: role env, model, launch flags, appended system
prompt, and the settings file that installs its hooks.

```bash
ai.sh ledger      # every agent: alive?, model, role, ctx, unread mail, and a LIVE wall check
ai.sh revivable   # who has a surviving log
ai.sh revive eval # back with memory AND constitution
```

**`revive` refuses rather than half-restoring.** No spec, a corrupt spec, or a spec
with no flags → refusal with a reason (`AI_FORCE=1` overrides). Broken or
non-executable hooks refuse **only for a `required` station**, where the wall is the
point; an `advised` one is revived with a loud warning. A missing settings file is
regenerated, not refused.

This is deliberate, because **in this system safety mechanisms fail silently**: a
hook file that isn't executable doesn't block anything — Claude Code prints an error
and runs the tool anyway. A revived agent that looks fine but has no wall is worse
than one that refused to come back.

For the same reason the wall column in `ledger` is a **live check**, not a copy of
what the spec claims: `[wall]` / `[advise]` / `[advise: hooks broken]` /
`!! NO WALL (hooks missing/not executable)`.

`revive` also auto-clears the resume chooser (`1. Resume from summary / 2. Resume
full session as-is`), defaulting to **2 = full memory**; `AI_RESUME_MODE=1` picks
the cheaper summary.

## Mail — how agents talk

```bash
ai.sh post <agent> [--kind K] "text"   # mail an agent + ring its doorbell — THE way to talk to one
ai.sh mail                             # read YOUR mailbox (advancing the cursor acks it)
ai.sh mailstat                         # unread per mailbox — the ack/retry signal
ai.sh register-self                    # run INSIDE the orchestrator's tmux: let agents wake you
```

Kinds: `task`, `blocked`, `question`, `result`, `done`, `fyi`. **`ai.sh post`
defaults to `task`** (pass `--kind` to override); `mail.sh send` defaults to `fyi`.

Three kinds have side effects, and they are what makes the context guard work:
`task` marks the recipient **busy**, and a `done`/`result` marks it **idle** again —
but only when it is addressed back to **the agent that tasked it**. A `done` fired at
some other peer leaves the sender busy forever (and a permanently-busy agent is never
soft-compacted). That is how compaction tells a task boundary from a mere turn
boundary (see "Context"). So don't hand out work as `--kind fyi` either: the agent
looks idle mid-task and can be compacted out from under itself.

**The push carries a doorbell, not the letter.** `mail.sh` appends the message to
the recipient's mailbox and types one fixed, path-free command into their pane:
`!bash $AF_MAIL read`. The payload never goes through the keyboard, so quotes,
backslashes, regexes and newlines arrive intact. Three properties follow, all
verified on live agents:

1. `!cmd` **runs the command and triggers a model turn** — one keystroke burst both
   delivers and wakes.
2. Typed at a **busy** agent it **queues and fires at the turn boundary** — mail can
   never interrupt work in progress.
3. `$AF_MAIL` expands in shell mode, so the text has no slash and the
   file-autocomplete popup (which silently swallows the Enter) never opens.

**The cursor is the ack.** `mail read` advances it, so a sender can see its message
is still unread and ring again.

**Agents escalate to you.** Every spawned agent gets its name, its parent, `$AF_MAIL`
and standing orders: on a real blocker it can't resolve — a decision only you or the
human can make, a missing secret, an irreversible action, repeated failure — it mails
instead of stalling silently. Agents also mail **each other**
(`mail.sh send --to <peer>`), so work moves between stations without going through
you. (For a `line.sh` line those orders come from the station's brief and system
prompt; `AI_NOTIFY_OFF=1`, which `line.sh` sets, only suppresses `ai.sh`'s own
generic escalation prompt — the mailbox and `$AF_MAIL` are there either way.)

**To be woken directly, run `ai.sh register-self` once from inside the
orchestrator's own tmux session.** Launch yourself with all three vars — without
`AF_SLUG`/`AF_MAILROOT` the orchestrator reads the **wrong mailbox** (slug falls back
to `proj`) and every reply looks lost:

```bash
tmux new -s <slug>-lead 'AF_MAIL=<skill>/scripts/mail.sh AF_MAILROOT=/tmp/agent-factory/.ai/<slug>/mail AF_SLUG=<slug> claude'
```

**Unregistered, you are not woken at all.** Nothing is typed at you: with no
registered pane, `mail.sh` resolves the orchestrator to the session
`ai-<slug>-orchestrator`, which doesn't exist for a human-launched session, so the
send is **QUEUED**, not delivered. You then only see mail when you go looking
(`ai.sh mail`), via the unread nudge printed after `ask`/`list`, or via the Stop
hook. (There *is* a degraded doorbell — typing a literal path the agent must choose
to obey — but that is for an orchestrator that IS registered and whose env lacks
`AF_MAIL`. It is not a fallback for skipping registration.)

Mailboxes are per-slug (`$AF_ROOT/.ai/<slug>/mail/`, default under
`/tmp/agent-factory`), so one project's escalations can't wake a session in another.

### The Stop hook — delivers, never waits

`hooks/escalation-stop-hook.sh` is the fallback for an orchestrator that isn't a
registered tmux pane. On `Stop` it checks your mailbox: if mail is waiting it returns
`{"decision":"block"}` with the message, so your turn continues and you handle it with
no human input. If nothing is waiting, it exits immediately and you stop for real.

**It does not hold your turn open waiting for work to come back.** It used to (45s,
whenever a task was outstanding), and that was a bad trade: the agents are working, and
whatever they produce arrives as mail that wakes you when it lands — so the wait bought
nothing, while a stale "outstanding" flag (a crashed agent, a task queued for an agent
that never came up) stalled *every* idle turn by 45 seconds. Hand control back; the
doorbell is how you hear about it.

## The warden — `warden.sh` (the line, when nobody is driving it)

```bash
warden watch     # started automatically by `line up`; safe to re-run
warden status    # quota, reset time, who got cut off, last sweep
warden stop
```

**It runs the context guard on a clock.** `ai sweep` only ever fired from `ai post` /
`ai mail` / `ai sweep` — i.e. only while the *driving session* was speaking. A line working
autonomously never goes through those (the agents mail each other via `mail.sh`), so it was
never compacted at all. Observed: a station reached **767k tokens against a 500k hard
threshold** overnight, with nothing to trip it. "Automatic" compaction that only fires while
a human is at the keyboard is a manual command with a misleading name.

The 5-hour subscription limit is **account-wide**. It does not stop one agent — it kills
every agent on the machine *and the orchestrator session driving them*, mid-turn, at the
same instant. So the thing that resumes them cannot be a Claude: there is no Claude left.
It has to be a process that spends no tokens and does not care about the limit at all.

That is `limits.sh`: a shell loop that sleeps. Two documented signals, no screen-scraping:

- **`StopFailure` hook** (matcher `rate_limit`, `hooks/limit-hook.sh`) fires at the instant
  a turn is killed by the limit. It cannot block or retry — Claude Code ignores its output
  — but it leaves a marker naming which agents were cut off *mid-work*, as opposed to idle
  and fine.
- **`statusline.sh`** is the only way `rate_limits.five_hour.resets_at` — the exact epoch
  the limit lifts — gets out of a live session. No CLI reports it. Every agent drops it on
  disk; the watcher reads it from there. Without it the watcher knows *who* was cut off but
  not *when* to wake them, and it will say so rather than guess.

At `resets_at + 45s` it mails each cut-off agent (staggered) telling it what happened and
to pick the work back up. It re-checks the pane first: the **7-day** cap can hold you down
long past the 5-hour reset, and waking into that just burns the turn on the same error.

**The killed turn is not recovered.** That API call is gone and its tool call with it. The
agent keeps its full context, so it can resume — but it must be told, and told what
happened, or it just sits there. The wake-up message says so, and tells it to ask rather
than guess if it cannot work out where it was.

Settings changes only take effect at launch: an agent spawned before this existed has no
limit hook and no statusline. Give it one with `ai down <name> && line up --resume <bp>`.

## Timers — `polling.sh` (the same message, on a clock)

```bash
polling start <agent> <minutes> "<message>" [--times N] [--kind K]
polling stop  [agent]        # no agent = $AF_AGENT: an agent switching ITS OWN timer off
polling list                 # every timer on this line
polling status <agent>
```

For an agent that must keep checking something: a deploy that takes twenty minutes, a
queue that drains, a colleague's branch that will eventually land. Instead of a human
re-poking it, a timer re-poks it.

**It delivers by MAIL, never by typing.** The naive build — `sleep N; ai say <agent> "…"`
in a loop — types into a live TUI on a clock, with no idea whether the agent is mid-turn,
mid-tool-call, or sitting on a permission prompt. Mail appends to a mailbox (always safe)
and only rings the doorbell.

Three ways a timer turns on you, and what stops each:

- **It outlives its agent.** The loop records the agent's `sid` and re-checks it every
  tick. A changed sid means a *different* agent took the name — the timer exits rather
  than hand a ghost's orders to a stranger. (A resumed agent keeps its sid, so it keeps
  its timer. A fresh spawn gets a clean slate.)
- **It outruns its agent.** A tick is **skipped while the previous one is still unread** —
  the timer can never build a backlog, one outstanding message ever. The floor is 1 minute
  (`AI_POLL_MIN` to override, deliberately).
- **Everyone forgets it exists.** `--times N` bounds it, `polling list` shows every live
  one, and the **agent can switch itself off** — `bash $AF_POLL stop`, no argument. It is
  the one that knows the wait is over. The first tick tells it so; the rest don't repeat it.

The tick is sent **from whoever started the timer** (you, by default), so the agent's reply
lands in your `ai mail` and not in some fictional `poll` mailbox nobody reads.

## Cleanup — `afctl.sh`

Every spawned agent runs with a known `--session-id <uuid>` recorded in
`~/.claude/agent-factory/manifest.tsv`, so factory logs can be purged without ever
touching the human's own sessions.

```bash
afctl.sh list             # the manifest
afctl.sh sessions         # locate each agent's .jsonl (present/missing)
afctl.sh purge --dry      # preview — ALWAYS show this to the user first
afctl.sh purge            # delete those logs, clear the manifest
```

Deleting logs is irreversible: run `purge --dry` and show the user before a real
`purge`. (`--append-system-prompt` is not written to transcripts, so the manifest —
not any in-log marker — is the durable link to an agent's log.)
