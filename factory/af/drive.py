"""Everything that TYPES INTO a live agent's pane, and everything that waits for one.

The one rule this module exists to enforce:

    NEVER type into a pane that is mid-generation or sitting on a permission prompt.

A permission prompt is a SELECT, not an input box. Text typed there lands in the selector
and the Enter that follows CONFIRMS the highlighted default — `❯ 1. Yes` — silently
approving a tool call no human ever saw. `say` and `compact` refuse on it; `approve` is
the only writer allowed to answer one, because answering it is its whole job.

The generation timer is treated differently by the two writers, ON PURPOSE:

  * `compact` refuses while the timer is up: its `/compact` mid-turn would interrupt the
    turn. `say` does NOT look at the timer — it clears the input with C-u (which cannot
    cancel a running turn) and lets `!cmd`/text queue to the turn boundary; a `_busy`
    screen-read here is the very race that used to cancel turns (see ai.sh). Only the
    permission prompt stops `say`, because a keystroke there approves a tool call unseen.
  * `ring` (the doorbell) does NOT look at the timer at all — see its docstring. The
    check is not merely unnecessary there, it is HARMFUL: a just-rung agent has not
    painted its timer yet, so a screen read calls it idle, and the guard that follows
    from that reading is what cancelled turns.

The box is cleared with C-u, never Escape. Escape clears it too — and CANCELS the turn in
flight ("Interrupted · What should Claude do instead?"). C-u closes the popup and clears
the line while a running turn continues to completion (verified live).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Iterator

from . import mailbox, patterns, probe as probemod, spec as specmod, tmux
from .paths import MAIL_SH, Paths, paths
from .probe import Probe
from .nums import intish

# Timings copied from ai.sh/mail.sh. They are not arbitrary: the 0.2s after C-u is what
# lets the popup actually close before the literal text lands, and the 0.5s after Enter is
# what lets the TUI repaint before we read the box back to see whether it was submitted.
CLEAR_SETTLE = 0.2
TYPE_SETTLE = 0.2
SUBMIT_SETTLE = 0.5
POLL = 0.5             # the wait loops' tick — `to*2` iterations of it, as in bash
IDLE_SETTLE_TICKS = 4  # wait: consecutive non-busy reads before we call it DONE
BOOT_SETTLE = 3.0      # up: let the TUI finish booting before the doorbell types into it
RING_DEBOUNCE = 120.0  # ring: how long a just-fired doorbell suppresses another (see ring)

DEFAULT_SOFT = 200000
DEFAULT_HARD = 500000
DEFAULT_TIMEOUT = 300
DEFAULT_COOLDOWN = 600


def _timeout() -> int:
    raw = os.environ.get("AI_TIMEOUT", "")
    return intish(raw, DEFAULT_TIMEOUT, positive=True)


# --- reads -----------------------------------------------------------------------
def screen(agent: str, p: Paths | None = None) -> str | None:
    p = p or paths()
    return tmux.capture_pane(p.session(agent))


def result(agent: str, p: Paths | None = None) -> str | None:
    """The text of the most recent COMPLETED turn, straight from the session log.

    Not scraped off the screen: the pane is a viewport with a scrollback limit, the jsonl
    is the authoritative append-only event stream. A torn final line (the file is being
    appended to as we read it) is dropped, not fatal.
    """
    p = p or paths()
    log = probemod.session_log(agent, p)
    if not log or not log.is_file():
        return None
    last: str | None = None
    try:
        with log.open("rb") as fh:
            for raw in fh:
                if b'"assistant"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                if not isinstance(msg, dict) or msg.get("stop_reason") != "end_turn":
                    continue
                text = "\n".join(
                    c.get("text", "") for c in (msg.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                if text:
                    last = text
    except OSError:
        return None
    return last if last is not None else "(no text in last turn)"


def ctx(agent: str, p: Paths | None = None) -> int:
    return probemod.probe(agent, p).ctx or 0


def mid_task(agent: str, p: Paths | None = None) -> bool:
    """Is the agent mid-TASK (not merely mid-turn)? INFORMATIONAL ONLY — `ledger` uses
    this to show a human what an agent is doing; compaction no longer gates on it (see
    compact_decision). The flag file is what BASH believes; mailbox.task_state() folds the
    same answer out of the mail itself and cannot go stale."""
    p = p or paths()
    return mailbox.state_flag(agent, p) == "busy"


# --- writers ---------------------------------------------------------------------
def say(agent: str, msg: str, p: Paths | None = None) -> bool:
    p = p or paths()
    s = p.session(agent)
    if not msg:
        print(f"[af] usage: af say {agent} <text>")
        return False
    pane = tmux.capture_pane(s)
    if pane is None:
        print(f"[af] no agent '{agent}' — af up {agent}")
        return False
    if probemod.phase_of(pane) == "permission":
        print(f"[af] '{agent}' is paused on a permission prompt — answer it first: "
              f"af approve {agent}")
        return False

    for _try in (1, 2):
        # Clear first: whatever is sitting in the box (a half-typed line, a stale
        # autocomplete popup) would otherwise CONCATENATE with our text.
        tmux.send_keys(s, "C-u")
        time.sleep(CLEAR_SETTLE)
        tmux.send_keys(s, msg, literal=True)
        time.sleep(TYPE_SETTLE)
        tmux.send_enter(s)
        time.sleep(SUBMIT_SETTLE)
        # Submitted ⇔ the LIVE box no longer holds our exact message. An empty box or a
        # greyed autosuggestion both count; only our text still sitting there means a
        # popup ate the Enter. (Submitted prompts also appear in the scrollback — hence
        # the last-match-wins box parser, not a whole-pane search.)
        back = tmux.capture_pane(s)
        pending = patterns.input_box(back) if back is not None else None
        if pending != msg:
            print(f"[af] sent to '{agent}': {msg}")
            return True
        print("[af] input box still holds our text (popup?), retrying…")
    print(f"[af] WARN: '{agent}' may not have submitted — check: af screen {agent}")
    return False


def keys(agent: str, *raw: str, p: Paths | None = None) -> bool:
    p = p or paths()
    ok = tmux.send_keys(p.session(agent), *raw)
    print(f"[af] keys -> '{agent}': {' '.join(raw)}")
    return ok


DOORBELL = "!bash $AF_MAIL read"      # typed into the pane, verbatim, always
DOORBELL_BODY = "bash $AF_MAIL read"  # how the TUI renders it back in the box


def ring(agent: str, p: Paths | None = None) -> bool:
    """The doorbell: type ONE fixed, path-free command into the recipient's pane.

    The payload never goes through the keyboard — it is already in the mailbox file. What
    is typed is `!bash $AF_MAIL read` and nothing else:

      * `!cmd` in the TUI runs the command AND triggers a model turn, so one send-keys
        both delivers and wakes.
      * typed at a BUSY agent it queues and fires exactly at the turn boundary, so a
        message can never interrupt work in progress.
      * `$AF_MAIL` expands from the agent's own env, so the text contains no slash — and
        no slash means the file-autocomplete popup never opens to eat the Enter.

    NO GENERATION CHECK, deliberately — this is mail.sh's rule and it is the right one.
    The check it used to have was a screen read, and a just-rung agent has not painted its
    timer yet: it reads as idle, the guard says "safe to press Escape", and Escape cancels
    the turn. Since the clear is C-u (which cannot cancel a turn) there is nothing left for
    a busy-check to protect. The permission check STAYS: that pane is a select, and typing
    into it would approve a tool call.
    """
    p = p or paths()
    tgt = _target(agent, p)
    if not tmux.has_session(tgt.split(":", 1)[0]):
        return False
    pane = tmux.capture_pane(tgt)
    phase = probemod.phase_of(pane) if pane is not None else "idle"
    if phase == "permission":
        return False

    # DEDUP. Each doorbell is its OWN model turn, and the FIRST one to land reads ALL unread
    # mail — so every doorbell typed between one being queued and the recipient reading is a
    # guaranteed-empty turn. The marker means "a doorbell is in flight, not yet consumed";
    # while it stands, another buys nothing. `mailbox.read` clears it on every non-peek read
    # (even one that found the box empty), so the common case retires it fast.
    #
    # It is checked for an IDLE agent TOO — that is the fix. The old code marked only a BUSY
    # agent, reasoning an idle one needs the nudge. But a just-rung agent reads as idle (the
    # pane has not painted its timer yet — see the comment below), so ring #1 left no marker
    # and the sender, plus the postmaster's 5-second ring-catch (unread still >0, the read
    # not yet landed), rang again and again. Observed: four `!…read` in one orc's pane, three
    # answering "no new mail".
    #
    # But the marker is AGE-BOUNDED, because "clear on read" is not enough on its own: an
    # agent killed in the window between the doorbell firing and its read leaves a marker no
    # read will ever retire, and an unbounded one would then silence that mailbox FOREVER —
    # trading the spam for stranded mail, the worse failure. A real doorbell is consumed at
    # the next turn boundary (seconds when idle, up to a long turn when busy); RING_DEBOUNCE
    # is the ceiling on that. Past it, ring anyway — at most one extra doorbell per two
    # minutes on a genuinely long turn, versus mail lost for good.
    marker = p.ring_pending(agent)
    try:
        if marker.is_file() and (time.time() - marker.stat().st_mtime) < RING_DEBOUNCE:
            return True
    except OSError:
        pass

    def _queued() -> bool:
        """Record that a doorbell is now in flight, so the next send skips until it is read
        (or until RING_DEBOUNCE lapses, whichever comes first)."""
        try:
            p.ring_pending(agent).write_text("1", encoding="utf-8")
        except OSError:
            pass
        return True

    tmux.send_keys(tgt, "C-u")
    time.sleep(CLEAR_SETTLE)

    if p.cap(agent).is_file():
        for _try in (1, 2):
            if not tmux.send_keys(tgt, DOORBELL, literal=True):
                return False
            time.sleep(TYPE_SETTLE)
            if not tmux.send_enter(tgt):
                return False
            time.sleep(SUBMIT_SETTLE)
            back = tmux.capture_pane(tgt)
            if back is None or patterns.input_box(back) != DOORBELL_BODY:
                return _queued()   # the box no longer holds it ⇒ it was submitted
            tmux.send_keys(tgt, "C-u")
            time.sleep(CLEAR_SETTLE)
        return False

    # DEGRADED PATH — an agent spawned before the mail channel existed has no AF_MAIL in
    # its env, so the path-free doorbell is impossible. Typing a literal path would open
    # the autocomplete popup, so we send an ordinary prompt and let the agent decide to
    # obey it: a model-judgment step, which is exactly what the fast path removes.
    # `af revive <name>` moves such an agent onto the reliable channel for good.
    prompt = f"NEW MAIL — run: bash {MAIL_SH} read"
    for _try in (1, 2):
        tmux.send_keys(tgt, "C-u")
        time.sleep(CLEAR_SETTLE)
        if not tmux.send_keys(tgt, prompt, literal=True):
            return False
        time.sleep(TYPE_SETTLE)
        if not tmux.send_enter(tgt):
            return False
        time.sleep(SUBMIT_SETTLE)
        back = tmux.capture_pane(tgt)
        if back is None or patterns.input_box(back) != prompt:
            return _queued()
    return False


def _target(agent: str, p: Paths) -> str:
    """An explicitly registered pane wins — that is how a human-launched orchestrator makes
    itself reachable — else the deterministic session ai-<slug>-<agent>."""
    try:
        pane = p.pane(agent).read_text(encoding="utf-8").strip()
        if pane:
            return pane
    except OSError:
        pass
    return p.session(agent)


# --- waiting ---------------------------------------------------------------------
def _observations(agent: str, p: Paths) -> Iterator[Probe]:
    while True:
        yield probemod.probe(agent, p)


def wait_turn(agent: str, base: int, timeout: int | None = None, p: Paths | None = None,
              observe: Iterator[Probe] | None = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> str:
    """Block until THIS turn ends. Returns DONE | NEEDS_INPUT | TIMEOUT.

    DONE requires BOTH a NEW end_turn in the transcript (> the baseline taken before we
    typed) AND the live timer gone. Either alone lies: a spurious early end_turn (a
    thinking block followed by more tool calls) fools the log, and a momentary gap between
    tool calls fools the screen.

    TIMEOUT does not kill anything — it reports. The turn is still running and the agent
    is still the authority on when it is finished.

    The bound is a DEADLINE, not a tick count. bash could count ticks because each tick cost
    it a `grep -c`; here a tick also reads the transcript, so on a mature agent an
    iteration-counted loop of `AI_TIMEOUT*2` ticks would quietly run for many times
    AI_TIMEOUT seconds — a timeout that does not time out when the system is slow is not a
    timeout.
    """
    p = p or paths()
    to = timeout if timeout is not None else _timeout()
    obs = observe if observe is not None else _observations(agent, p)
    deadline = clock() + to
    while clock() < deadline:
        try:
            pr = next(obs)
        except StopIteration:
            break
        if pr.phase == "permission":
            return "NEEDS_INPUT"
        if (pr.endturns or 0) > base and pr.phase != "generating":
            return "DONE"
        sleep(POLL)
    return "TIMEOUT"


def wait_idle(agent: str, timeout: int | None = None, p: Paths | None = None,
              observe: Iterator[Probe] | None = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> str:
    """Block until the agent is idle/paused NOW — the ad-hoc wait, with no baseline to
    compare against. Idle has to be SUSTAINED (IDLE_SETTLE_TICKS consecutive reads): the
    timer blinks out between tool calls, and a single idle read there is a lie."""
    p = p or paths()
    to = timeout if timeout is not None else _timeout()
    obs = observe if observe is not None else _observations(agent, p)
    settle = 0
    deadline = clock() + to
    while clock() < deadline:
        try:
            pr = next(obs)
        except StopIteration:
            break
        if pr.phase == "permission":
            return "NEEDS_INPUT"
        if pr.phase == "generating":
            settle = 0
        else:
            settle += 1
            if settle >= IDLE_SETTLE_TICKS:
                return "DONE"
        sleep(POLL)
    return "TIMEOUT"


def ask(agent: str, msg: str, p: Paths | None = None) -> int:
    """say, then wait for THIS turn to finish, then print its result from the log."""
    p = p or paths()
    base = probemod.probe(agent, p).endturns or 0
    if not say(agent, msg, p):
        return 1
    verdict = wait_turn(agent, base, p=p)
    if verdict == "DONE":
        print(result(agent, p) or "(no text in last turn)")
        maybe_autocompact(agent, p=p)
        inbox_hint(p)
        return 0
    if verdict == "NEEDS_INPUT":
        print(f"[af] ⚠ '{agent}' paused on a permission prompt — approve: "
              f"af approve {agent} [1|2|3]")
        pane = tmux.capture_pane(p.session(agent)) or ""
        for line in [l for l in pane.splitlines() if l.strip()][-8:]:
            print(line)
        return 1
    print(f"[af] ⚠ '{agent}' still working past {_timeout()}s — check: "
          f"af result {agent} / af screen {agent}")
    return 1


def approve(agent: str, choice: str = "2", p: Paths | None = None) -> int:
    """Answer a tool-permission prompt (default 2 = allow & don't ask again), then wait for
    the resumed turn and print its result. The ONLY writer permitted to type into a
    permission prompt — because that is what it is for."""
    p = p or paths()
    s = p.session(agent)
    if not tmux.has_session(s):
        print(f"[af] no agent '{agent}'")
        return 1
    base = probemod.probe(agent, p).endturns or 0
    tmux.send_keys(s, choice, literal=True)
    tmux.send_enter(s)
    # Let the prompt dismiss before we look again, or we read the stale prompt as
    # NEEDS_INPUT and report a second permission gate that isn't there.
    time.sleep(1.0)
    verdict = wait_turn(agent, base, p=p)
    if verdict == "DONE":
        print(result(agent, p) or "(no text in last turn)")
        return 0
    if verdict == "NEEDS_INPUT":
        print(f"[af] ⚠ another permission prompt for '{agent}' — af approve {agent}")
        return 1
    print(f"[af] ⚠ '{agent}' still working — af result {agent}")
    return 1


def compact(agent: str, nowait: bool = False, p: Paths | None = None) -> bool:
    """Run /compact. SAFE ONLY between turns.

    Refuses mid-generation (would interrupt), on a permission prompt (the keystrokes would
    ANSWER the prompt, not compact), and under the account-wide usage limit — /compact is a
    model call, and the model is exactly what a limited agent has run out of. Sending it
    there achieves nothing, and it achieves nothing FOREVER: the context never drops, so
    every following sweep sees the same fat agent and sends /compact again, every tick,
    until the quota returns. The warden deals with the limit; compaction stays out of its
    way.
    """
    p = p or paths()
    pr = probemod.probe(agent, p)
    if not pr.alive:
        print(f"[af] no agent '{agent}'")
        return False
    if pr.phase == "generating":
        print(f"[af] '{agent}' is mid-turn — refusing to compact (would interrupt). "
              f"retry when idle.")
        return False
    if pr.phase == "permission":
        print(f"[af] '{agent}' is on a permission prompt — answer it first "
              f"(af approve {agent}).")
        return False
    if pr.phase == "limited":
        print(f"[af] '{agent}' is out of quota (usage limit) — /compact is a model call and "
              f"would bounce. The warden wakes it on reset.")
        return False

    before = pr.ctx or 0
    print(f"[af] compacting '{agent}' (ctx ≈ {before} tok)…")
    if not say(agent, "/compact", p):
        return False
    # Stamp it. `ctx` reads the session log, which keeps the OLD size until the compaction
    # turn lands — so without this the next sweep sees a still-fat agent and compacts it
    # again. The stamp, not the size, is what says "this one has been dealt with".
    p.state.mkdir(parents=True, exist_ok=True)
    p.compacted(agent).write_text(str(int(time.time())), encoding="utf-8")

    if nowait:
        # The keystrokes are sent; the agent compacts on its own time. Waiting is only to
        # report the new size, and doing that inside an autosweep would hang `af post` for
        # up to AI_TIMEOUT per over-threshold agent.
        print(f"[af] '{agent}' compacting in the background (was ≈ {before} tok).")
        return True

    wait_idle(agent, p=p)
    after = ctx(agent, p)
    if after < before:
        print(f"[af] '{agent}' compacted: ≈ {before} → {after} tok.")
    else:
        # /compact writes no assistant record, so the shrunk size only becomes visible
        # after the agent's next turn. Don't print a "now ≈ …" that is really the
        # pre-compaction number in disguise.
        print(f"[af] '{agent}' compacted (was ≈ {before} tok; the new size reads out after "
              f"its next turn).")
    return True


# --- the thresholds --------------------------------------------------------------
def _num(v: object, default: int) -> int:
    """Junk must not DISABLE the guard — it falls back to the default. Only an explicit 0
    turns a threshold off, which is why "" and "abc" cannot be allowed to mean 0."""
    s = "" if v is None else str(v)
    return intish(s, default)


def resolve_thresholds(soft: object = None, hard: object = None,
                       env: dict[str, str] | None = None) -> tuple[int, int]:
    """(soft, hard), in absolute tokens, resolved the way ai.sh resolves them:

        explicit argument (sweep passes the AGENT'S OWN, out of its spec)
        else this session's env (AI_COMPACT_SOFT / AI_COMPACT_HARD)
        else the defaults (200k / 500k)

    A station on a 200k-window model is configured `compact_soft: 80000`; judging it by the
    sweeper's 200000 means it is never compacted until it dies. Hence the agent's own.
    """
    env = os.environ if env is None else env
    s = soft if (soft is not None and str(soft) != "") else env.get("AI_COMPACT_SOFT", "")
    h = hard if (hard is not None and str(hard) != "") else env.get("AI_COMPACT_HARD", "")
    return _num(s, DEFAULT_SOFT), _num(h, DEFAULT_HARD)


def spec_thresholds(agent: str, p: Paths | None = None) -> tuple[int | None, int | None]:
    try:
        return specmod.read(agent, p).thresholds()
    except specmod.SpecError:
        return None, None


def compact_decision(ctx_tokens: int, soft: int, hard: int) -> str:
    """"hard" | "soft" | "none" — the whole compaction policy, as one pure function.

    Compacts on MEASURED CONTEXT alone — no task-boundary prediction. This used to also
    gate SOFT on a mail-derived busy/idle flag (HOLD mid-task, compact otherwise), which
    was a GUESS: the flag went stale the instant its agent crashed or a done/result was
    misrouted, which is why a whole reaper (sweep._reap) had to exist just to undo that
    staleness. A measured token count is read fresh off the transcript every time —
    nothing to go stale, nothing left to gate or reap.

      HARD — compact at the next TURN boundary regardless. Losing some working state is
             bad; running out of context loses everything.
      SOFT — compact as soon as the threshold is crossed, mid-task or not.
      0 on either threshold disables it.
    """
    if ctx_tokens <= 0:
        return "none"
    if hard != 0 and ctx_tokens > hard:
        return "hard"
    if soft != 0 and ctx_tokens > soft:
        return "soft"
    return "none"


def maybe_autocompact(agent: str, soft: object = None, hard: object = None,
                      nowait: bool = False, p: Paths | None = None) -> str:
    p = p or paths()
    s, h = resolve_thresholds(soft, hard)
    c = ctx(agent, p)
    d = compact_decision(c, s, h)
    if d == "hard":
        print(f"[af] context ≈ {c} tok > hard {h} — compacting '{agent}' now (mid-task or "
              f"not; running out would lose everything)…")
        compact(agent, nowait=nowait, p=p)
    elif d == "soft":
        print(f"[af] context ≈ {c} tok > soft {s} — compacting '{agent}'…")
        compact(agent, nowait=nowait, p=p)
    return d


# --- the nudge -------------------------------------------------------------------
def inbox_hint(p: Paths | None = None) -> None:
    """One line appended to ask/list output when mail is waiting, so the orchestrator
    notices an agent that blocked while it was doing something else."""
    p = p or paths()
    who = os.environ.get("AF_AGENT") or "orchestrator"
    n = mailbox.unread(who, p)
    if n > 0:
        print(f"[af] ⚠ {n} unread message(s) from spawned agents — af mail",
              file=sys.stdout)


# --- standalone target helpers (no agent name, just a tmux target) ----------------

def ring_target(target: str) -> bool:
    """Ring the doorbell on a standalone tmux target (no agent name / Paths needed)."""
    if not tmux.has_session(target.split(':', 1)[0]):
        return False
    pane = tmux.capture_pane(target)
    phase = probemod.phase_of(pane) if pane is not None else 'idle'
    if phase == 'permission':
        return False
    tmux.send_keys(target, 'C-u')
    time.sleep(CLEAR_SETTLE)
    for _try in (1, 2):
        if not tmux.send_keys(target, DOORBELL, literal=True):
            return False
        time.sleep(TYPE_SETTLE)
        if not tmux.send_enter(target):
            return False
        time.sleep(SUBMIT_SETTLE)
        back = tmux.capture_pane(target)
        if back is None or patterns.input_box(back) != DOORBELL_BODY:
            return True
    return False


def say_target(target: str, msg: str) -> bool:
    """Say a message into a standalone tmux target (no agent name / Paths needed)."""
    if not msg:
        return False
    pane = tmux.capture_pane(target)
    if pane is None:
        return False
    if probemod.phase_of(pane) == 'permission':
        return False
    for _try in (1, 2):
        tmux.send_keys(target, 'C-u')
        time.sleep(CLEAR_SETTLE)
        tmux.send_keys(target, msg, literal=True)
        time.sleep(TYPE_SETTLE)
        tmux.send_enter(target)
        time.sleep(SUBMIT_SETTLE)
        back = tmux.capture_pane(target)
        pending = patterns.input_box(back) if back is not None else None
        if pending != msg:
            return True
    return False


def compact_target(target: str, sid: str | None, nowait: bool = False) -> bool:
    """Same as `compact()`, but against a standalone tmux target instead of an agent name —
    `compact()` resolves the pane via `p.session(agent)`, which does not exist for a target
    with no squad/Paths behind it at all."""
    pr = probemod.probe_target(target, sid)
    if not pr.alive:
        print(f"[af] no session '{target}'")
        return False
    if pr.phase == "generating":
        print(f"[af] '{target}' is mid-turn — refusing to compact (would interrupt).")
        return False
    if pr.phase == "permission":
        print(f"[af] '{target}' is on a permission prompt — answer it first.")
        return False
    before = pr.ctx or 0
    print(f"[af] compacting '{target}' (ctx ≈ {before} tok)…")
    if not say_target(target, "/compact"):
        return False
    if nowait:
        print(f"[af] '{target}' compacting in the background (was ≈ {before} tok).")
    return True


def maybe_autocompact_target(target: str, sid: str | None, soft: object = None,
                             hard: object = None, nowait: bool = False) -> str:
    """Same policy as `maybe_autocompact()`, scoped to a standalone target+sid instead of an
    agent name — see `compact_target` for why the agent-shaped primitives don't fit here."""
    s, h = resolve_thresholds(soft, hard)
    c = probemod.probe_target(target, sid).ctx or 0
    d = compact_decision(c, s, h)
    if d in ("hard", "soft"):
        compact_target(target, sid, nowait=nowait)
    return d
