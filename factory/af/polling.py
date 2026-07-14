"""polling — put an agent on a timer. Every N MINUTES it gets the same message.

    python3 -m af.polling start <agent> <minutes> <message> [--times N] [--kind K]
    python3 -m af.polling stop  [agent]     # no agent = $AF_AGENT: an agent switching
                                            # its OWN timer off
    python3 -m af.polling list              # every timer on this line
    python3 -m af.polling status <agent>

The interval is MINUTES, everywhere — the argument, the state file, every message this
prints, and the hint the agent gets. Seconds exist only inside the sleep. A tool whose flag
says one unit and whose docs say another is how someone eventually types `polling start orc
20` meaning twenty minutes and gets a tick every twenty seconds.

WHY IT DELIVERS BY MAIL AND NOT BY TYPING. The obvious build is `sleep N; say <agent> "…"`
in a loop. That types into a live TUI on a timer with no idea what the agent is doing at
that instant — mid-turn, on a permission prompt, halfway through a tool call. The message
lands inside someone else's turn. Mail has a mailbox, a cursor and a doorbell: the letter is
APPENDED (always safe, whatever the agent is doing) and only the doorbell is typed. That is
the channel; polling just rings it on a clock.

THREE WAYS A TIMER TURNS INTO A WEAPON, AND WHAT STOPS EACH:

1. IT OUTLIVES ITS AGENT. The agent dies, the loop keeps running, and the next agent to take
   that name inherits a stream of orders meant for a ghost. The loop records the agent's SID
   at start and re-checks it every tick: a changed sid means a different agent wearing the
   same name, and the timer EXITS rather than talk to it. (`revive` / `line up --resume` keep
   the sid — a resumed agent keeps its timer. A fresh spawn mints a new one, and gets a clean
   slate.)

2. IT OUTRUNS THE AGENT. An interval shorter than a turn means every tick lands on an agent
   still working on the last one. The mailbox grows, and the agent spends its life reading
   its own alarm clock. So: a tick is SKIPPED while the previous one is still unread. The
   timer cannot build a backlog — one outstanding message, ever. (And the floor is 1 minute.
   A poller is not a busy-wait.)

3. NOBODY REMEMBERS IT EXISTS. A forgotten timer burns tokens forever. `--times N` exists for
   that, `list` shows every live one, and the agent itself can run `bash $AF_POLL stop` — it
   is the one that knows when the wait is over.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import drive, mailbox, tmux
from .paths import FACTORY_DIR, Paths, paths
from .nums import intish

FIRST_TICK_HINT = """

(You are on a timer: this message repeats every {mins} min. When the wait is over, switch it off yourself: bash $AF_POLL stop)"""


def min_minutes() -> int:
    v = (os.environ.get("AI_POLL_MIN") or "").strip()
    return intish(v, 1, positive=True)


def _dir(agent: str, p: Paths) -> Path:
    return p.polldir / agent


def _get(agent: str, key: str, p: Paths, default: str = "") -> str:
    try:
        return _dir(agent, p).joinpath(key).read_text(encoding="utf-8")
    except OSError:
        return default


def _put(agent: str, key: str, val: str, p: Paths) -> None:
    d = _dir(agent, p)
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath(key).write_text(val, encoding="utf-8")


def _pid(agent: str, p: Paths) -> int:
    v = _get(agent, "pid", p).strip()
    return intish(v, 0)


def running(agent: str, p: Paths) -> bool:
    """A timer is running iff its pid is."""
    pid = _pid(agent, p)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sid(agent: str, p: Paths) -> str:
    try:
        return p.sid_file(agent).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _log(agent: str, msg: str, p: Paths) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[poll {stamp}] {agent}: {msg}\n"
    d = _dir(agent, p)
    d.mkdir(parents=True, exist_ok=True)
    with d.joinpath("log").open("a", encoding="utf-8") as f:
        f.write(line)


def parse_minutes(raw: str) -> int:
    """`5m` is what a human types when the unit is minutes. Take it; do not make them
    discover by silence that it parsed as garbage."""
    v = raw.strip()
    for suffix in ("min", "m"):
        if v.endswith(suffix):
            v = v[: -len(suffix)]
            break
    n = intish(v, None)
    if n is None:
        raise ValueError(f"interval is in MINUTES and must be a whole number, got '{raw}'")
    return n


# --- the three safeties, as pure decisions ------------------------------------------
def tick_verdict(unread: int, alive: bool, started_sid: str, now_sid: str) -> str:
    """"send" | "skip" | "exit-down" | "exit-hijacked" — the whole tick policy, testable
    without a tmux server or a clock."""
    if not alive:
        return "exit-down"
    if now_sid != started_sid:
        return "exit-hijacked"
    if unread > 0:
        return "skip"
    return "send"


def done(sent: int, times: int) -> bool:
    return times != 0 and sent >= times


# --- start ---------------------------------------------------------------------------
def start(agent: str, minutes: str, msg: str, times: int = 0, kind: str = "fyi",
          p: Paths | None = None) -> int:
    p = p or paths()
    if not agent or not minutes or not msg:
        print("[poll] usage: polling start <agent> <minutes> <message> [--times N] [--kind K]",
              file=sys.stderr)
        return 1
    try:
        mins = parse_minutes(minutes)
    except ValueError as e:
        print(f"[poll] {e}", file=sys.stderr)
        return 1

    floor = min_minutes()
    # A tick faster than a turn is not a poll, it is a denial of service against your own
    # agent: every message lands on an agent still answering the last one.
    if mins < floor:
        print(f"[poll] {mins} min is below the floor of {floor} min — an agent cannot finish a",
              file=sys.stderr)
        print("[poll]   turn that fast, so the ticks would pile up behind it.", file=sys.stderr)
        print(f"[poll]   Deliberate? AI_POLL_MIN={mins} polling start …", file=sys.stderr)
        return 1

    if not tmux.has_session(p.session(agent)):
        print(f"[poll] '{agent}' has no tmux session — nothing to poll.", file=sys.stderr)
        return 1
    sid = _sid(agent, p)
    if not sid:
        print(f"[poll] no recorded session for '{agent}' — refusing to poll an agent I cannot "
              f"identify.", file=sys.stderr)
        return 1
    if running(agent, p):
        print(f"[poll] '{agent}' already has a timer (every {_get(agent, 'minutes', p)} min). "
              f"Stop it first: polling stop {agent}", file=sys.stderr)
        return 1

    d = _dir(agent, p)
    d.mkdir(parents=True, exist_ok=True)
    _put(agent, "msg", msg, p)            # raw bytes: a message is data, never eval'd
    _put(agent, "minutes", str(mins), p)  # MINUTES on disk too — no unit changes hands
    _put(agent, "kind", kind, p)
    # The tick is FROM whoever set the timer — not from some fictional "poll" agent. A
    # poll-shaped sender has no pane and no reader, so the agent's answer ("pong 1") was filed
    # into a mailbox nobody would ever open. Observed on the first live test. Sending it as
    # the caller means the reply lands where the caller reads.
    _put(agent, "from",
         os.environ.get("AF_POLL_FROM") or os.environ.get("AF_AGENT") or "orchestrator", p)
    _put(agent, "times", str(times), p)
    _put(agent, "sid", sid, p)
    _put(agent, "sent", "0", p)
    _put(agent, "started", str(int(time.time())), p)
    d.joinpath("pid").unlink(missing_ok=True)

    env = dict(os.environ)
    env.update({"AF_ROOT": str(p.root), "AF_SLUG": p.slug, "AF_CWD": str(p.cwd)})
    env["PYTHONPATH"] = os.pathsep.join(
        x for x in (str(FACTORY_DIR), env.get("PYTHONPATH", "")) if x)
    # Detached, and deliberately not disowned into the void: the pid is the handle.
    proc = subprocess.Popen(
        [sys.executable, "-m", "af.polling", "_loop", agent],
        cwd=str(p.cwd), env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _put(agent, "pid", str(proc.pid), p)
    every = f"every {mins} min" + (f", {times} time(s)" if times else "")
    print(f"[poll] '{agent}' → {every}: {msg}")
    print(f"[poll] stop it:  polling stop {agent}      (the agent itself: bash $AF_POLL stop)")
    return 0


# --- the loop -------------------------------------------------------------------------
def loop(agent: str, p: Paths | None = None) -> int:
    """Everything it does is guarded, because it runs unattended for hours."""
    p = p or paths()
    d = _dir(agent, p)
    mins = int(_get(agent, "minutes", p, "0") or 0)
    msg = _get(agent, "msg", p)
    kind = _get(agent, "kind", p, "fyi") or "fyi"
    times = int(_get(agent, "times", p, "0") or 0)
    sid = _get(agent, "sid", p).strip()
    frm = _get(agent, "from", p).strip() or "orchestrator"
    secs = mins * 60                       # the ONE place minutes become seconds
    if secs <= 0:
        _log(agent, "no interval on disk — timer exits", p)
        return 1

    stop_flag = {"now": False}

    def _term(_s, _f):
        stop_flag["now"] = True

    for s in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(s, _term)

    while not stop_flag["now"]:
        time.sleep(secs)

        # Stopped from the outside (or by the agent itself).
        if not d.joinpath("pid").is_file():
            _log(agent, "stopped", p)
            return 0

        verdict = tick_verdict(
            unread=mailbox.unread(agent, p),
            alive=tmux.has_session(p.session(agent)),
            started_sid=sid,
            now_sid=_sid(agent, p),
        )
        if verdict == "exit-down":
            # The agent is gone. Do not linger: the name will be reused.
            _log(agent, f"'{agent}' is down — timer exits", p)
            cleanup(agent, p)
            return 0
        if verdict == "exit-hijacked":
            _log(agent, f"session changed ({sid} → {_sid(agent, p)}) — a different agent holds "
                        f"this name; timer exits", p)
            cleanup(agent, p)
            return 0
        if verdict == "skip":
            # Sending another would queue an alarm behind an alarm; the agent is busy or
            # ignoring us, and either way more mail does not help.
            _log(agent, "skipped — previous tick still unread", p)
            continue

        sent = int(_get(agent, "sent", p, "0") or 0)
        # An agent being ticked has no way to know it is on a timer, let alone that it may
        # switch it off — the mail just looks like someone nagging it every few minutes. Say
        # so ONCE, on the first tick. Repeating it every tick would buy nothing and be paid
        # for out of the agent's context, forever.
        body = msg + (FIRST_TICK_HINT.format(mins=mins) if sent == 0 else "")
        try:
            mailbox.send(agent, body, kind=kind, frm=frm, p=p)
            drive.ring(agent, p)
        except (OSError, ValueError) as e:
            _log(agent, f"send failed ({e}) — retrying next tick", p)
            continue
        sent += 1
        _put(agent, "sent", str(sent), p)
        _log(agent, f"tick {sent} sent", p)

        if done(sent, times):
            _log(agent, f"{times} tick(s) done — timer exits", p)
            cleanup(agent, p)
            return 0
    _log(agent, "stopped (signal)", p)
    return 0


def cleanup(agent: str, p: Paths) -> None:
    _dir(agent, p).joinpath("pid").unlink(missing_ok=True)


# --- stop / list / status ---------------------------------------------------------------
def stop(agent: str = "", p: Paths | None = None) -> int:
    p = p or paths()
    # No argument = the caller is an agent switching its OWN timer off. That is the whole
    # point of handing $AF_POLL to the agent: it is the one that knows the wait is over.
    agent = agent or os.environ.get("AF_AGENT") or ""
    if not agent:
        print("[poll] usage: polling stop <agent>   (inside an agent: polling stop)",
              file=sys.stderr)
        return 1
    pid = _pid(agent, p)
    if not pid:
        print(f"[poll] '{agent}' has no timer running.")
        return 0
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    cleanup(agent, p)
    print(f"[poll] '{agent}' timer stopped (was every {_get(agent, 'minutes', p)} min, "
          f"{_get(agent, 'sent', p)} tick(s) sent).")
    return 0


def list_timers(p: Paths | None = None) -> int:
    p = p or paths()
    if not p.polldir.is_dir():
        print(f"[poll] no timers on line '{p.slug}'.")
        return 0
    rows = [d for d in sorted(p.polldir.iterdir()) if d.is_dir()]
    if not rows:
        print(f"[poll] no timers on line '{p.slug}'.")
        return 0
    print(f"{'AGENT':<10} {'EVERY':<9} {'SENT/MAX':<7} {'STATE':<6} MESSAGE")
    for d in rows:
        a = d.name
        mx = _get(a, "times", p, "0").strip() or "0"
        mx = "∞" if mx == "0" else mx
        print(f"{a:<10} {_get(a, 'minutes', p) + 'm':<9} "
              f"{_get(a, 'sent', p) + '/' + mx:<7} {'live' if running(a, p) else 'dead':<6} "
              f"{_get(a, 'msg', p)[:48]}")
    return 0


def status(agent: str = "", p: Paths | None = None) -> int:
    p = p or paths()
    agent = agent or os.environ.get("AF_AGENT") or ""
    if not agent:
        print("[poll] usage: polling status <agent>", file=sys.stderr)
        return 1
    d = _dir(agent, p)
    if not d.is_dir():
        print(f"[poll] '{agent}': no timer.")
        return 0
    print(f"[poll] '{agent}': {'LIVE' if running(agent, p) else 'stopped'}")
    times = _get(agent, "times", p, "0").strip() or "0"
    log_lines = _get(agent, "log", p).splitlines()
    print(f"  every    : {_get(agent, 'minutes', p)} min")
    print(f"  sent     : {_get(agent, 'sent', p)} / {'∞' if times == '0' else times}")
    print(f"  kind     : {_get(agent, 'kind', p)}")
    print(f"  message  : {_get(agent, 'msg', p)}")
    print(f"  last log : {log_lines[-1] if log_lines else ''}")
    return 0


# --- cli ---------------------------------------------------------------------------------
def _parse_start(argv: list[str]) -> tuple[str, str, str, int, str]:
    """Hand-rolled, exactly as the bash was: --times/--kind may appear anywhere, and every
    remaining word is the message. argparse would swallow a message that starts with a dash."""
    agent = argv[0] if argv else ""
    minutes = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]
    times, kind, words = 0, "fyi", []
    i = 0
    while i < len(rest):
        if rest[i] == "--times":
            v = rest[i + 1] if i + 1 < len(rest) else "0"
            times = intish(v, 0)
            i += 2
        elif rest[i] == "--kind":
            kind = (rest[i + 1] if i + 1 < len(rest) else "fyi") or "fyi"
            i += 2
        else:
            words.append(rest[i])
            i += 1
    return agent, minutes, " ".join(words), times, kind


USAGE = """polling — put an agent on a timer. Every N MINUTES it gets the same message.

  polling start <agent> <minutes> <message> [--times N] [--kind K]
  polling stop  [agent]        # no agent = $AF_AGENT, i.e. an agent switching ITSELF off
  polling list                 # every timer on this line
  polling status <agent>

It delivers BY MAIL, never by typing into a live TUI. A tick is skipped while the previous
one is still unread, the timer exits if the agent's session id changes under it, and the
floor is 1 minute (AI_POLL_MIN)."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    rest = argv[1:]
    p = paths()
    if cmd == "start":
        return start(*_parse_start(rest), p=p)
    if cmd == "stop":
        return stop(rest[0] if rest else "", p)
    if cmd == "list":
        return list_timers(p)
    if cmd == "status":
        return status(rest[0] if rest else "", p)
    if cmd == "_loop":
        if not rest:
            return 1
        return loop(rest[0], p)
    print(USAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
