---
name: agent-factory
description: >-
  Spawn and talk to long-lived SECONDARY Claude agents from this session — real
  peer agents in their own tmux sessions, not ephemeral subagents/Tasks. Use this
  whenever the user wants to launch/spawn/open another agent or "a second
  Claude", drive or chat with a peer/worker agent, run agents that talk to each
  other, revive a killed agent, or list/clean up previously spawned agents and
  their session logs. ALSO use it to stand up a whole TEAM of agents — a SQUAD — with
  roles and a hierarchy (orchestrator + workers, parallel multi-agent experiments,
  ablations) — that is `af squad` with a blueprint. Invoked as `/agent-factory
  plan <goal>` it runs an INTERACTIVE blueprint wizard: it interviews the user,
  proposes the stations and their briefs, and writes the blueprint with them.
  Trigger even if the user just says "spawn an agent", "open another claude", "talk
  to it", "kill that agent", "design a team of agents", or "clean up the agent
  logs" without naming this skill.
---

# Agent Factory

Lets THIS Claude session spawn independent, long-lived Claude agents and converse
with them across many turns. They are NOT subagents: each has its own context
window and session, persists across your turns until explicitly `down`ed, and can
be watched live by the human.

This file covers **one agent** — spawn it, drive it, read it back, revive it. The
moment the user wants a **team** (roles, a chain of command, parallel experiments,
ablations, agents talking to each other) — read `$SKILL/scripts/squad.md` instead;
everything below still applies to each station in a team, squad.md only covers what
changes when there is more than one.

- **`af up` / `af ask` — a real interactive Claude TUI** in a detached tmux session.
  You drive it with `af say`/`af ask` (which wrap `tmux send-keys`) and read it with
  `af screen`/`af result`. **Default for a single agent.**
- **`af squad` — a whole team from one blueprint (JSON).** Roles, chain of command,
  per-agent model, enforced delegation. See `squad.md`.

## Running `af`

Commands here are written as `af <cmd>`. `af` is **`python3 -m af`** — the stdlib-only
Python core in `factory/af/`. It needs that package importable, so run it either from
the `factory/` dir or with the dir on `PYTHONPATH`. `$SKILL/scripts` is that dir (the
installed skill symlinks `scripts/` → the repo's `factory/`):

```bash
SKILL="$HOME/.claude/skills/agent-factory"
PYTHONPATH="$SKILL/scripts" python3 -m af up neo     # the canonical form
```

**The old `*.sh` names still work** — `ai.sh`, `mail.sh`, `squad.sh`, `warden.sh`,
`polling.sh`, `statusline.sh` and the `hooks/*.sh` are now **6-line shims** that set
`PYTHONPATH` and `exec python3 -m af…` for you, so `bash "$SKILL/scripts/ai.sh" up
neo` is exactly `af up neo` with zero setup. They are kept on purpose and must stay:
a spawned agent's **doorbell** types `bash $AF_MAIL read` and its **hooks/statusline**
are wired by absolute path (`.../scripts/hooks/role-reminder.sh`, `.../statusline.sh`),
so those paths have to keep resolving. `afctl.sh` is still real bash, not a shim.

Present `af` as the primary surface; reach for a `.sh` name only when you want the
no-setup shim or are matching an existing call site.

(Invoked as `/agent-factory plan …`, or asked to design a *team* — that's the
blueprint wizard, see `squad.md`.)

## Nothing pops up: an agent is a tmux session

`up` creates a **detached tmux session** `ai-<slug>-<name>` and nothing else. No
window opens. A human who wants to watch runs:

```bash
tmux attach -r -t ai-<slug>-<name>    # -r = READ-ONLY — what you want while you drive it
```

Tell the human that command when they ask to see an agent; `af attach <name>`
prints it. **Read-only matters:** you drive the agent by typing into its pane with
`send-keys`, so a human typing into the same pane interleaves keystrokes and
corrupts the input. One writer at a time.

(Older versions auto-opened a Terminal.app window. That is gone — `-w/--window` is
accepted and ignored.)

**`<slug>`** is a short per-project tag (from the working-dir name, or `$AF_SLUG`),
so agents from different projects never collide in `tmux ls` or in state:
`ai-linkai-worker`, `ai-inna-scout`. You address agents by the short `<name>`; the
slug is applied for you. `af slug` prints it.

## Single agent — `af`

```bash
af up      [name]          # launch a Claude TUI in a detached tmux session
af ask     [name] "text"   # say + wait for the turn to finish + print the result  ← use this
af say     [name] "text"   # type + submit, don't wait
af wait    [name]          # block until the agent is idle or needs input
af screen  [name]          # dump the current TUI screen
af result  [name]          # last completed turn's text (from the session log)
af ctx     [name]          # estimated context size (tokens)
af compact [name]          # run /compact (idle only)
af sweep                   # compact agents past their threshold. `post`/`mail` run it for you.
af remote  [name]          # relaunch with Remote Control so the human drives from the Claude app
af approve [name] [1|2|3]  # answer a tool-permission prompt (default 2)
af keys    [name] <keys>   # raw tmux keys (Escape, C-c, …)
af attach  [name]          # print the tmux attach command for a human viewer
af down    [name]          # quit the agent + kill its session
af list                    # running agents
af slug                    # the current project slug
af ledger                  # THE status view — see "Spec and revive"
af revive  [name]          # bring a killed agent back with memory AND role/hooks
af revivable               # who can be revived
af probe   <name>          # one look — see below
```

Mail (agent-to-agent — see `squad.md`): `af post <agent> [--kind K] "text"`, `af mail`
(`inbox` is an alias), `af mailstat`, `af register-self`, `af unregister-self`.

**`af probe <name>` is the single source of truth for an agent's live state** — one
`capture-pane` + one session-log read yield alive/dead, phase (generating, on a
permission prompt, idle), context size, and the input box, replacing the nine
scattered screen-scrapes the old code hand-rolled per call. `ask`, `wait`, `sweep`
and `ledger` all read it; you rarely call it directly, but it is why they agree.

Typical flow — always `ask`, not `say`, when you want the reply:

```bash
SKILL="$HOME/.claude/skills/agent-factory"
export PYTHONPATH="$SKILL/scripts${PYTHONPATH:+:$PYTHONPATH}"   # scripts/ → the repo's factory/
python3 -m af up neo
python3 -m af ask neo "Summarize the README in one line"
# ... more turns; the agent remembers (same session) ...
python3 -m af down neo
```

(Or, with no setup, the shim: `bash "$SKILL/scripts/ai.sh" ask neo "…"`.)

### What you must know to drive it well

- **`ask` returns when the agent is actually done** — it waits for a new `end_turn`
  record in the agent's session log AND for the live generation timer
  (`✻ Computing… (4s · …)`) to vanish. The log is the authority precisely because
  the screen isn't: the footer and token counter churn constantly, so polling the
  screen for "stability" never settles. Use `ask`; it already does all of this.
- **`ask` gives up after `AI_TIMEOUT` seconds (default 300)** and tells you the
  agent is still working — it does not kill the turn. Raise it for long tasks
  (`AI_TIMEOUT=1800 af ask …`) or come back with `af wait` / `af result`.
- **The first turn is slow** (~5–6s of session init after `up`). `ask` waits.
- **Spawned agents skip permission prompts** (`--dangerously-skip-permissions` on
  `up`/`revive`/`remote`): they run unattended, so otherwise they'd stall forever
  on the first tool gate with nobody to approve. They can read/write/run without
  asking — only spawn them on work you'd let run on its own. `AI_SKIP_PERMS=0`
  restores prompting; then `ask` surfaces `⚠ paused on a permission prompt` and
  `af approve <name>` answers it.
- **After `up`, confirm the agent is alive** (`screen` shows the TUI, or `ask`
  returns) before reporting success.
- **Don't tear agents down unless asked** — they're meant to be long-lived. Agents
  you spawned for a demo/test: track the names and `down` them when the user says
  you're done.

### Slash commands inside a spawned agent

It's a real Claude Code TUI, so it takes slash commands — `say` one (e.g.
`af say neo "/model"`). Two are wired in as first-class commands because timing
matters:

- **`/compact` — `af compact <name>`.** It refuses **mid-generation or on a
  permission prompt** (keystrokes would interrupt the turn or wrongly answer the
  prompt), so it only ever runs at the idle point between turns. Check context size
  anytime with `af ctx` or `af ledger`. Mostly you don't need to call it — see
  "Context" below, compaction is automatic.
- **`/remote-control` — hand the human live control from the Claude app or phone.**
  `af remote <name>` relaunches the agent with `--remote-control <name>`,
  **resuming its session so memory survives**, then tells the user to open the
  Claude app (sign-in required). After that the human drives; you can still read
  along with `af screen`.

### Context — compaction is AUTOMATIC, on token count

Compaction fires on **measured context size** alone — soft and hard thresholds in
absolute tokens. There is no task-boundary gate: if the context is past threshold,
it compacts at the next turn boundary, mid-task or not.

| knob | default | meaning |
|---|---|---|
| `AI_COMPACT_SOFT` | 200000 | past this, compact at the next turn boundary |
| `AI_COMPACT_HARD` | 500000 | compact at the next turn boundary regardless — losing some working state beats running out of context |

Absolute token counts; set either to `0` to disable. Per-station in a blueprint:
`compact_soft:` / `compact_hard:` (in `defaults:` or on one agent).

**Where it fires matters.** `af` is a command, not a daemon: it can only check an
agent's context at a moment when it happens to hold control right after that agent's
turn. `ask` is such a moment (it waited for the turn). **`af sweep` is the guard for
any other moment**: it walks every agent with a mailbox and compacts the ones past
threshold, skipping any that is mid-generation or paused on a permission prompt
(keystrokes would interrupt the turn or answer the prompt for the human).

**You do not have to remember to call it.** `af post` and `af mail` run a sweep
themselves (`AI_SWEEP_OFF=1` disables that). `post` sweeps **before** it sends, and
skips the recipient — compacting an agent in the same breath as handing it a task is
a race with nothing to gain. Each agent is judged by **its own** thresholds, taken
from its spec, not the orchestrator's env.

`af ledger` deliberately does **not** sweep: it's a look, and silently shrinking an
agent's memory out from under someone who came to inspect it isn't what "show me the
line" means. It prints which agents a sweep would compact.

**A sweeper cannot compact itself.** A sweep runs inside the sweeper's own shell, so
typing `/compact` into its own pane would land mid-turn — its own turn. So `sweep`
skips whoever is running it.

For a single agent driven by `af post`/`af mail`/`af ask` this is all self-contained.
**A team of agents driven by mail alone, with nobody running `af` from the top, is a
different gap** — nothing in this file's mechanism guards that. See `squad.md`'s
"The warden" for how an unattended team stays compacted.

## Spec and revive — an agent's constitution

`--resume` restores an agent's **memory**. It does not restore its role, model,
system prompt or hooks. So every spawn writes a **spec** —
`~/.claude/agent-factory/lines/<slug>/agent-<name>.json` — holding everything
needed to recreate it identically: role env, model, launch flags, appended system
prompt, and the settings file that installs its hooks.

```bash
af ledger      # every agent: alive?, model, role, ctx, unread mail, and a LIVE wall check
af revivable   # who has a surviving log
af revive eval # back with memory AND constitution
```

**`revive` refuses rather than half-restoring.** No spec, a corrupt spec, or a spec
with no flags → refusal with a reason (`AI_FORCE=1` overrides). Broken or
non-executable hooks refuse **only for a `required` station** (the `delegate:`
wall levels — a team-station concept, see `squad.md`), where the wall is the
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

## Cleanup — `afctl.sh`

Every spawned agent runs with a known `--session-id <uuid>` recorded in
`~/.claude/agent-factory/manifest.tsv`, so factory logs can be purged without ever
touching the human's own sessions. (`afctl.sh` is still real bash — not a shim.)

```bash
afctl.sh list             # the manifest
afctl.sh sessions         # locate each agent's .jsonl (present/missing)
afctl.sh purge --dry      # preview — ALWAYS show this to the user first
afctl.sh purge            # delete those logs, clear the manifest
```

Deleting logs is irreversible: run `purge --dry` and show the user before a real
`purge`. (`--append-system-prompt` is not written to transcripts, so the manifest —
not any in-log marker — is the durable link to an agent's log.)
