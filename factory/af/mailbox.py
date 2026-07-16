"""The mailbox: the reliable channel between agents.

On-disk format (every `.sh` is a 6-line Python shim now; nothing but this module and
`af.mailcli` ever opens these files):

    <agent>.jsonl    one message per physical line: {id,ts,from,to,kind,body|body_file,dedup?}
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

import contextlib
import fcntl
import json
import os
import re
import sys
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import Paths, paths

BLOB_AT = 2000  # bytes of encoded line we consider safely atomic
LOCK_WAIT = 5.0    # seconds to wait for the flock before giving up
LOCK_POLL = 0.05


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
    dedup: str = ""

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
            dedup=str(m.get("dedup") or ""),
        )


# --- the lock ---------------------------------------------------------------------
# fcntl.flock, not a mkdir mutex. The mkdir mutex existed only to exclude a BASH writer —
# an flock taken by Python is invisible to a bash process touching the same file, so the
# two could sail past each other and rewind the cursor. Every `.sh` is now a 6-line Python
# shim; nothing but this module opens these files, so a kernel-held flock is correct and
# strictly better: it cannot go stale. The mkdir version needed a "steal it after 5s"
# escape hatch for a holder that crashed mid-lock, and that escape hatch could ALSO steal
# the lock out from under a holder that was merely slow, not dead — the exact class of bug
# a squad.json review caught in the same pattern (see af.squad). flock is held by the OS
# per file descriptor: it releases the instant the holding process exits or closes the fd,
# crash or not, so there is no stale case to steal past — a bounded wait either succeeds or
# reports a genuinely different live holder.
@contextlib.contextmanager
def _locked(agent: str, p: Paths):
    path = p.mail_lock(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w", encoding="utf-8")
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MailboxLocked(
                        f"mailbox of '{agent}' is locked by another reader — try again")
                time.sleep(LOCK_POLL)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


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
    [0-9] ('²'.isdigit() is True and int('²') raises), so match explicitly.

    The reset is WRITTEN BACK, which is what keeps bash and Python from disagreeing about the
    same bytes: mail.sh clamps the in-memory value and leaves the bad one on disk, so for as
    long as both implementations are live, an unrepaired cursor would have bash reporting
    `unread 0` and Python reporting `unread N` for one mailbox — and sweep's reaper decides
    whether a busy flag is garbage on exactly that number. Repairing the file makes the two
    agree on the next read: bash clamps nothing, because there is nothing left to clamp."""
    try:
        raw = p.cursor(agent).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        raw = ""
    c = int(raw) if _DIGITS.fullmatch(raw) else 0
    if c > _lines(p.box(agent)):
        try:
            p.cursor(agent).write_text("0", encoding="utf-8")
        except OSError:
            pass   # read-only mailbox: still answer 0, still deliver the mail
        return 0
    return c


def total(agent: str, p: Paths | None = None) -> int:
    """How many messages the mailbox holds — the sequence number `send` reports."""
    p = p or paths()
    return _lines(p.box(agent))


def unread(agent: str, p: Paths | None = None) -> int:
    p = p or paths()
    return _lines(p.box(agent)) - _read_cursor(agent, p)


def stat(p: Paths | None = None) -> dict[str, int]:
    p = p or paths()
    return {a: unread(a, p) for a in p.boxes()}


# --- send -------------------------------------------------------------------------
def find_by_dedup(to: str, dedup_key: str, p: Paths) -> Message | None:
    """The last message to `to` carrying this dedup key, if any — the primitive `send`'s
    idempotent path checks before appending a duplicate."""
    for m in dump(to, p):
        if m.dedup == dedup_key:
            return m
    return None


def send(to: str, body: str, kind: str = "fyi", frm: str | None = None,
         p: Paths | None = None, dedup_key: str | None = None) -> Message:
    """Append a message to the recipient's mailbox and update the task bookkeeping.

    This is the file half of delivery only. The DOORBELL — the one fixed, path-free command
    typed into the recipient's pane — is a pane write, and lands with the rest of the tmux
    writers; until then, a message sent from Python is read on the recipient's next
    `mail read` (which mail.sh, before it was a shim, called a QUEUED message).

    `dedup_key`, when given, makes the send idempotent: a caller that might retry (a ring
    re-attempted next tick, a wake re-sent after an ambiguous failure) passes a stable key,
    and a second send with the same key for the same recipient returns the ALREADY-SENT
    message instead of appending a duplicate. Checked and appended under the mailbox lock so
    two concurrent deduped sends cannot both pass the check. Callers that never retry (the
    overwhelming majority — a human `af post`, a model composing a fresh message) pass
    nothing and pay no lock, no scan: the current lock-free atomic-append path is unchanged.
    """
    p = p or paths()
    if not to:
        raise ValueError("send needs a recipient")
    if not body:
        raise ValueError("refusing to send an empty message")
    frm = frm or os.environ.get("AF_AGENT") or "orchestrator"
    kind = kind or "fyi"

    def _append() -> Message:
        p.blobdir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        msg_id = f"m-{ts}-{os.getpid()}-{random.randint(0, 32767)}"

        def encode(key: str, val: str) -> str:
            d = {"id": msg_id, "ts": ts, "from": frm, "to": to, "kind": kind, key: val}
            if dedup_key:
                d["dedup"] = dedup_key
            return json.dumps(
                d, ensure_ascii=False,
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
        return Message(id=msg_id, ts=ts, frm=frm, to=to, kind=kind, body=body,
                       dedup=dedup_key or "")

    if not dedup_key:
        return _append()

    with _locked(to, p):
        existing = find_by_dedup(to, dedup_key, p)
        if existing is not None:
            return existing
        return _append()


def _mark_task(to: str, frm: str, kind: str, p: Paths) -> None:
    """The busy/idle flag files. A task goes out, a done/result comes back — this
    bookkeeping is used by `af ledger` to show whether an agent is busy or idle.
    Compaction no longer consults these flags.

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

    with _locked(agent, p):
        box = p.box(agent)
        cur = _read_cursor(agent, p)
        tot = _lines(box)
        if not peek:
            # The doorbell that triggered this read has now fired — clear the pending marker
            # so a future send may ring again. Cleared even when there is nothing new (a
            # doorbell that found the box already drained still fired), or the marker would
            # stick and silence the next send. See Paths.ring_pending / drive.ring.
            p.ring_pending(agent).unlink(missing_ok=True)
        if tot <= cur:
            return []
        if not peek:
            p.cursor(agent).write_text(str(tot), encoding="utf-8")

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

    Used by `af ledger` for display. The fold cannot go stale — it IS the messages,
    re-folded from the mail log on every read. No reaper is needed for this.

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
