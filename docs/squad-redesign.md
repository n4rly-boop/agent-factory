# Squad redesign — a clean, deterministic team model

Status: DRAFT for discussion. No code until approved.

This document reworks the multi-agent side of the skill (`af line`, renamed **squad**)
around one idea the current code is missing: **when nobody is driving the team, some
process must own its state.** Today `af` is a command, not a process, so state is
recomputed from `tmux`+`ps` on every call and thrown away, mail is delivered
synchronously by whoever sends it, and the context guard only fires while a human is
speaking. The fixes below give the team an owner.

The single-agent use case (`af up` / `af ask` — one Claude you talk to) is **not
changed** and gets its own thin skill doc. Everything here is about the team.

---

## Scope — two use cases, cleanly split

- **`agent`** — one long-lived Claude in a tmux session that a human drives directly.
  Deterministic, no hierarchy, no daemons beyond an optional guard. Documented in a
  ~80-line `SKILL.md`.
- **`squad`** — a team from a blueprint: a root orchestrator and its children, roles
  enforced by hooks, mail between stations, two daemons keeping it alive. Documented in
  a separate `squad.md` that only loads when the user wants a team.

The split is the low-context win: the common case (spawn one agent) never pulls in the
squad machinery.

---

## Problems in today's `line`

Confirmed by reading the current code:

1. **No state store.** "Who is on the team / is it alive / what is its role" is scattered
   across `blueprint.yml`, per-agent spec JSON, tmux session existence, mailbox flag
   files, `sid-<agent>` files, and `manifest.tsv` — seven places with different truth
   semantics. The blueprint is effectively immutable after `line up` (`line.py:721` leaves
   running stations alone). `down` (`lifecycle.py:213`) kills the tmux session but never
   updates the spec, manifest, or `line.json`, so "removed" is expressed only as
   tmux-absence. `heal.diagnose` / `live.live_sid` already **compute** real live status —
   then throw it away.

2. **Fragile session binding.** A session id lives in three places (`sid-<agent>` file,
   `spec.sid`, manifest row) that can disagree; `spec.sid` is frozen at spawn and actively
   misleading as a fallback. Claude Code forks the session on `--resume`, and the drift is
   healed **only while the agent is alive** (`hooks.session_start`, `live.heal_sid_file`).
   An agent that forks then dies before a heal re-raises on the frozen parent transcript,
   losing post-fork work.

3. **No mail router, no dedup.** `mailbox.send` + `drive.ring` run synchronously inside
   whichever `af` process sends: append to a file, then type `!bash $AF_MAIL read` into
   the recipient's pane. There is no background router and **no message dedup at all**
   (`msg_id` exists but nothing checks it). The `mkdir` lock and the whole bash-parity
   apparatus were justified by "bash and Python both write the mailbox" — but every `.sh`
   is now a 6-line Python shim, so **that premise is dead** and the machinery can go.

4. **Compaction fires on a prediction and only while driven.** Context *size* is measured
   fact (transcript tokens, `probe._scan_log`), but the soft-threshold "compact only
   between tasks" gate reads `is_mid_task` — a busy/idle flag set from mail kinds
   (`mailbox._mark_task`). That flag goes stale and needs a reaper (`sweep._reap`) to undo
   it. And `sweep` only ran from `post`/`mail`/`sweep`, i.e. only while the driving session
   spoke — a team working autonomously was never compacted (observed: 767k tokens against a
   500k hard threshold). The warden closes the "while driven" gap but keeps the
   prediction gate.

5. **Flat topology, no dynamic spawn, agents don't delegate.** The hierarchy is a flat
   list with a `parent` label (`line.py:383`); mail is a full mesh. No agent can spawn
   another — all stations are created up-front by `line up`. The `delegate-wall` default
   (`advised`) never blocks, only adds a note, so agents ignore it and write bulk work
   themselves. Delegating to the local model is multi-step and therefore always the
   expensive path the model avoids.

---

## Design

### 1. One state store — `squad.json`

A single per-slug file is the only mutable source of truth for the team. `tmux`+`ps`
become the **reconciler** that corrects it, not the store that replaces it.

Per station:

```
name, role, parent, model, delegate, spawn_flags,
settings_path,     # the fork-proof identity key (--settings .../settings-<name>.json)
live_sid,          # authoritative current session id; updated on heal; captured on down
status,            # planned | alive | down | limited
ctx_tokens,        # last measured
unread             # last measured
```

Writers, all through a single `flock` (mkdir mutex retired — see §3):

- `up` / `down` mutate the roster and status. **`down` records `status: down` and captures
  `live_sid` before killing the session** — the fix for re-raise resuming a frozen parent.
- The **postmaster** daemon (§3) reconciles `status`, `live_sid`, `ctx_tokens`, `unread`
  on its tick and on mail events.
- The `SessionStart` hook writes `live_sid` from inside the session.

`line.json` and the `sid-<agent>` file collapse into this. The per-agent **spec** survives,
but only as the immutable revive constitution (role env, flags, model, appended prompt);
**`spec.sid` is dropped from the resume fallback chain** — `squad.json.live_sid` is the one
authority.

Reuse: `heal.Finding` (`heal.py:41`) is essentially this record already; persist it instead
of recomputing and discarding.

### 2. Session binding

- **Primary key = `settings_path`.** The `--settings .../lines/<slug>/settings-<name>.json`
  token uniquely names `(slug, name)` and survives forks — a firmer anchor than any sid
  (`live._agent_lines`, `live.py:63`).
- **`live_sid` is authoritative and always current.** Healed from inside (`SessionStart`
  hook) and outside (`live.live_sid` via `ps` argv), written to `squad.json`, and **captured
  at `down`** so a killed-then-forked agent re-raises on the transcript that was actually
  growing.
- `revive` / `up --resume` read `squad.json.live_sid`, gate on transcript existence, and
  refuse rather than silently spawn fresh — keeping the existing safety
  (`line.py:688`, `lifecycle.py:342`) but off one trusted field instead of three that
  disagree.

### 3. Daemon A — postmaster (state + mail)

Hot path. Short tick / event-driven. Owns `squad.json` and mail routing.

- **Routing with dedup.** Agents append to their outbox; the postmaster dedups by `msg_id`,
  routes to `<to>.jsonl`, and rings the doorbell **once**. This decouples wake-up from send
  (today they are welded in the caller) and adds the message-level idempotency that is
  absent now.
- **Reconcile.** On its tick it refreshes `status` (tmux liveness), `live_sid` (heal),
  and `unread` in `squad.json`.
- **Keep the on-disk contract:** one JSON line per message (atomic sub-`PIPE_BUF`
  `O_APPEND`), cursor-as-ack, blob spill for large bodies, `task_state` fold. These are
  transport-agnostic and reused unchanged.
- **Retire the bash-parity machinery.** Every `.sh` is already a Python shim and nothing in
  bash reads the mailbox, so the `mkdir` mutex becomes a plain `fcntl.flock`, and the
  parity code (`_split` vs `splitlines`, jq-parity separators, cursor write-back to keep
  "both implementations" agreeing) is deleted. Single writer, one lock primitive.

### 4. Daemon B — warden (context + limit), standalone and tmux-only

Cold clock (~5 min). Guards context and rescues the usage limit. **Decoupled from the
squad concept** — it can watch any live Claude Code session, not just factory agents.

- **Binding is by SID; driving is by tmux — and tmux is required.** `af warden watch
  --session <sid>` (or `--target <tmux-session>`) reads the transcript and `limits.json`,
  but **compaction and rescue both need to type into a pane**, so the warden refuses a
  target that is not a tmux session: *"wrap your session in tmux to be guarded."* There is
  no notify-only / observe-only tier — one code path, always a write channel. A human's own
  session is guarded only if launched inside tmux; a plain session is out of scope by
  design.
- **Compaction by measured fact, no prediction.** Compact on the measured token threshold
  (`AI_COMPACT_SOFT` / `AI_COMPACT_HARD`) alone. **The `is_mid_task` busy-flag gate is
  removed**, and with it the reaper that existed only to undo that flag's staleness. Skip
  only: generating, on a permission prompt, or already empty (`Context% <= 2`). Compaction
  is always driven by the warden typing `/compact` — never delegated to Claude Code's
  built-in auto-compact — so behavior is deterministic and observable.
- **Limit rescue pokes only the orchestrator.** Detect via the `limited-<name>` hook marker
  plus the pane belt (`patterns.USAGE_LIMIT`). Instead of waking every cut-off agent, poke
  **only `orc`**; once its window reopens (a turn lands — the one honest reset signal,
  `warden.py:329`), the orchestrator re-drives its workers by mail. Keep the landed-turn
  detector; keep ignoring the scraped `resets_at` for wake decisions.
- **No two-warden race.** The pidfile moves out of `/tmp`
  (`~/.claude/agent-factory/state/<slug>/`) and is guarded by a real lock, closing the
  `/tmp`-purge → second-warden window.

Why two daemons and not one: postmaster is hot (every message), warden is cold (every few
minutes, heavy log scans). Different cadences and different blast radius — if the warden
dies, mail still flows.

### 5. Topology — parent-only tree, root spawns, others delegate

- **`parent` pointer only. No depth counter, no configurable N.** The hierarchy is the set
  of `parent` edges; `squad.json` drops the `depth` field entirely.
- **Only the root spawns full agents.** A new **spawn-gate** hook (PreToolUse on Bash,
  matching `af up`) is binary: `AF_ROLE == orchestrator` → allow; any other station → deny,
  with the message *"below the root, use a Task subagent or delegate-to-local-model, not a
  full agent."* Built like `delegate-wall`: fail-closed, strict. This is the one hard
  topology invariant.
- **Dynamic spawn for the root.** The orchestrator may `af up --parent <self>`;
  `lifecycle.up` is already importable and parameterized, so it registers the child into
  `squad.json` with the parent edge set.
- **Human → any node, by convention not enforcement.** The human normally talks to the
  root, but `af post <any>` stays open — no access control on the human's channel. Only the
  `role-reminder` states the convention.

### 6. Make agents actually delegate and spend context well

Delegation and context thrift decay when they live in a prompt — the same reason roles are
enforced by hooks. So they move to the tool boundary. **Writes stay free** (a write-wall
that farms out two-line fixes is overhead); the levers are cheaper-delegation, visible cost,
and a read-wall.

- **B — one-command delegate.** `af delegate "<spec>" <out>` wraps the whole local-model
  call. The model avoids delegation today because it is multi-step (read the skill, run
  `agent.py`, stage a prompt, collect output) while writing it yourself is one step. Make
  the sanctioned path one tool call and the incentive flips.
- **C — context cost made visible.** A `UserPromptSubmit` hook injects the current
  `Context: NN%` (~5 tokens) every turn, so the model feels the cost instead of being told
  about it once.
- **D — read-wall with an escape hatch.** Reading a huge file straight into the window is
  the top context sink. A `PreToolUse` hook on `Read`:
  1. File over `K` lines **and** the read is unbounded (no `limit`) → **deny**, offering
     three routes: read in bounded pages (`offset`/`limit`), `af delegate` to return a
     distilled slice, or `af read-force <path>` to drop a **one-shot** override and re-read.
  2. If a force token for that path is present → allow and consume it (deliberate each
     time).

  Bounded reads (`limit` set) always pass — that is normal work. Read has a fixed input
  schema, so the "special argument that skips the deny" is realized as the preceding
  `af read-force` command rather than a field on `Read`.
- **E — all of the above at the tool boundary, none in the brief.** The prompt rots; the
  hook does not.

Net effect: the self-serve path is expensive or blocked on large reads and cheap on
delegation, so the model delegates because it is the cheaper path, not because it was asked.

---

## Reuse vs rewrite

| Reuse unchanged | Rewrite / new |
|---|---|
| `probe`, `patterns`, `live.live_sid` / `heal`, `lifecycle.up`, the hook engine (env → inject/deny), walled-path machinery, "landed turn = recovery" | mailbox (flock, dedup, router), warden (standalone tmux-only, compact-by-fact, poke-orc-only), `squad.json` state store, `line` → `squad` (parent-only, spawn-gate, dynamic spawn), delegation levers B/C/D, `SKILL.md` split |

## Open questions

None blocking. Confirm the doc, then sequence the implementation (suggested order:
`squad.json` + session binding → postmaster → warden split → topology/spawn-gate →
delegation levers → skill-doc split).
