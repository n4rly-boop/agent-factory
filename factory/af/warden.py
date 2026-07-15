"""warden — the thing that watches a line WHEN NOBODY IS DRIVING IT.

    python3 -m af.warden watch     start it for this line (detached; safe to re-run)
    python3 -m af.warden stop
    python3 -m af.warden status     quota, reset time, who got cut off, when it last swept

Two jobs, and they are the same job: keep the line alive through the hours when no human and
no orchestrator is issuing commands.

  1. CONTEXT. `sweep` compacts idle agents past their threshold — but it only ever ran from
     `post` / `mail` / `sweep`, i.e. only when the DRIVING session spoke. An autonomous line
     does not go through those: its agents mail each other, and mail never swept. So a line
     left to work overnight was never compacted AT ALL. Observed, and it is why this file
     exists: a station reached 767k tokens against a 500k HARD threshold, with nothing to
     trip it. "Automatic" compaction that only fires while a human is at the keyboard is not
     automatic; it is a manual command with a misleading name.

  2. THE USAGE LIMIT. Account-wide: it kills every agent AND the orchestrator session at the
     same instant, mid-turn. The rescuer therefore cannot be a Claude — there is none left.
     It must be a process that spends no tokens, so the limit cannot touch it.

HOW IT RESCUES. It marks WHO was cut off, then wakes them by GROUND TRUTH — never a clock:
  * WHO — the StopFailure hook (matcher rate_limit) drops `limited-<name>` for the agent whose
    turn was killed, so an agent that was merely idle when the limit landed is left alone. The
    pane is read as a BELT: a hook that fails to fire (wrong version, lost +x) fails SILENTLY,
    and a rescue system that quietly does not rescue is worse than none.
  * WHEN — it does NOT wait for a reset time. The statusline's resets_at was a PROPHECY: true
    only for the account that rendered it, and a line's agents get moved between accounts, so a
    reset time can name a window that has already lifted (or one that never applied). Instead
    the warden POKES a cut-off agent on a capped interval and watches its TRANSCRIPT. A turn
    that LANDS (the end_turn count climbs) is the only honest proof the window reopened — the
    limit prose lingers in the scrollback long past the reset, so the pane cannot say. A poke
    that lands while still limited costs one turn that errors instantly: cheap, and the price
    of never trusting a clock that lies. (limits.json is still read, but ONLY for `status`
    display — never for a wake decision.)

WHAT IT DOES NOT DO. It cannot recover the killed turn: that API call is gone, and its tool
call with it. The agent keeps its full context, so it can pick the work back up — but it has
to be TOLD to, and told what happened, or it will just sit there. That is the message it gets.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import drive, live, mailbox, patterns, probe as probemod, sweep as sweepmod, tmux
from .paths import FACTORY_DIR, Paths, paths
from .nums import intish


def _int_env(k: str, default: int) -> int:
    v = (os.environ.get(k) or "").strip()
    return intish(v, default)


def tick_secs() -> int:
    return _int_env("AI_LIMITS_TICK", 60)


def poke_every() -> int:
    # We never wait for a scraped reset time; we retry the wake this often and watch the
    # transcript. Low enough that an early reset (an account switch) is found within one
    # interval; high enough that a poke wasted on a still-limited agent is rare.
    return _int_env("AI_LIMITS_POKE", 300)


def sweep_every() -> int:
    # Not every tick: a sweep reads every agent's session log, and there is no point paying
    # that once a minute for a threshold that takes an hour of work to cross.
    return _int_env("AI_SWEEP_EVERY", 300)


def sweep_off() -> bool:
    return (os.environ.get("AI_SWEEP_OFF") or "0") == "1"


WAKE = """The subscription usage limit cut your turn off ({why}). It has now reset.

Your context is intact — only the interrupted turn was lost, along with whatever tool call was in flight. Nothing else was rolled back.

Pick the work back up: re-read your brief and your report file, work out where you were cut off, redo the lost step, and carry on. If you cannot tell what you were doing, say so and ask, rather than guessing."""

STAGGER = 10   # seconds between wakes: four agents starting at the same instant all fire
               # their first request into the same fresh window.


# --- state ------------------------------------------------------------------------
def pidfile(p: Paths) -> Path:
    return p.state / "warden.pid"


def logfile(p: Paths) -> Path:
    return p.state / "warden.log"


def log(msg: str, p: Paths) -> None:
    p.state.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m-%d %H:%M:%S")
    with logfile(p).open("a", encoding="utf-8") as f:
        f.write(f"[warden {stamp}] {msg}\n")


def _pid(p: Paths) -> int:
    try:
        return int(pidfile(p).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _live(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --- the two signals ---------------------------------------------------------------
def _limits(p: Paths) -> dict:
    import json
    try:
        d = json.loads(p.limits_json.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return ((d.get("rate_limits") or {}).get("five_hour") or {}) if isinstance(d, dict) else {}


def resets_at(p: Paths) -> int:
    """The epoch the 5-hour window lifts, or 0 if nobody has rendered a statusline yet.
    0 means UNKNOWN — never "now"."""
    try:
        return int(_limits(p).get("resets_at") or 0)
    except (TypeError, ValueError):
        return 0


def used_pct(p: Paths) -> int | None:
    try:
        v = _limits(p).get("used_percentage")
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def pane_limited(agent: str, p: Paths) -> bool:
    """The belt. patterns.USAGE_LIMIT is the union of every wording Claude Code prints — the
    bash had two copies of this regex and they had already diverged."""
    pane = tmux.capture_pane(p.session(agent))
    return bool(pane and patterns.USAGE_LIMIT.search(pane))


def line_agents(p: Paths) -> list[str]:
    """Every agent of THIS line that has a session."""
    pfx = f"ai-{p.slug}-"
    return [s[len(pfx):] for s in tmux.list_sessions() if s.startswith(pfx)]


def marker(agent: str, p: Paths) -> tuple[int, str]:
    """(when it was cut off, which sid was cut off) — the TSV the limit-hook wrote."""
    try:
        cols = p.limited(agent).read_text(encoding="utf-8").split("\t")
    except OSError:
        return 0, ""
    when = intish(cols[0], 0) if cols else 0
    sid = cols[1].strip() if len(cols) > 1 else ""
    return when, sid


def _sid(agent: str, p: Paths) -> str:
    try:
        return p.sid_file(agent).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --- waking -------------------------------------------------------------------------
def wake(agent: str, why: str, p: Paths) -> None:
    """By MAIL, never by typing the message itself: the doorbell is one fixed token-free
    line, and the letter lands in the mailbox whatever state the pane is in.

    From `orchestrator`, so the agent's reply lands where the human reads it."""
    try:
        mailbox.send(agent, WAKE.format(why=why), kind="task", frm="orchestrator", p=p)
    except (OSError, ValueError) as e:
        log(f"{agent}: could not mail the wake-up ({e})", p)
        return
    drive.ring(agent, p)


# --- the loop -----------------------------------------------------------------------
def _heal_sids(p: Paths) -> str:
    """Before every sweep, point each agent's sid file at the session it is ACTUALLY running.

    Claude Code forks the session on --resume, so an agent rescued from a usage limit — or
    restarted by a human — is now on a new id while its sid file still names the frozen parent.
    Sweep would then read the parent's stale context (the "compacting an agent already at 0%"
    loop). This is the same repair heal does by hand; the warden does it on its own clock so a
    parked, unattended line self-corrects without anyone running a command.
    """
    fixed = []
    for a in line_agents(p):
        try:
            new = live.heal_sid_file(a, p)
        except Exception:
            new = None
        if new:
            fixed.append(f"{a}→{new[:8]}")
    return ("sid drift corrected: " + ", ".join(fixed)) if fixed else ""


def _sweep_quietly(p: Paths) -> str:
    """`sweep` narrates; the warden keeps a log, not a console. Only the lines that mean
    something (a compaction, a warning) are worth a line in it."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            sweepmod.sweep("", p)
    except Exception as e:                       # a failing sweep must not kill the warden
        return f"sweep failed: {e}"
    keep = [l for l in buf.getvalue().splitlines() if "compact" in l or "⚠" in l]
    return " ".join(keep)


def loop(p: Paths) -> int:
    """It must be boring: it runs for hours, unattended, through the exact event that kills
    everything else on the machine."""
    tick, every, poke = tick_secs(), sweep_every(), poke_every()
    log(f"warden up (tick {tick}s, sweep every {every}s, poke every {poke}s)", p)
    last_sweep = 0.0
    stop = {"now": False}

    # Per-agent rescue state, in memory: the warden is long-lived, and a restart just
    # re-baselines (it will poke once more and read the transcript afresh — no harm).
    attempt_after: dict[str, float] = {}   # do not poke this agent again before this epoch
    baseline: dict[str, int] = {}          # its end_turn count at our last poke

    def _forget(a: str) -> None:
        attempt_after.pop(a, None)
        baseline.pop(a, None)

    def _term(_signum, _frame):
        stop["now"] = True

    for s in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(s, _term)

    while not stop["now"]:
        time.sleep(tick)
        if not pidfile(p).is_file():
            log("stopped", p)
            return 0

        # THE CONTEXT GUARD, on a clock. `sweep` itself is the careful one: it skips agents
        # that are generating, agents on a permission prompt, agents out of quota and agents
        # below their threshold; it compacts a BUSY agent only past the HARD line, where
        # running out of context would lose everything anyway.
        if not sweep_off() and time.time() - last_sweep >= every:
            last_sweep = time.time()
            healed = _heal_sids(p)          # correct the sid files BEFORE sweep reads them
            if healed:
                log(healed, p)
            out = _sweep_quietly(p)
            if out:
                log(f"sweep: {out}", p)

        marked: list[str] = []
        for a in line_agents(p):
            # Hook marker: this agent was cut off MID-TURN. That is the case that needs a
            # wake; an agent that was idle when the limit landed lost nothing.
            if p.limited(a).is_file():
                marked.append(a)
                continue
            # Belt: the hook may not have fired (old version, lost +x — hooks FAIL OPEN and
            # say nothing). Believe the screen too.
            if pane_limited(a, p):
                p.state.mkdir(parents=True, exist_ok=True)
                p.limited(a).write_text(f"{int(time.time())}\t{_sid(a, p)}\tpane\n",
                                        encoding="utf-8")
                log(f"{a}: limit detected on the PANE (the StopFailure hook did not fire — "
                    f"check it)", p)
                marked.append(a)
        if not marked:
            continue

        now = int(time.time())
        # RESCUE by ground truth, not by a scraped clock (see the module docstring). No
        # resets_at gate: poke on a capped interval and watch the transcript for a turn to land.
        for a in marked:
            if not tmux.has_session(p.session(a)):
                log(f"{a}: gone — dropping its marker", p)
                p.limited(a).unlink(missing_ok=True)
                _forget(a)
                continue
            # The name is the same; is the AGENT? A fresh spawn minted a new sid and never
            # lived through the limit — it must not be told to "carry on where you were cut off".
            when, msid = marker(a, p)
            nsid = _sid(a, p)
            if msid and nsid and msid != nsid:
                log(f"{a}: session changed since it was cut off — a different agent holds the "
                    f"name; dropping the marker", p)
                p.limited(a).unlink(missing_ok=True)
                _forget(a)
                continue
            pr = probemod.probe(a, p)
            # RECOVERED: a turn LANDED since our last poke → the window really reopened. This is
            # the only signal that survives an account switch — the pane keeps the old wall in
            # scrollback, but the transcript's end_turn count climbs only when a real turn
            # completes. It rests on one assumption: a rate-limited turn writes NO end_turn (the
            # same assumption drive.wait_turn makes), so a poke that errors cannot look like
            # recovery.
            #
            # Only NOW do we mail the WAKE — on this confirmed-working turn. Mailing it at poke
            # time would lose it: the poke's `!bash $AF_MAIL read` runs even under the limit and
            # ACKs the letter (the cursor is the ack), while the model turn that would have read
            # it errors — so the reopened agent would read an empty box and never be told to
            # resume. Deliver on recovery, and the very next turn carries the instruction.
            if a in baseline and pr.endturns > baseline[a]:
                why = f"at {datetime.fromtimestamp(when).strftime('%H:%M')}" if when else "earlier"
                wake(a, why, p)
                p.limited(a).unlink(missing_ok=True)
                _forget(a)
                log(f"{a}: recovered — a turn landed; woken to pick the work back up", p)
                continue
            if now < attempt_after.get(a, 0.0):
                continue                       # inside the poke interval: wait and keep watching
            # POKE: trigger a turn to test whether the window reopened — ring only, no WAKE yet
            # (see above). The doorbell's `!mail read` consumes only mail already in the box,
            # which a peer's own ring would have consumed anyway, so it adds no new loss.
            drive.ring(a, p)
            baseline[a] = pr.endturns
            attempt_after[a] = now + poke
            log(f"{a}: poked — watching for a turn to land", p)
            time.sleep(STAGGER)
    log("stopped (signal)", p)
    return 0


# --- commands -----------------------------------------------------------------------
def watch(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if _live(pid):
        print(f"[warden] already watching line '{p.slug}' (pid {pid}). Stop it first: "
              f"af warden stop")
        return 0
    p.state.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({"AF_ROOT": str(p.root), "AF_SLUG": p.slug, "AF_CWD": str(p.cwd)})
    # The warden is nobody's agent. If it was started from inside an agent's pane, AF_AGENT
    # is still set — and sweep would then read that name as "self" and refuse to ever compact
    # it: the longest-lived, least-guarded station, exactly the 767k-overnight case. Scrub it.
    for k in ("AF_AGENT", "AF_ROLE"):
        env.pop(k, None)
    # The warden is a child of whatever spawned the line, but it must OUTLIVE it: the usage
    # limit kills the orchestrator too, and a watcher in its process group would die with it.
    env["PYTHONPATH"] = os.pathsep.join(
        x for x in (str(FACTORY_DIR), env.get("PYTHONPATH", "")) if x)
    proc = subprocess.Popen(
        [sys.executable, "-m", "af.warden", "_loop"],
        cwd=str(p.cwd), env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pidfile(p).write_text(str(proc.pid), encoding="utf-8")
    print(f"[warden] watching line '{p.slug}' (pid {proc.pid}).")
    print(f"[warden]   compacts idle agents past their threshold every "
          f"{sweep_every() // 60}m — with or without you")
    print("[warden]   and wakes the line when the usage limit resets. It spends no tokens, "
          "so the limit cannot kill it.")
    print(f"[warden]   log: {logfile(p)}")
    return 0


def stop(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if not pid:
        print(f"[warden] not watching line '{p.slug}'.")
        return 0
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    pidfile(p).unlink(missing_ok=True)
    print("[warden] watcher stopped.")
    return 0


def status(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if _live(pid):
        print(f"[warden] watcher: LIVE (pid {pid})")
    else:
        print("[warden] watcher: not running  — start it: af warden watch")

    reset, pct = resets_at(p), used_pct(p)
    if reset:
        left = max(0, reset - int(time.time()))
        at = datetime.fromtimestamp(reset).strftime("%H:%M")
        print(f"  5-hour quota : {pct if pct is not None else '?'}% used, resets in "
              f"{left // 3600}h{(left % 3600) // 60:02d}m ({at})")
    else:
        print("  5-hour quota : unknown — no agent has rendered a statusline yet")
        print("                 (without it the watcher cannot know WHEN to wake anyone)")

    any_ = False
    for a in line_agents(p):
        if not p.limited(a).is_file():
            continue
        when, _sidv = marker(a, p)
        at = datetime.fromtimestamp(when).strftime("%H:%M") if when else "?"
        print(f"  cut off      : {a} (at {at})")
        any_ = True
    if not any_:
        print("  cut off      : nobody")

    lf = logfile(p)
    print(f"  log          : {lf}")
    if lf.is_file():
        tail = [l for l in lf.read_text(encoding="utf-8", errors="replace").splitlines()
                if "sweep:" in l]
        print(f"  last sweep   : {tail[-1] if tail else 'nothing to compact yet'}")
    else:
        print("  last sweep   : never (the watcher has not run)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af.warden", description="the line's unattended watcher")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch", help="start the watcher for this line (safe to re-run)")
    sub.add_parser("stop", help="stop it")
    sub.add_parser("status", help="quota, reset time, who got cut off, last sweep")
    sub.add_parser("_loop", help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    p = paths()
    if a.cmd == "watch":
        return watch(p)
    if a.cmd == "stop":
        return stop(p)
    if a.cmd == "status":
        return status(p)
    if a.cmd == "_loop":
        return loop(p)
    return 1


if __name__ == "__main__":
    sys.exit(main())
