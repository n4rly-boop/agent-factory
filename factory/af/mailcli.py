"""`python3 -m af.mailcli` — mail.sh's CLI, byte-for-byte on disk, on the Python core.

mail.sh is what live agents call by absolute path: the doorbell types `bash $AF_MAIL read`
into a pane, and agents run `bash $AF_MAIL send --to <agent> --kind <K> "body"`. When mail.sh
becomes a thin shim it must exec THIS module, so this module has to reproduce mail.sh's CLI
exactly — its flag grammar (`--to`/`--kind`/`--from` up front, the rest is the body), its
defaults (kind=fyi, from=$AF_AGENT else orchestrator), its side-effects (task→busy,
done/result→idle-back-to-the-tasker, and the doorbell ring), and its exit codes.

This is deliberately NOT `af post`/`af mail`: those default kind=task and run an autosweep.
mail.sh does neither. So this delegates straight to the low-level primitives — af.mailbox for
the file half, af.drive.ring for the doorbell — and adds nothing of its own.
"""

from __future__ import annotations

import os
import sys

from . import drive, mailbox
from .paths import Paths, paths


def _self() -> str:
    """mail.sh's SELF: who am I when --from is not given."""
    return os.environ.get("AF_AGENT") or "orchestrator"


# --- send -------------------------------------------------------------------------
def _parse_send(argv: list[str]) -> tuple[str, str, str, str]:
    """mail.sh's send parser: consume leading --to/--kind/--from pairs, the REST is the body.

    The loop stops at the first non-flag word, exactly as the bash `while … case … *) break`
    does — so `send --to x hi --kind y` files body="hi --kind y", a message, not a flag. A
    trailing flag with no value takes "" (bash's `${2:-}` after a failed `shift 2`)."""
    to = ""
    kind = ""
    frm = None  # None => fall back to SELF, matching mail.sh's `from="$SELF"` default
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--to":
            to = argv[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--kind":
            kind = argv[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--from":
            frm = argv[i + 1] if i + 1 < n else ""
            i += 2
        else:
            break
    body = " ".join(argv[i:])
    return to, kind, frm if frm is not None else _self(), body


def cmd_send(argv: list[str], p: Paths) -> int:
    to, kind, frm, body = _parse_send(argv)
    kind = kind or "fyi"
    if not to:
        print("[mail] usage: mail.sh send --to <agent> [--kind K] <text>")
        return 1
    if not body:
        print("[mail] refusing to send an empty message")
        return 1

    # The file half — append + task bookkeeping (task→busy, done/result→idle to the tasker)
    # all live inside mailbox.send, which is the same code path af post uses.
    msg = mailbox.send(to, body, kind=kind, frm=frm, p=p)
    seq = mailbox.total(to, p)

    # The doorbell. Ring failure is not an error — the message is already in the box and the
    # cursor is the ack, so it is QUEUED and read on the recipient's next `mail read`.
    if drive.ring(to, p):
        print(f"[mail] {msg.frm} → {to} [{msg.kind}] #{seq} delivered (doorbell rung), "
              f"id={msg.id}")
    else:
        print(f"[mail] {msg.frm} → {to} [{msg.kind}] #{seq} QUEUED — '{to}' has no live/idle "
              f"pane. It will be rung on next 'ai up/revive {to}', or read on its next "
              f"'mail read'. id={msg.id}")
    return 0


# --- read (the ack) ---------------------------------------------------------------
def cmd_read(argv: list[str], p: Paths) -> int:
    who = _self()
    peek = False
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--agent":
            who = argv[i + 1] if i + 1 < n and argv[i + 1] else _self()
            i += 2
        elif a == "--peek":
            peek = True
            i += 1
        else:
            break

    try:
        msgs = mailbox.read(who, peek=peek, p=p)
    except mailbox.MailboxLocked as e:
        print(f"[mail] {e}")
        return 1
    if not msgs:
        print(f"[mail] no new mail for '{who}'")
        return 0
    print(f"═══ MAIL for '{who}' — {len(msgs)} new ═══")
    for m in msgs:
        print(f"── from: {m.frm}   kind: {m.kind}   id: {m.id}")
        print(m.body)
    print("═══ end of mail ═══")
    print('Reply with: bash $AF_MAIL send --to <agent> '
          '--kind <question|blocked|result|done|fyi> "..."')
    return 0


# --- unread -----------------------------------------------------------------------
def cmd_unread(argv: list[str], p: Paths) -> int:
    # mail.sh: `unread [agent]` OR `unread --agent A`; default SELF.
    if argv and argv[0] == "--agent":
        who = argv[1] if len(argv) > 1 and argv[1] else _self()
    elif argv and argv[0]:
        who = argv[0]
    else:
        who = _self()
    print(mailbox.unread(who, p))
    return 0


# --- ring -------------------------------------------------------------------------
def cmd_ring(argv: list[str], p: Paths) -> int:
    who = argv[0] if argv else ""
    if not who:
        # Exit 0, as bash does: its dispatch is `ring "$@" && echo … || echo …`, and the
        # usage path's non-zero return is swallowed by the `||` branch's own echo. A caller
        # that checks $? must see the same 0 the bash gave it.
        print("[mail] usage: mail.sh ring <agent>")
        return 0
    # mail.sh's dispatch: `ring && echo rang || echo no-pane` — the compound is 0 either way,
    # so this subcommand's exit code does not depend on whether a pane was there.
    if drive.ring(who, p):
        print(f"[mail] rang '{who}'")
    else:
        print(f"[mail] '{who}' has no live pane")
    return 0


# --- dump -------------------------------------------------------------------------
def cmd_dump(argv: list[str], p: Paths) -> int:
    who = argv[0] if argv and argv[0] else _self()
    msgs = mailbox.dump(who, p)
    if not msgs:
        print(f"[mail] mailbox of '{who}' is empty")
        return 0
    for m in msgs:
        print(f"── from: {m.frm}   kind: {m.kind}   id: {m.id}")
        print(m.body)
    return 0


_HELP = """mail — reliable agent↔agent message transport for the factory.

  mail.sh send --to <agent> [--kind K] [--from F] <text>   append + ring
  mail.sh read [--agent A] [--peek]                        read unread, ack
  mail.sh unread [--agent A]                               count unread
  mail.sh ring <agent>                                     doorbell only
  mail.sh dump <agent>                                     whole mailbox"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    rest = argv[1:]
    p = paths()
    if cmd == "send":
        return cmd_send(rest, p)
    if cmd == "read":
        return cmd_read(rest, p)
    if cmd == "unread":
        return cmd_unread(rest, p)
    if cmd == "ring":
        return cmd_ring(rest, p)
    if cmd == "dump":
        return cmd_dump(rest, p)
    print(_HELP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
