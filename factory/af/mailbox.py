"""The mailbox: the reliable channel between agents.

On-disk format is fixed by mail.sh, which is still running against the same files:

    <agent>.jsonl    one message per physical line: {id,ts,from,to,kind,body|body_file}
    <agent>.cursor   how many messages the agent has consumed. THE CURSOR IS THE ACK.
    blob/<id>.txt    the body of a message too big to append atomically
    state-<agent>    literal "busy" | "idle" — is it mid-TASK (not merely mid-turn)
    tasker-<agent>   who handed it that task
    cap-<agent>      exists => the agent understands the $AF_MAIL doorbell

One physical line per message, so appends stay atomic: a write smaller than PIPE_BUF to
an O_APPEND fd cannot interleave with another writer's. That budget is in BYTES — a
Cyrillic body JSON-encodes to \\uXXXX at 6 bytes a character — so the threshold is
measured on the ENCODED line and anything longer spills to a blob.
"""

from __future__ import annotations

import json
import os
import re
import sys
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import Paths, paths

BLOB_AT = 2000  # bytes of encoded line we consider safely atomic
LOCK_SPINS = 50
LOCK_SLEEP = 0.1


class MailboxLocked(Exception):
    pass


@dataclass(frozen=True)
class Message:
    id: str
    ts: int
    frm: str
    to: str
    kind: str
    body: str

    @classmethod
    def parse(cls, line: str) -> "Message | None":
        try:
            m = json.loads(line)
        except Exception:
            return None
        if not isinstance(m, dict):
            return None
        body = m.get("body")
        if body is None:
            # A blob reference is expanded here, so the agent sees the full text inline and
            # never has to go open a file.
            ref = m.get("body_file") or ""
            try:
                body = Path(ref).read_text(encoding="utf-8", errors="replace") if ref else ""
            except OSError:
                body = f"(body_file missing: {ref})"
        return cls(
            id=str(m.get("id", "")),
            ts=int(m.get("ts") or 0),
            frm=str(m.get("from", "")),
            to=str(m.get("to", "")),
            kind=str(m.get("kind", "")),
            body=str(body),
        )


# --- the lock ---------------------------------------------------------------------
# A mkdir-directory mutex, NOT fcntl.flock. This is not conservatism: bash and Python must
# exclude EACH OTHER while both are live, and an flock does not exclude a mkdir-lock — the
# two would sail straight through one another and the cursor (a read-modify-write) would be
# rewound, re-delivering mail that was already acked. flock replaces this the day mail.sh
# is gone, and not before. (fcntl.flock is fine for any NEW lock only Python ever holds.)
def _lock(agent: str, p: Paths) -> bool:
    d = p.mail_lock(agent)
    d.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(LOCK_SPINS):
        try:
            d.mkdir()
            return True
        except FileExistsError:
            time.sleep(LOCK_SLEEP)
        except OSError:
            return False
    # A stale lock must not wedge the mailbox forever — take it after 5s.
    shutil.rmtree(d, ignore_errors=True)
    try:
        d.mkdir()
        return True
    except OSError:
        return False


def _unlock(agent: str, p: Paths) -> None:
    shutil.rmtree(p.mail_lock(agent), ignore_errors=True)


# --- counting ---------------------------------------------------------------------
def _split(f: Path) -> list[str]:
    """A mailbox line is a `\\n`-terminated record and NOTHING else.

    str.splitlines() also breaks on U+2028, U+0085, \\x0b and friends — and neither
    json.dumps(ensure_ascii=False) nor jq escapes those, so an agent that mails a body
    containing one would have its record split in two here while `_lines` (and bash's
    `grep -c ''`) still counted it as one. The cursor then advanced past a record that
    was never delivered: the message was acked and destroyed, silently."""
    try:
        data = f.read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    if not data:
        return []
    lines = data.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _lines(f: Path) -> int:
    return len(_split(f))


_DIGITS = re.compile(r"[0-9]+")


def _read_cursor(agent: str, p: Paths) -> int:
    """A cursor AHEAD of the mailbox is evidence the box was truncated or rotated under it
    (AF_ROOT lives in /tmp, which macOS purges) while the cursor survived. Reset to 0 rather
    than clamp: clamping leaves the stale value on disk, so the next N messages to arrive
    land 'behind' the cursor and are eaten one by one, forever, with no error.

    A non-numeric cursor fails closed to 0 for the same reason — note str.isdigit() is not
    [0-9] ('²'.isdigit() is True and int('²') raises), so match explicitly."""
    try:
        raw = p.cursor(agent).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        raw = ""
    c = int(raw) if _DIGITS.fullmatch(raw) else 0
    if c > _lines(p.box(agent)):
        return 0
    return c


def unread(agent: str, p: Paths | None = None) -> int:
    p = p or paths()
    return _lines(p.box(agent)) - _read_cursor(agent, p)


def stat(p: Paths | None = None) -> dict[str, int]:
    p = p or paths()
    return {a: unread(a, p) for a in p.boxes()}


# --- send -------------------------------------------------------------------------
def send(to: str, body: str, kind: str = "fyi", frm: str | None = None,
         p: Paths | None = None) -> Message:
    """Append a message to the recipient's mailbox and update the task bookkeeping.

    This is the file half of delivery only. The DOORBELL — the one fixed, path-free command
    typed into the recipient's pane — is a pane write, and lands with the rest of the tmux
    writers; until then, a message sent from Python is read on the recipient's next
    `mail read` (which is exactly what mail.sh calls a QUEUED message).
    """
    p = p or paths()
    if not to:
        raise ValueError("send needs a recipient")
    if not body:
        raise ValueError("refusing to send an empty message")
    frm = frm or os.environ.get("AF_AGENT") or "orchestrator"
    kind = kind or "fyi"

    p.blobdir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    msg_id = f"m-{ts}-{os.getpid()}-{random.randint(0, 32767)}"

    def encode(key: str, val: str) -> str:
        return json.dumps(
            {"id": msg_id, "ts": ts, "from": frm, "to": to, "kind": kind, key: val},
            ensure_ascii=False,
            separators=(",", ":"),  # jq -c parity: same body, same blob-spill threshold
        )

    line = encode("body", body)
    if len(line.encode("utf-8")) > BLOB_AT:
        blob = p.blob(msg_id)
        blob.write_text(body, encoding="utf-8")
        line = encode("body_file", str(blob))

    with p.box(to).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    _mark_task(to, frm, kind, p)
    return Message(id=msg_id, ts=ts, frm=frm, to=to, kind=kind, body=body)


def _mark_task(to: str, frm: str, kind: str, p: Paths) -> None:
    """The busy/idle flag files. A task goes out, a done/result comes back — that is what
    tells a turn boundary from a TASK boundary, and compacting at the wrong one throws away
    the working state the agent still needs.

    done/result clears the SENDER's state only when it is answering the party that tasked
    it; clearing on any done/result would let a side-reply to a peer mark an agent idle
    while its real task is still open.

    These files die once nothing in bash reads them — task_state() below already computes
    the same answer as a fold over the jsonl, which cannot be orphaned by a crash the way a
    flag can. Until then both are written, and the flag is what bash believes.
    """
    if kind == "task":
        p.task_flag(to).write_text("busy")
        p.tasker(to).write_text(frm)
    elif kind in ("done", "result"):
        try:
            tasker = p.tasker(frm).read_text(encoding="utf-8").strip()
        except OSError:
            tasker = ""
        if tasker == to:
            p.task_flag(frm).write_text("idle")


# --- read (the ack) ---------------------------------------------------------------
def read(agent: str | None = None, peek: bool = False,
         p: Paths | None = None) -> list[Message]:
    """Everything since the cursor; then the cursor advances, which IS the acknowledgement.

    The cursor moves BEFORE the caller sees the messages. If we handed them over first and
    died (tool timeout, killed pane) the cursor would never move and an escalation would be
    re-delivered on a loop forever — the worse failure, and the one the exactly-once claim
    exists to prevent. A message that was truly missed is still there in `dump`.
    """
    p = p or paths()
    agent = agent or os.environ.get("AF_AGENT") or "orchestrator"
    p.mailroot.mkdir(parents=True, exist_ok=True)

    if not _lock(agent, p):
        raise MailboxLocked(f"mailbox of '{agent}' is locked by another reader — try again")
    try:
        box = p.box(agent)
        cur = _read_cursor(agent, p)
        tot = _lines(box)
        if tot <= cur:
            return []
        if not peek:
            p.cursor(agent).write_text(str(tot), encoding="utf-8")
    finally:
        _unlock(agent, p)

    out, dropped = [], 0
    for line in _split(box)[cur:tot]:
        m = Message.parse(line)
        if m:
            out.append(m)
        else:
            dropped += 1
    if dropped:
        # The cursor already advanced under the lock — these are acked and unrecoverable
        # from `read`. Say so: a silent drop here looks exactly like "no new mail".
        print(f"[mail] ⚠ {dropped} message(s) could not be decoded (already acked) — "
              f"recover with: af mail --dump", file=sys.stderr)
    return out


def dump(agent: str, p: Paths | None = None) -> list[Message]:
    p = p or paths()
    try:
        lines = _split(p.box(agent))
    except OSError:
        return []
    return [m for m in (Message.parse(line) for line in lines) if m]


# --- task state -------------------------------------------------------------------
def state_flag(agent: str, p: Paths | None = None) -> str:
    """What bash believes: the literal contents of state-<agent> ("busy"/"idle"/"")."""
    p = p or paths()
    try:
        return p.task_flag(agent).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _envelopes(box: Path):
    """The raw envelopes of a mailbox, in line order — no blob expansion, no ack."""
    try:
        lines = _split(box)
    except OSError:
        return
    for line in lines:
        try:
            m = json.loads(line)
        except Exception:
            continue
        if isinstance(m, dict):
            yield m


def task_state(agent: str, p: Paths | None = None) -> str:
    """Is the agent mid-TASK, folded out of the mail itself: the last `task` addressed TO it,
    versus the `done`/`result` it sent BACK to whoever tasked it.

    The flag file is a cache of this, and a cache an interrupted `send` or a purged /tmp can
    strand: a stale `busy` silently exempts a name from soft compaction forever, which is why
    sweep grew a reaper for it. The fold cannot go stale — it IS the messages.

    An agent's replies live in the TASKER's box, not its own, so this reads two boxes rather
    than folding one. There is no global order across boxes to fold over: `ts` has one-second
    resolution, and every tiebreak that invents one is wrong somewhere (ordering by filename
    put a reply ahead of the task it answered; ordering task-before-reply put a NEW task
    behind an OLD reply). So the question is asked narrowly instead — did a qualifying reply
    land at or after the last task? — where the only remaining blur is a `done` that closes an
    older task in the very second a new one arrives. Real tasks are minutes apart.
    """
    p = p or paths()
    last_task = None
    for m in _envelopes(p.box(agent)):
        if m.get("kind") == "task" and str(m.get("to", "")) == agent:
            last_task = m
    if last_task is None:
        return "idle"

    tasker = str(last_task.get("from", ""))
    if not tasker:
        return "busy"
    since = int(last_task.get("ts") or 0)
    for m in _envelopes(p.box(tasker)):
        if (m.get("kind") in ("done", "result")
                and str(m.get("from", "")) == agent
                and int(m.get("ts") or 0) >= since):
            return "idle"
    return "busy"
