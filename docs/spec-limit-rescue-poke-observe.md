# Spec — Limit rescue by poke-and-observe (Option B)

Status: DRAFT for review. No code until approved.

## Problem

The warden's usage-limit rescue trusts a scraped wall-clock prophecy. The statusline
reads `resets 8:40pm` from Claude Code's output, writes it to `limits.json` as
`resets_at`, and the warden sleeps until that timestamp before waking a limited agent.

The prophecy goes stale and the warden waits for a reset that already happened:

- **Account switch** — user moves the agent to a different account; that account's limit
  is gone, but `resets_at` still names the old account's wall.
- **Laptop sleep / clock skew** — the wall drifts from real time.
- **CC changes the string** — `resets 8:40pm` → any other phrasing → scrape returns
  nothing or garbage.

Same bug class as sid-drift and ghost-compact: **a value cached once, acted on later,
gone stale.** The fix that already works (the `EMPTY_PCT` context guard) works because it
reads ground truth at decision time instead of a cached number.

## Principle

Ground-truth-at-decision-time. The warden never predicts *when* the limit lifts. It
observes *whether* it has lifted, by poking and looking. The only truth of "am I still
limited" is "try, and see what the pane does."

## Design

Token-free, runs in the existing detached warden loop. No new process, no cron. This is
**subtraction** from today's warden, not addition.

### Remove

- Statusline no longer writes `limits.json` / `resets_at`.
- Warden no longer reads `resets_at` and no longer gates on a wall-clock.
- `Paths.limits_json` and its readers go away.

### Keep

- `limited-<agent>` marker: the cheap *observation* that an agent hit the wall. Written by
  the statusline when it sees the limit error (ground-truth observation, not prophecy),
  and/or by the warden when a poke re-errors. This stays.

### Warden rescue tick (token-free)

For each line-agent whose `limited-<agent>` marker is set:

1. **Skip if busy.** `phase == "generating"` → the agent is working, so it is not limited
   right now; clear the marker and move on. (A limited agent cannot generate.)
2. **Skip if not yet due.** Per-agent backoff: don't poke again until `last_poke +
   backoff`. `last_poke` is an *observation* (when we last tried), never a prediction of
   the reset.
3. **Poke once.** `drive.ring(agent)` — one `!bash $AF_MAIL read` doorbell. On a recovered
   account this fires a real turn (agent drains mail, resumes). On a still-limited account
   it fires a turn that errors immediately (~0 tokens).
4. **Observe** after a settle delay, from `capture_pane`:
   - Pane starts a turn (`phase == "generating"`) OR returns to a clean idle with **no
     fresh limit error** → **recovered.** Clear `limited-<agent>` and `last_poke`. Done.
   - A **fresh** limit error is the latest render → **still limited.** Leave the marker,
     record `last_poke = now`, grow backoff, try next tick.

### Fresh vs. stale limit error

A parked limited agent's pane shows the OLD limit error frozen — it will not clear on its
own, so "error string absent" is not the recovery signal. Recovery is decided by what the
**poke** produces, not by the pre-poke pane:

- After poke, if the agent transitions to generating / normal input → recovered.
- If the poke's turn produces a limit error *again* (error is the newest line after the
  poke) → still limited.

`patterns.limit_error(pane)` matches the CC limit line; the decision keys off phase +
whether the error is the newest render after the poke, not off a scraped time.

### Backoff

Per-agent, capped. Start ~60–120 s, grow, **cap at ~5 min.** The cap is the whole point:
an early reset (account switch) is caught within one interval — at most ~5 min late —
regardless of any wall-clock. No lower bound on how early recovery can be detected.

## Cost / trade-offs

- A poke to a still-limited agent = one *failed* turn (errors before generating, ~0
  tokens) plus one keystroke — the same keystroke `ring` already sends. The backoff cap
  bounds how often this happens.
- Trade a whole prophecy chain (statusline scrape → `limits.json` → `resets_at` → wall
  compare) for a dumb capped poke. Fewer moving parts than today.
- Immune to account switch, laptop sleep, clock skew, CC string changes — by
  construction, because nothing depends on the clock.

## Files touched

- `factory/hooks/statusline*` — stop writing `limits.json` (keep the `limited-` marker).
- `factory/af/warden.py` — replace the `resets_at` wall-clock gate with the
  poke-and-observe backoff loop.
- `factory/af/patterns.py` — `limit_error(pane)` matcher (add if absent).
- `factory/af/paths.py` — remove `limits_json`; add per-agent `last_poke` state path.
- Tests — rewrite the warden rescue tests around poke→observe with an injected pane/phase;
  no wall-clock in the fixtures.

## Out of scope

- Per-account tracking / adapting to which account a line runs under. The poke-observe
  design is account-agnostic by construction; do not add account awareness.

## Open questions for review

1. Backoff cap — 5 min acceptable, or tighter (catch reset sooner, poke more)?
2. Should the statusline keep detecting the limit at all, or should the warden be the
   sole observer (one place writes the marker)? One writer = simpler.
3. Fold the mail-doorbell **atomic claim** fix (O_CREAT|O_EXCL ring marker, closes the
   concurrent-sender race) into this work, or ship it separately first?
