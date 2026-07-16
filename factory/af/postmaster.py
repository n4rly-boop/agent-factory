"""postmaster — the process that owns squad.json and the mail safety net WHEN NOBODY IS
DRIVING THE TEAM.

    python3 -m af.postmaster watch     start it for this slug (detached; safe to re-run)
    python3 -m af.postmaster stop
    python3 -m af.postmaster status    last reconcile, last ring-catch, pid

Mail delivery keeps working with ZERO daemon running — that is load-bearing, not a
migration step. `mailbox.send` + `drive.ring` are synchronous and self-sufficient: an
agent calling `bash $AF_MAIL send` wakes its recipient on its own, and a human's `af post`
does too. Nothing here may become a prerequisite for that.

What was actually missing, per the redesign doc:

  1. STATE. `squad.json` (af.roster) is the durable roster, but nothing keeps it honest
     while no `af` command is running — `up`/`down` write it, `reconcile()` corrects it
     against tmux+ps, but a team working unattended never calls a command, so nobody
     ever calls reconcile either. This daemon is the thing that does, on a clock.

  2. THE RING SAFETY NET. The synchronous ring is best-effort: it can fail (a pane that
     is mid-fork, a transient tmux hiccup) or never be attempted (a message appended
     through the low-level mailbox API without a paired ring). A message that misses its
     ring is not lost — `unread` still counts it — but nothing else will ever ring it
     again, so it sits until the recipient happens to run `mail` on its own. The
     postmaster catches that: any mailbox whose message COUNT has grown since the last
     tick and still has unread mail gets one ring attempt. Growth is keyed on the
     append-only total, not on `unread` itself — `unread` drops the instant the recipient
     reads, so a read and a fresh arrival landing in the same tick window could otherwise
     mask each other and strand a message permanently (see `_ring_catch`).

  3. DEDUP. `mailbox.send(..., dedup_key=...)` (new, see af.mailbox) lets a caller that
     might retry send idempotently — a stable key makes a second send with the same key
     return the already-sent message instead of appending a duplicate. Not yet wired into
     any caller (this daemon's own ring-catch only retypes the doorbell; it never
     re-sends mail), but it is the primitive a future retry-prone sender — the warden's
     wake, a polling tick — reaches for instead of inventing its own guard.

One process per slug, like the warden — but this is the HOT path (short tick, cheap
per-tick work: one `ps -A` call via roster.reconcile, one unread count per mailbox) where
the warden is the COLD path (a five-minute clock, full transcript scans). Different
cadences, different blast radius: if this dies, mail still flows (synchronous send+ring
still works); if the warden dies, mail still flows too — only the unattended-context-
guard and limit-rescue stop.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import drive, mailbox, roster
from .nums import intish
from .paths import FACTORY_DIR, Paths, paths


def _int_env(k: str, default: int) -> int:
    import os
    return intish((os.environ.get(k) or "").strip(), default)


def tick_secs() -> int:
    return _int_env("AI_POSTMASTER_TICK", 5)


# --- pidfile / log, under the DURABLE state dir (not /tmp) -------------------------
# The warden's pidfile lived in AF_ROOT (default /tmp/agent-factory), and a /tmp purge
# dropping it mid-run spawned a second watcher with no coordination. durable_state lives
# under ~/.claude/agent-factory, which nothing purges on a whim.
def pidfile(p: Paths) -> Path:
    return p.durable_state / "postmaster.pid"


def logfile(p: Paths) -> Path:
    return p.durable_state / "postmaster.log"


def log(msg: str, p: Paths) -> None:
    p.durable_state.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m-%d %H:%M:%S")
    with logfile(p).open("a", encoding="utf-8") as f:
        f.write(f"[postmaster {stamp}] {msg}\n")


def _pid(p: Paths) -> int:
    try:
        return int(pidfile(p).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _live(pid: int) -> bool:
    if pid <= 0:
        return False
    import os
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --- the loop -----------------------------------------------------------------------
def _ring_catch(p: Paths, last_total: dict[str, int]) -> list[str]:
    """One ring attempt for every mailbox that has grown since the last tick and still has
    unread mail — the safety net for a message whose synchronous ring failed or was never
    attempted.

    Growth is keyed on `mailbox.total` (the mailbox's append-only line count), NOT
    `unread`. Unread is not monotonic — it drops the instant the recipient reads — so a
    read and a fresh arrival landing in the same tick window can silently mask each
    other: unread goes 3 (stuck) -> 0 (agent drains it) -> 1 (new message, ring failed),
    and comparing only against the last-seen unread (1) vs the PRIOR tick's stuck value
    (3) would read 1 > 3 as false and never catch it again — a permanently stranded
    message. `total` only ever increases when mail is actually appended, so it cannot be
    fooled by an interleaved read; `unread > 0` alongside it is still required so a
    mailbox that grew but was already fully read (e.g. delivered by the normal
    synchronous path between ticks) is not re-rung for nothing.

    A mailbox whose total stops growing (recipient genuinely down, or unread mail it is
    simply ignoring) is deliberately left alone tick after tick — re-ringing a pane that
    cannot answer is noise, not help."""
    rung = []
    for agent in roster.load(p).agents:
        try:
            tot = mailbox.total(agent, p)
        except Exception:
            continue
        # Default to 0, not `tot`: a freshly (re)started postmaster has no prior baseline
        # for an agent it has never seen, and defaulting to the CURRENT total would make
        # the first-ever observation always look like "no growth" — silently skipping any
        # mail that was already stuck before this daemon started watching.
        prev = last_total.get(agent, 0)
        last_total[agent] = tot
        if tot > prev:
            try:
                unread = mailbox.unread(agent, p)
            except Exception:
                unread = 0
            if unread > 0 and drive.ring(agent, p):
                rung.append(agent)
    return rung


def loop(p: Paths) -> int:
    tick = tick_secs()
    log(f"postmaster up (tick {tick}s)", p)
    last_total: dict[str, int] = {}
    stop = {"now": False}

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
        try:
            roster.reconcile(p)
        except Exception as e:
            log(f"reconcile failed: {e}", p)
        try:
            rung = _ring_catch(p, last_total)
        except Exception as e:
            log(f"ring-catch failed: {e}", p)
            rung = []
        if rung:
            log(f"ring-catch: {', '.join(rung)}", p)
    log("stopped (signal)", p)
    return 0


# --- commands -----------------------------------------------------------------------
def watch(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if _live(pid):
        print(f"[postmaster] already watching '{p.slug}' (pid {pid}). Stop it first: "
              f"af postmaster stop")
        return 0
    p.durable_state.mkdir(parents=True, exist_ok=True)

    import os
    env = dict(os.environ)
    env.update({"AF_ROOT": str(p.root), "AF_SLUG": p.slug, "AF_CWD": str(p.cwd)})
    for k in ("AF_AGENT", "AF_ROLE"):
        env.pop(k, None)
    env["PYTHONPATH"] = os.pathsep.join(
        x for x in (str(FACTORY_DIR), env.get("PYTHONPATH", "")) if x)
    proc = subprocess.Popen(
        [sys.executable, "-m", "af.postmaster", "_loop"],
        cwd=str(p.cwd), env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pidfile(p).write_text(str(proc.pid), encoding="utf-8")
    print(f"[postmaster] watching '{p.slug}' (pid {proc.pid}).")
    print(f"[postmaster]   reconciles squad.json and catches missed doorbells every "
          f"{tick_secs()}s.")
    print(f"[postmaster]   log: {logfile(p)}")
    return 0


def stop(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if not pid:
        print(f"[postmaster] not watching '{p.slug}'.")
        return 0
    import os
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    pidfile(p).unlink(missing_ok=True)
    print("[postmaster] watcher stopped.")
    return 0


def status(p: Paths | None = None) -> int:
    p = p or paths()
    pid = _pid(p)
    if _live(pid):
        print(f"[postmaster] watcher: LIVE (pid {pid})")
    else:
        print("[postmaster] watcher: not running — start it: af postmaster watch")
    lf = logfile(p)
    print(f"  log: {lf}")
    if lf.is_file():
        lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
        recon = [l for l in lines if "reconcile failed" in l]
        catches = [l for l in lines if "ring-catch:" in l]
        print(f"  last ring-catch: {catches[-1] if catches else 'nothing caught yet'}")
        if recon:
            print(f"  ⚠ reconcile errors seen: {len(recon)} (last: {recon[-1]})")
    else:
        print("  last ring-catch: never (the watcher has not run)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af.postmaster",
                                  description="the team's state + mail-safety-net daemon")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch", help="start the watcher for this slug (safe to re-run)")
    sub.add_parser("stop", help="stop it")
    sub.add_parser("status", help="pid, last reconcile, last ring-catch")
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
