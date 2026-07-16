"""The context guard for a MAIL-DRIVEN line.

`maybe_autocompact` only ever runs where something holds control at the end of an agent's
turn — i.e. from `ask`. But a line's agents are driven by MAIL, not by `ask`, so the guard
never applied to the very line it was built for. `sweep` walks every agent with a mailbox
and applies the same two thresholds at a safe point.

It has to be REMEMBERED to be run, and "the model will remember" is exactly the guarantee
that has silently failed in this system before — so every command that touches the line
sweeps first: `post` (handing out work) and `mail` (collecting it). `ledger` deliberately
does NOT: it is a LOOK, and silently shrinking an agent's memory out from under someone who
came to inspect it is not what "show me the line" means. It reports what a sweep would do.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass

from . import drive
from .paths import Paths, paths
from .probe import Probe, probe
from .nums import intish

SWEEP_LOCK_STALE = 600   # a lock older than this is a corpse — its holder was killed

# At or below this statusline Context%, a /compact has nothing to do and CC rejects it. The
# threshold is deliberately tiny: it only fires the "empty" skip on an agent CC itself reports
# as all but empty, never on one that is merely small. Window size is unknown (200k vs 1M), so
# this stays a percentage guard, not a token conversion.
EMPTY_PCT = 2


def skip_reason(name: str, pr: Probe, *, me: str = "", skip: str = "",
                last_compacted: int | None = None, now: int | None = None,
                cooldown: int = drive.DEFAULT_COOLDOWN) -> str | None:
    """Why this agent must not be compacted right now — or None, meaning go ahead.

    Every clause is a bug that shipped:

      orchestrator — the mailbox of the DRIVING session, not an agent. It has no pane of
                     its own to compact and it must never be treated as a station.
      self         — a sweeper cannot compact ITSELF: the sweep runs inside its own agent's
                     Bash tool, so /compact would land in its own pane, mid-turn — its own
                     turn.
      recipient    — the agent `post` is about to hand a task to. A /compact typed at the
                     same moment is a race with nothing to win: both are keystrokes into
                     one input box, and the agent is about to be busy with that very task.
      down         — no pane to type into.
      generating   — mid-turn: not a safe point. The keystrokes would interrupt the turn.
      permission   — waiting on a human. Typing there ANSWERS the prompt.
      limited      — out of quota. /compact is a model call and would bounce — forever: the
                     context never drops, so the next sweep sees the same fat agent and
                     re-sends it, every tick, until the quota returns.
      cooling      — a compaction takes a TURN to happen and the log does not shrink until
                     it lands, so for a minute or two the agent still READS fat. Without
                     this an unguarded sweep re-compacts an agent that is already
                     compacting (seen live: the warden's first tick re-compacted three
                     agents a manual sweep had just done).
      empty        — Claude Code's own statusline says the context is near-empty, but the
                     transcript's last usage record still shows a pre-/clear size. Compacting
                     bounces ("Not enough messages to compact") and the transcript never
                     shrinks, so the next sweep re-sends it forever. Believe CC's readout over
                     the stale estimate. (Seen live: eval and rag pinned at Context 0% while
                     the transcript read 371k/392k, re-compacted every tick.)
    """
    if name == "orchestrator":
        return "orchestrator"
    if me and name == me:
        return "self"
    if skip and name == skip:
        return "recipient"
    if not pr.alive:
        return "down"
    if pr.phase == "generating":
        return "generating"
    if pr.phase == "permission":
        return "permission"
    if pr.phase == "limited":
        return "limited"
    if pr.ctxpct is not None and pr.ctxpct <= EMPTY_PCT:
        return "empty"
    if last_compacted is not None:
        now = int(time.time()) if now is None else now
        if now - last_compacted < cooldown:
            return "cooling"
    return None


@dataclass
class _Lock:
    p: Paths
    held: bool = False

    def take(self) -> bool:
        """One sweep at a time. Two concurrent ones would each decide the same agent is idle
        and type /compact into the same pane twice."""
        d = self.p.sweep_lock
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            d.mkdir()
            self.held = True
            return True
        except FileExistsError:
            pass
        except OSError:
            return False
        try:
            age = int(time.time() - d.stat().st_mtime)
        except OSError:
            age = SWEEP_LOCK_STALE
        if age < SWEEP_LOCK_STALE:
            # Silence here would make an explicit `af sweep` report success having done
            # nothing at all.
            print(f"[af] another sweep is running (lock {age}s old) — skipped.")
            return False
        shutil.rmtree(d, ignore_errors=True)
        try:
            d.mkdir()
            self.held = True
            return True
        except OSError:
            return False

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.p.sweep_lock, ignore_errors=True)
            self.held = False


def _cooldown() -> int:
    raw = os.environ.get("AI_COMPACT_COOLDOWN", "")
    return intish(raw, drive.DEFAULT_COOLDOWN)


def _last_compacted(agent: str, p: Paths) -> int | None:
    try:
        raw = p.compacted(agent).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return intish(raw, None)


def sweep(skip: str = "", p: Paths | None = None) -> int:
    p = p or paths()
    p.mailroot.mkdir(parents=True, exist_ok=True)
    p.state.mkdir(parents=True, exist_ok=True)

    lock = _Lock(p)
    if not lock.take():
        return 0

    # The lock is released on the way out of EVERY exit — return, exception, Ctrl-C, and a
    # TERM from a tool timeout (each probe reads a whole transcript, so a slow sweep is
    # real). A sweep that died holding it would silently disable EVERY sweep for the next
    # ten minutes; try/finally covers the exceptions, and the signal handler turns a TERM
    # into one. Re-raising after cleanup matters: a handler that only cleans up would let
    # the loop resume with its lock already released, so a second sweep could start and
    # type /compact into the same pane twice — the one thing the lock exists to prevent.
    prev: dict[int, object] = {}

    def _die(signum, _frame):
        lock.release()
        sys.exit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev[sig] = signal.signal(sig, _die)
        except (ValueError, OSError):
            pass  # not the main thread (the warden calls sweep from one) — try/finally still holds

    me = os.environ.get("AF_AGENT", "")
    cooldown = _cooldown()
    try:
        for name in p.boxes():
            pr = probe(name, p)
            reason = skip_reason(
                name, pr, me=me, skip=skip,
                last_compacted=_last_compacted(name, p), cooldown=cooldown,
            )
            if reason == "limited":
                print(f"[af] '{name}' is out of quota (usage limit) — not compacting; "
                      f"the warden will wake it on reset")
                continue
            if reason:
                continue
            soft, hard = drive.spec_thresholds(name, p)
            # nowait: /compact is keystrokes, and the agent compacts on its own time.
            # Blocking here would put a 300s wait inside every `af post` — per agent.
            drive.maybe_autocompact(name, soft, hard, nowait=True, p=p)

    finally:
        lock.release()
        for sig, handler in prev.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
    return 0


def autosweep(skip: str = "", p: Paths | None = None) -> None:
    """Only an ORCHESTRATOR sweeps. A worker running `af mail` in its own session must not
    start compacting its peers.

    Orchestrator means either the top session (no AF_AGENT) or an agent whose ROLE is
    orchestrator. Testing the NAME instead left the autonomous line — where the orc drives
    the workers by mail and the human never touches the CLI — with no sweeps at all, which
    is exactly the case this was built for.
    """
    if os.environ.get("AI_SWEEP_OFF") == "1":
        return
    me = os.environ.get("AF_AGENT") or "orchestrator"
    if me != "orchestrator" and os.environ.get("AF_ROLE") != "orchestrator":
        return
    try:
        sweep(skip, p)
    except SystemExit:
        raise
    except Exception as e:  # a sweep that fails must not take `post` down with it
        print(f"[af] sweep failed: {e}", file=sys.stderr)
    self_ctx_warn(p)


def self_ctx_warn(p: Paths | None = None) -> None:
    """A sweeper cannot compact itself, and on an AUTONOMOUS line the orc IS the only
    sweeper — so the longest-lived agent on the line is the one agent nothing guards. We
    cannot act for it; we can tell it, in output it is already reading, to act for itself.
    (The top session has no AF_AGENT and no agent log of its own, so it says nothing there.)
    """
    p = p or paths()
    me = os.environ.get("AF_AGENT", "")
    if not me:
        return
    c = drive.ctx(me, p)
    if c <= 0:
        return
    soft, _hard = drive.resolve_thresholds()
    if soft != 0 and c > soft:
        print(f"[af] ⚠ YOUR OWN context is ≈ {c} tok (> {soft}). Nothing can compact you — "
              f"run /compact yourself at your next safe point.")
