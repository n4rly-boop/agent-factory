# Blueprint wizard — design a line WITH the human, then write it

You are here because the human invoked the skill as `/agent-factory plan …` (or asked to
design a line interactively). Your job is an **interview**, not a guess: you ask, they
decide, and only then does a `blueprint.yml` exist.

Read `af/line.py` if you need the ground truth on a key. Everything below is what the code
actually enforces — do not invent keys.

## The one rule

**Do not write the file until the human has approved the resolved plan.** A blueprint is a
constitution for N long-lived agents; a wrong `delegate:` or a bad brief is not a typo you
fix later — it is a line that spends an hour doing the wrong thing, on the human's tokens.
Interview, echo back, get a yes, then write.

Equally: **do not run `af line up` on your own.** Writing the file is the deliverable. Spawning
is a separate, explicit yes.

## Step 0 — what is already on the table

If the human passed a goal with the invocation (`/agent-factory plan refactor the parser`),
that is the mission — do not re-ask it. If they passed nothing, ask what the line is FOR in
one open question, in prose, not with `AskUserQuestion` (a free-text goal is not a menu).

Then look before you ask: the repo they are in, its languages, its test command. A wizard
that asks "what language is this project?" while sitting in a `Cargo.toml` is wasting their
turn.

## Step 1 — the interview

Use `AskUserQuestion`, batched (up to 4 questions per call), with a recommended option
FIRST and marked `(Recommended)`. Do not ask what you can default sanely; do ask what
changes the shape of the line.

Worth asking:

1. **Topology.** Orchestrator + N workers (the default; one station holds `role:
   orchestrator`, everyone else reports to it) / a pipeline of stages / flat peers with no
   boss. Offer concrete station names in the option descriptions, not abstractions.
2. **Stations.** Given the goal, PROPOSE the split (name + one-line remit each) and ask them
   to confirm, cut, or add. Do not make them invent the org chart from nothing — you have
   read the repo, they hired you for this.
3. **Model tier.** `opus` orchestrator + `sonnet` workers (default) / all sonnet (cheap,
   good for bulk) / all opus (expensive; only for genuinely hard reasoning per station).
4. **`delegate:`** — `advised` (default: bulk writes outside `work/` get a nudge, nothing
   blocks) / `required` (hard wall: outside `work/` — and scratch, `/tmp` and `/var/folders`
   — the station may not write AT ALL, at any size; it must dispatch to
   `delegate-to-local-model` or a peer) / `no` (no hook).
   Tell them what `required` costs: a station under a hard wall cannot make a three-line fix
   itself. It is right for a station that reviews or coordinates, wrong for one that edits code.
5. **`work:`** — the writable zone. Everything the line writes lands there, and (scratch
   aside) it is the only place a walled station may write. Default `./work` under the repo.

Skip anything they have already told you. Four crisp questions beat eight thorough ones.

## Step 2 — write the briefs (the part that actually matters)

A `brief:` is the station's mission. `af line up` materialises it as
`$work/entrypoint-<name>.md` — the first thing the agent reads.

`af line up` ALREADY prepends, per station: who you are, who you report to, your peers + the
`mail send/read` commands, the whole delegate clause, and "write your report to
`$work/<name>.md`". **Do not repeat any of that in the brief.** A brief that re-explains the
mail protocol just burns the agent's context on something it was already told.

A brief SHOULD say, in this order:

- **Mission** — one sentence. What this station is for.
- **Scope** — what it owns, and explicitly what it does NOT own (which peer owns that).
- **Inputs** — the files, dirs, or upstream station's report it starts from.
- **Done** — the condition under which it mails `--kind done`. Make it checkable. "Refactor
  the parser" is not a done-condition; "every call site migrated and `cargo test` green" is.
- **Quality bar** — the standard it is held to, and what it must never do.

Write briefs in the human's language if they are writing to you in it. Write them as YAML
block scalars (`brief: |`), never as one long quoted line.

## Step 3 — echo the resolved plan

Before writing anything, show them a compact table — station, role, parent, model, delegate,
count — plus the briefs in full. Ask for the yes.

## Step 4 — write, then validate

Write the file where they want it (default: `blueprint.yml` in the repo root). Then:

```bash
PYTHONPATH="$SKILL/scripts" python3 -m af line plan blueprint.yml
```

`$SKILL` is this skill's directory — an ABSOLUTE path, and `scripts/` symlinks to the
repo's `factory/`, which holds the `af` package. `PYTHONPATH` MUST be that absolute path:
your CWD is the human's project, not the skill dir, so a relative `scripts/` will not
resolve. (The compatibility shim `bash "$SKILL/scripts/line.sh" plan blueprint.yml` runs
the same code with no setup, if you prefer it.)

`af line plan` resolves defaults, expands `count:`, and validates — it prints exactly what
`af line up` will spawn, and spawns nothing. **Always run it.** If it errors, fix the blueprint
and re-run; never hand over a blueprint you have not seen validate.

Then tell them the next command and stop:

```bash
PYTHONPATH="$SKILL/scripts" python3 -m af line up blueprint.yml
```

## The schema — every key `af line` reads

```yaml
slug: myline              # mailbox + tmux namespace (ai-myline-<name>). Default: basename(cwd)
work: ./work              # the writable zone. RESOLVED TO AN ABSOLUTE PATH AT `af line up`,
                          # against the CWD OF THAT COMMAND — not the blueprint's dir.
                          # Write it absolute unless they will always run from the repo root.

defaults:                 # overridable per station, EXCEPT bulk_lines (see below)
  model: sonnet
  delegate: advised       # required | advised | no    (a TYPO here is FATAL — by design)
  caveman: false          # terse-output mode
  bulk_lines: 40          # what the advisory wall calls "bulk". READ FROM `defaults:` ONLY —
                          # a per-station bulk_lines is silently ignored, as is a non-numeric
                          # value (falls back to 40).
  compact_soft: 200000    # TOKENS, not chars. Auto-compact between tasks past this.
  compact_hard: 500000    # TOKENS. Auto-compact at any turn boundary past this.
                          # "Auto" = when the DRIVING session runs `af sweep` / `af post` /
                          # `af mail` (or the warden, on a clock). A line nobody drives and
                          # no warden watches is never compacted.

agents:
  boss:
    role: orchestrator    # EXACTLY ONE station should hold this. It is the default parent
                          # of every other station. The NAME `orchestrator` is RESERVED
                          # (it is the mailbox of the session driving the line) — the role
                          # is a key, not a name.
    model: opus
    delegate: required
    brief: |
      Mission: …
  worker:
    role: worker          # default
    parent: boss          # default: whoever holds role: orchestrator
    count: 3              # expands to worker1 worker2 worker3, each with this same brief.
                          # NOTE: the test is "is count set", not "is count > 1" — so
                          # `count: 1` RENAMES the station worker → worker1, breaking every
                          # `parent: worker` and every brief that says "mail worker".
                          # Want one station? Omit count entirely.
    brief: |
      Mission: …
```

Anything not in that list does not exist. `count:` copies the brief verbatim to every
replica — if replicas need to differ (an ablation over three configs), write three stations,
not `count: 3`.

## Traps to steer them off

- **`work:` is CWD-relative at `af line up` time.** Someone runs `af line up` from a subdirectory
  and the line's writable zone silently moves. Prefer an absolute path.
- **A station named `orchestrator` is fatal** — and it should be: it would share the driving
  session's mailbox and start compacting its peers. Give it the ROLE.
- **`delegate: required` on a station that must edit code** puts it in a loop: the one thing
  it cannot do is the thing it was hired for. Walls are for coordinators and reviewers.
- **No `role: orchestrator` anywhere is not an error — it makes the HUMAN'S session the
  boss.** Every station's parent resolves empty, and the role-reminder hook then tells each
  one "report to: orchestrator" — the mailbox of the session driving the line, i.e. yours.
  That is a legitimate flat line (you read the reports with `af mail` / `af ledger`), but it
  is a choice, not a default to stumble into: say it out loud, and set `parent:` explicitly
  if they meant something else.
- **Two stations with `role: orchestrator`** does not error either — the FIRST in YAML order
  becomes everyone's default parent and the second is quietly just another worker. The code
  does not enforce "exactly one"; you do.
