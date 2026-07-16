"""warden — the thing that watches a squad WHEN NOBODY IS DRIVING IT.

    python3 -m af.warden watch     start it for this squad (detached; safe to re-run)
    python3 -m af.warden watch --target <session>  guard a single tmux session (standalone)
    python3 -m af.warden stop
    python3 -m af.warden status     quota, reset time, who got cut off, when it last swept

Two jobs, and they are the same job: keep the squad alive through the hours when no human and
no orchestrator is issuing commands.

  1. CONTEXT. `sweep` compacts agents past their threshold — but it only ever ran from
     `post` / `mail` / `sweep`, i.e. only when the DRIVING session spoke. An autonomous squad
     does not go through those: its agents mail each other, and mail never swept. So a squad
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
    only for the account that rendered it, and a squad's agents get moved between accounts, so a
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
from .paths import FACTORY_DIR, Paths, paths, SPEC_HOME
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
# pidfile/logfile under durable_state (not /tmp) so a /tmp purge cannot drop the pidfile
# mid-run and spawn a second warden with no coordination — the same bug postmaster.py's
# own module docstring documents.
def _standalone_dir(target: str) -> Path:
    """Durable state dir for a standalone warden watching one target."""
    return SPEC_HOME / "state" / "_standalone" / target


def pidfile(p: Paths) -> Path:
    return p.durable_state / "warden.pid"


def logfile(p: Paths) -> Path:
    return p.durable_state / "warden.log"


def last_sweep_file(p: Paths) -> Path:
    # Just the epoch the last sweep CHECK ran (whether or not it found anything to compact) —
    # so `af ledger` can read "seconds until the next one" off disk without signaling this
    # process at all. `loop()`'s own `last_sweep` lives only in memory and resets to 0 on
    # every warden restart; this file is the one externally-visible copy of it.
    return p.durable_state / "last-sweep"


def _write_last_sweep(p: Paths, at: float) -> None:
    try:
        p.durable_state.mkdir(parents=True, exist_ok=True)
        last_sweep_file(p).write_text(str(int(at)), encoding="utf-8")
    except OSError:
        pass   # best-effort: a failed write only degrades ledger's ETA, never the sweep itself


def next_sweep_in(p: Paths) -> int | None:
    """Seconds until the next sweep check, for a read-only display. None when there is
    nothing to report: no warden running, or it has not swept even once yet."""
    if sweep_off():
        return None
    if not _live(_pid(p)):
        return None
    try:
        last = int(last_sweep_file(p).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return sweep_every() - (int(time.time()) - last)


def _standalone_pidfile(target: str) -> Path:
    d = _standalone_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    return d / "warden.pid"


def _standalone_logfile(target: str) -> Path:
    d = _standalone_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    return d / "warden.log"


def log(msg: str, p: Paths) -> None:
    p.durable_state.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m-%d %H:%M:%S")
    with logfile(p).open("a", encoding="utf-8") as f:
        f.write(f"[warden {stamp}] {msg}\n")


def _standalone_log(msg: str, target: str) -> None:
    d = _standalone_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m-%d %H:%M:%S")
    with _standalone_logfile(target).open("a", encoding="utf-8") as f:
        f.write(f"[warden {stamp}] {msg}\n")


def _pid(p: Paths) -> int:
    try:
        return int(pidfile(p).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _standalone_pid(target: str) -> int:
    try:
        return int(_standalone_pidfile(target).read_text(encoding="utf-8").strip())
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


def pane_limited_target(target: str) -> bool:
    """Check if a standalone target's pane shows the usage limit."""
    pane = tmux.capture_pane(target)
    return bool(pane and patterns.USAGE_LIMIT.search(pane))


def squad_agents(p: Paths) -> list[str]:
    """Every agent of THIS squad that has a session."""
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
def wake(agent: str, why: str, p: Paths, dedup_key: str = "") -> None:
    """By MAIL, never by typing the message itself: the doorbell is one fixed token-free
    line, and the letter lands in the mailbox whatever state the pane is in.

    From `orchestrator`, so the agent's reply lands where the human reads it.

    `dedup_key` identifies the LIMIT EPISODE, not the call. The caller drops the agent's
    marker only after this returns, so a warden killed in between (a reboot, a TERM, a crash)
    re-enters the same episode on its next tick, sees the same recovery, and mails a second
    identical WAKE — "carry on where you were cut off", twice, into an agent that already did.
    A key keyed on the episode makes the retry a no-op instead."""
    try:
        mailbox.send(agent, WAKE.format(why=why), kind="task", frm="orchestrator", p=p,
                     dedup_key=dedup_key or None)
    except (OSError, ValueError) as e:
        log(f"{agent}: could not mail the wake-up ({e})", p)
        return
    drive.ring(agent, p)


def _poke_target(target: str) -> None:
    """Poke a standalone target by typing the doorbell into its pane."""
    drive.ring_target(target, DOORBELL)


# --- the loop -----------------------------------------------------------------------
def _heal_sids(p: Paths) -> str:
    """Before every sweep, point each agent's sid file at the session it is ACTUALLY running.

    Claude Code forks the session on --resume, so an agent rescued from a usage limit — or
    restarted by a human — is now on a new id while its sid file still names the frozen parent.
    Sweep would then read the parent's stale context (the "compacting an agent already at 0%"
    loop). This is the same repair heal does by hand; the warden does it on its own clock so a
    parked, unattended squad self-corrects without anyone running a command.
    """
    fixed = []
    for a in squad_agents(p):
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


def _find_orchestrator(p: Paths) -> str | None:
    """Find the orchestrator station name for this slug.

    Primary: read from squad.json's Station.role field.
    Fallback: if the roster has no record, check for a station literally named "orc" or
    "orchestrator" (defensive fallback only).
    Returns None if no orchestrator can be identified."""
    try:
        from . import roster
        for st in roster.stations(p):
            if st.role == "orchestrator":
                return st.name
    except Exception:
        pass
    # Defensive fallback: check for literally-named orchestrator
    for a in squad_agents(p):
        if a in ("orc", "orchestrator"):
            return a
    return None


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
        # below their threshold; it compacts any sweepable agent past its threshold,
        # busy or idle — no distinction is made.
        if not sweep_off() and time.time() - last_sweep >= every:
            last_sweep = time.time()
            _write_last_sweep(p, last_sweep)
            healed = _heal_sids(p)          # correct the sid files BEFORE sweep reads them
            if healed:
                log(healed, p)
            out = _sweep_quietly(p)
            if out:
                log(f"sweep: {out}", p)

        marked: list[str] = []
        for a in squad_agents(p):
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
        # Find the orchestrator — only it gets actively poked
        orchestrator = _find_orchestrator(p)

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
                # The episode, not the attempt: (agent, when it was cut off, which session).
                # Stable across a warden restart mid-rescue; different for the next real limit.
                wake(a, why, p, dedup_key=f"wake:{a}:{when}:{msid}")
                p.limited(a).unlink(missing_ok=True)
                _forget(a)
                log(f"{a}: recovered — a turn landed; woken to pick the work back up", p)
                continue
            if now < attempt_after.get(a, 0.0):
                continue                       # inside the poke interval: wait and keep watching

            # POKE-ONLY-ORCHESTRATOR: only the orchestrator gets actively poked (typed into).
            # Non-orchestrator agents still get their baseline tracked via probe() (cheap,
            # read-only, no keystrokes) so their marker can be cleared the moment a turn
            # naturally lands for them (e.g. once the revived orchestrator re-tasks them by mail).
            if a == orchestrator:
                # POKE: trigger a turn to test whether the window reopened — ring only, no WAKE yet
                # (see above). The doorbell's `!mail read` consumes only mail already in the box,
                # which a peer's own ring would have consumed anyway, so it adds no new loss.
                drive.ring(a, p)
                baseline[a] = pr.endturns
                attempt_after[a] = now + poke
                log(f"{a}: poked — watching for a turn to land", p)
                time.sleep(STAGGER)
            else:
                # Non-orchestrator: just record baseline, never poke.
                # The orchestrator will re-drive them by mail once it recovers.
                baseline[a] = pr.endturns
                attempt_after[a] = now + poke
                log(f"{a}: baseline recorded (not orchestrator — waiting for orchestrator to re-drive)", p)
    log("stopped (signal)", p)
    return 0


def _standalone_loop(target: str) -> int:
    """Loop for standalone mode: guard a single tmux session by session id."""
    tick, every, poke = tick_secs(), sweep_every(), poke_every()
    _standalone_log(f"standalone warden up for '{target}' (tick {tick}s, sweep every {every}s, poke every {poke}s)", target)
    last_sweep = 0.0
    stop = {"now": False}

    # Resolve the session id once at startup
    sid = live.sid_in_pane(target)
    if sid is None:
        _standalone_log(f"no --session-id/--resume found in pane '{target}' — cannot track", target)
        return 1

    attempt_after: dict[str, float] = {}
    baseline: dict[str, int] = {}

    def _forget() -> None:
        attempt_after.clear()
        baseline.clear()

    def _term(_signum, _frame):
        stop["now"] = True

    for s in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(s, _term)

    while not stop["now"]:
        time.sleep(tick)
        if not _standalone_pidfile(target).is_file():
            _standalone_log("stopped (pidfile removed)", target)
            return 0

        # Check session still exists
        if not tmux.has_session(target):
            _standalone_log(f"session '{target}' gone — stopping", target)
            return 0

        # Re-resolve sid (in case of fork)
        current_sid = live.sid_in_pane(target)
        if current_sid is None:
            _standalone_log(f"lost session id for '{target}' — stopping", target)
            return 0

        # Context guard: compact if needed
        if not sweep_off() and time.time() - last_sweep >= every:
            last_sweep = time.time()
            # For standalone, we use drive.maybe_autocompact directly against the target
            # rather than going through sweep.sweep() which is squad/mailbox-shaped
            try:
                pr = probemod.probe_target(target, current_sid)
                if pr.alive and pr.phase not in ("generating", "permission", "limited"):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        drive.maybe_autocompact_target(target, current_sid, nowait=True)
                    out = " ".join(l for l in buf.getvalue().splitlines() if "compact" in l or "⚠" in l)
                    if out:
                        _standalone_log(f"sweep: {out}", target)
            except Exception as e:
                _standalone_log(f"sweep failed: {e}", target)

        # Check for limit marker (standalone uses a file under the standalone dir)
        limited_file = _standalone_dir(target) / "limited"
        marked = False

        if limited_file.is_file():
            marked = True
        elif pane_limited_target(target):
            limited_file.write_text(f"{int(time.time())}\t{current_sid}\tpane\n", encoding="utf-8")
            _standalone_log(f"limit detected on the PANE (the StopFailure hook did not fire — check it)", target)
            marked = True

        if not marked:
            continue

        now = int(time.time())

        # Read marker
        try:
            cols = limited_file.read_text(encoding="utf-8").split("\t")
            when = intish(cols[0], 0) if cols else 0
            msid = cols[1].strip() if len(cols) > 1 else ""
        except OSError:
            when, msid = 0, ""

        # Check if session changed since cutoff
        if msid and current_sid and msid != current_sid:
            _standalone_log(f"session changed since cutoff — a different agent holds the name; dropping the marker", target)
            limited_file.unlink(missing_ok=True)
            _forget()
            continue

        pr = probemod.probe_target(target)

        # RECOVERED: a turn landed since our last poke
        if "target" in baseline and pr.endturns > baseline["target"]:
            why = f"at {datetime.fromtimestamp(when).strftime('%H:%M')}" if when else "earlier"
            # For standalone, we type the WAKE message directly into the pane
            _standalone_wake(target, why)
            limited_file.unlink(missing_ok=True)
            _forget()
            _standalone_log(f"recovered — a turn landed; woken to pick the work back up", target)
            continue

        if now < attempt_after.get("target", 0.0):
            continue

        # POKE: trigger a turn to test whether the window reopened
        _poke_target(target)
        baseline["target"] = pr.endturns
        attempt_after["target"] = now + poke
        _standalone_log(f"poked — watching for a turn to land", target)

    _standalone_log("stopped (signal)", target)
    return 0


def _standalone_wake(target: str, why: str) -> None:
    """Type the WAKE message directly into the standalone target's pane."""
    msg = WAKE.format(why=why)
    # Type the message into the pane
    drive.say_target(target, msg)


# --- commands -----------------------------------------------------------------------
def watch(p: Paths | None = None, target: str | None = None) -> int:
    if target is not None:
        return _watch_standalone(target)
    return _watch_squad(p)


def _watch_squad(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if _live(pid):
        print(f"[warden] already watching squad '{p.slug}' (pid {pid}). Stop it first: "
              f"af warden stop")
        return 0
    p.durable_state.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({"AF_ROOT": str(p.root), "AF_SLUG": p.slug, "AF_CWD": str(p.cwd)})
    # The warden is nobody's agent. If it was started from inside an agent's pane, AF_AGENT
    # is still set — and sweep would then read that name as "self" and refuse to ever compact
    # it: the longest-lived, least-guarded station, exactly the 767k-overnight case. Scrub it.
    for k in ("AF_AGENT", "AF_ROLE"):
        env.pop(k, None)
    # The warden is a child of whatever spawned the squad, but it must OUTLIVE it: the usage
    # limit kills the orchestrator too, and a watcher in its process group would die with it.
    env["PYTHONPATH"] = os.pathsep.join(
        x for x in (str(FACTORY_DIR), env.get("PYTHONPATH", "")) if x)
    proc = subprocess.Popen(
        [sys.executable, "-m", "af.warden", "_loop"],
        cwd=str(p.cwd), env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pidfile(p).write_text(str(proc.pid), encoding="utf-8")
    print(f"[warden] watching squad '{p.slug}' (pid {proc.pid}).")
    print(f"[warden]   compacts agents past their threshold every "
          f"{sweep_every() // 60}m — with or without you")
    print("[warden]   and wakes the squad when the usage limit resets. It spends no tokens, "
          "so the limit cannot kill it.")
    print(f"[warden]   log: {logfile(p)}")
    return 0


def _watch_standalone(target: str) -> int:
    """Start a standalone warden for a single tmux session."""
    # Check tmux session exists
    if not tmux.has_session(target):
        print(f"[warden] no such tmux session '{target}' — the warden only guards a live "
              f"tmux session, wrap yours in tmux first.")
        return 1

    # Check for existing standalone warden
    pid = _standalone_pid(target)
    if _live(pid):
        print(f"[warden] already watching '{target}' (pid {pid}). Stop it first: "
              f"af warden stop --target {target}")
        return 0

    # Check for session id
    sid = live.sid_in_pane(target)
    if sid is None:
        print(f"[warden] no --session-id/--resume found in that pane's claude process — "
              f"relaunch it with an explicit --session-id so the warden can track it across "
              f"a fork, e.g.: claude --session-id $(uuidgen)")
        return 1

    # Start the standalone loop
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        x for x in (str(FACTORY_DIR), env.get("PYTHONPATH", "")) if x)
    proc = subprocess.Popen(
        [sys.executable, "-m", "af.warden", "_standalone_loop", target],
        cwd=os.getcwd(), env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _standalone_dir(target).mkdir(parents=True, exist_ok=True)
    _standalone_pidfile(target).write_text(str(proc.pid), encoding="utf-8")
    print(f"[warden] watching '{target}' (pid {proc.pid}, sid {sid[:8]}…).")
    print(f"[warden]   log: {_standalone_logfile(target)}")
    return 0


def stop(p: Paths | None = None, target: str | None = None) -> int:
    if target is not None:
        return _stop_standalone(target)
    return _stop_squad(p)


def _stop_squad(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if not pid:
        print(f"[warden] not watching squad '{p.slug}'.")
        return 0
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    pidfile(p).unlink(missing_ok=True)
    print("[warden] watcher stopped.")
    return 0


def _stop_standalone(target: str) -> int:
    pid = _standalone_pid(target)
    if not pid:
        print(f"[warden] not watching '{target}'.")
        return 0
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    _standalone_pidfile(target).unlink(missing_ok=True)
    print(f"[warden] watcher for '{target}' stopped.")
    return 0


def status(p: Paths | None = None, target: str | None = None) -> int:
    if target is not None:
        return _status_standalone(target)
    return _status_line(p)


def _status_line(p: Paths | None = None) -> int:
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
    for a in squad_agents(p):
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


def _status_standalone(target: str) -> int:
    pid = _standalone_pid(target)
    if _live(pid):
        print(f"[warden] watcher for '{target}': LIVE (pid {pid})")
    else:
        print(f"[warden] watcher for '{target}': not running — start it: af warden watch --target {target}")

    sid = live.sid_in_pane(target)
    if sid:
        print(f"  session id   : {sid}")
    else:
        print("  session id   : unknown")

    limited_file = _standalone_dir(target) / "limited"
    if limited_file.is_file():
        print("  cut off      : yes")
    else:
        print("  cut off      : no")

    lf = _standalone_logfile(target)
    print(f"  log          : {lf}")
    if lf.is_file():
        tail = [l for l in lf.read_text(encoding="utf-8", errors="replace").splitlines()
                if "sweep:" in l]
        print(f"  last sweep   : {tail[-1] if tail else 'nothing to compact yet'}")
    else:
        print("  last sweep   : never")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af.warden", description="the squad's unattended watcher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    watch_p = sub.add_parser("watch", help="start the watcher for this squad (safe to re-run)")
    watch_p.add_argument("--target", type=str, default=None,
                         help="standalone mode: guard a single tmux session by name")

    stop_p = sub.add_parser("stop", help="stop it")
    stop_p.add_argument("--target", type=str, default=None,
                        help="stop the standalone watcher for this target")

    status_p = sub.add_parser("status", help="quota, reset time, who got cut off, last sweep")
    status_p.add_argument("--target", type=str, default=None,
                          help="status for a standalone target")

    sub.add_parser("_loop", help=argparse.SUPPRESS)
    sl_p = sub.add_parser("_standalone_loop", help=argparse.SUPPRESS)
    sl_p.add_argument("target", type=str, help="tmux session target")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    p = paths()
    if a.cmd == "watch":
        return watch(p, target=getattr(a, "target", None))
    if a.cmd == "stop":
        return stop(p, target=getattr(a, "target", None))
    if a.cmd == "status":
        return status(p, target=getattr(a, "target", None))
    if a.cmd == "_loop":
        return loop(p)
    if a.cmd == "_standalone_loop":
        return _standalone_loop(a.target)
    return 1


if __name__ == "__main__":
    sys.exit(main())
