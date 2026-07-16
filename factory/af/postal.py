"""`post` and `mail` — the orchestrator's end of the reliable channel.

The orchestrator drives the same transport its agents use: `post` appends to an agent's
mailbox and rings its doorbell; `mail` reads THIS session's mailbox (as 'orchestrator') and
acks it. Both sweep first — see sweep.autosweep for why that is not optional.
"""

from __future__ import annotations

import os
import sys

from . import drive, mailbox, sweep as sweepmod
from .paths import Paths, paths

KINDS = ("task", "question", "blocked", "result", "done", "fyi")


def post(to: str, body: str, kind: str = "", p: Paths | None = None) -> int:
    """Send mail to an agent and ring its doorbell.

    The default kind is `task`, because that is what posting to an agent MEANS. It also
    marks the agent busy in the ledger display — the busy/idle signal is purely
    informational (used by `af ledger`); it no longer affects compaction at all.
    """
    p = p or paths()
    if not to:
        print("[af] usage: af post <agent> [--kind K] <text>", file=sys.stderr)
        return 1
    if not body:
        print("[af] refusing to send an empty message", file=sys.stderr)
        return 1

    # SWEEP FIRST, SEND SECOND — and skip the recipient.
    #
    # Sweeping AFTER the send let the reaper delete the state-/tasker- files that `send` had
    # just written for a task QUEUED to a down agent, reading them as garbage from a crashed
    # one. Sweeping first only ever sees state that predates this command.
    #
    # Skipping the recipient: a /compact typed at the same moment we hand it a task is a race
    # with nothing to win — both are keystrokes into one input box, and the agent is about to
    # be busy with the very task we just gave it.
    sweepmod.autosweep(to, p)

    msg = mailbox.send(to, body, kind=kind or "task", p=p)
    seq = mailbox.total(to, p)
    if drive.ring(to, p):
        print(f"[af] {msg.frm} → {to} [{msg.kind}] #{seq} delivered (doorbell rung), id={msg.id}")
    else:
        # QUEUED, not lost. The cursor is the ack, so nothing has to be re-sent: the message
        # is read on the recipient's next `mail read`, and `up`/`revive` rings a returning
        # agent that has unread mail.
        print(f"[af] {msg.frm} → {to} [{msg.kind}] #{seq} QUEUED — '{to}' has no live/idle "
              f"pane. It will be rung on next 'af up/revive {to}', or read on its next "
              f"'mail read'. id={msg.id}")
    return 0


def read_mail(agent: str | None = None, peek: bool = False, p: Paths | None = None) -> int:
    p = p or paths()
    who = agent or os.environ.get("AF_AGENT") or "orchestrator"
    rc = 0
    try:
        msgs = mailbox.read(who, peek=peek, p=p)
    except mailbox.MailboxLocked as e:
        # A locked box is a genuine failure and the verdict is this command's. The sweep that
        # follows must not overwrite it: its last act is a conditional unlink, whose status is
        # not the answer to "did I read my mail".
        print(f"[mail] {e}")
        msgs = []
        rc = 1
    else:
        if not msgs:
            print(f"[mail] no new mail for '{who}'")
        else:
            print(f"═══ MAIL for '{who}' — {len(msgs)} new ═══")
            for m in msgs:
                print(f"── from: {m.frm}   kind: {m.kind}   id: {m.id}")
                print(m.body)
            print("═══ end of mail ═══")
            print('Reply with: bash $AF_MAIL send --to <agent> '
                  '--kind <question|blocked|result|done|fyi> "..."')
    sweepmod.autosweep("", p)
    return rc
