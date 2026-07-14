"""python3 -m af <cmd> — the Python CLI, growing one command at a time.

Only the READ-ONLY commands are wired: they observe the same files bash writes, so they
can be trusted next to a live line. Everything that TYPES INTO A PANE or spawns a process
is still ai.sh's job, and says so rather than half-doing it — a command that looked
migrated and wasn't would be found out on a live agent, which is the one place this system
cannot afford to be wrong.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from . import mailbox, spec as specmod
from .paths import paths
from .probe import probe as do_probe

NOT_MIGRATED = [
    "up", "ask", "say", "post", "sweep", "ledger", "revive", "line", "warden", "poll",
]


def cmd_slug(args: argparse.Namespace) -> int:
    print(paths().slug)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    p = do_probe(args.agent)
    # Exit status follows aliveness, like `tmux has-session` — so a shell can gate on it.
    if args.json:
        print(json.dumps(dataclasses.asdict(p)))
        return 0 if p.alive else 1
    print(f"agent    : {args.agent}  ({paths().session(args.agent)})")
    print(f"alive    : {'yes' if p.alive else 'no'}")
    print(f"phase    : {p.phase}")
    print(f"ctx      : {p.ctx if p.ctx is not None else '-'}")
    print(f"endturns : {p.endturns if p.endturns is not None else '-'}")
    print(f"inputbox : {p.inputbox!r}" if p.inputbox is not None else "inputbox : -")
    print(f"task     : {mailbox.task_state(args.agent)} "
          f"(flag file says: {mailbox.state_flag(args.agent) or '-'})")
    return 0 if p.alive else 1


def cmd_mail(args: argparse.Namespace) -> int:
    who = args.agent or os.environ.get("AF_AGENT") or "orchestrator"
    try:
        msgs = mailbox.read(who, peek=args.peek)
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


def cmd_mailstat(args: argparse.Namespace) -> int:
    for who, n in mailbox.stat().items():
        print(f"  {who:<14} {n} unread")
    return 0


def cmd_unread(args: argparse.Namespace) -> int:
    print(mailbox.unread(args.agent or os.environ.get("AF_AGENT") or "orchestrator"))
    return 0


def cmd_spec(args: argparse.Namespace) -> int:
    try:
        s = specmod.read(args.agent)
    except specmod.SpecError as e:
        print(f"[af] {e}", file=sys.stderr)
        return 1
    print(json.dumps(s.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_not_migrated(args: argparse.Namespace) -> int:
    print(f"[af] '{args.cmd}' is not migrated yet — use: ai.sh {args.cmd} …", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af", description="agent-factory (python core)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("slug", help="the slug this directory resolves to").set_defaults(fn=cmd_slug)

    p = sub.add_parser("probe", help="one look at an agent: alive, phase, ctx, endturns, input box")
    p.add_argument("agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("mail", help="read a mailbox and ack it (the cursor is the ack)")
    p.add_argument("--agent", default=None, help="default: $AF_AGENT, else orchestrator")
    p.add_argument("--peek", action="store_true", help="read without acking")
    p.set_defaults(fn=cmd_mail)

    sub.add_parser("mailstat", help="unread count per mailbox").set_defaults(fn=cmd_mailstat)

    p = sub.add_parser("unread", help="unread count for one mailbox")
    p.add_argument("--agent", default=None)
    p.set_defaults(fn=cmd_unread)

    p = sub.add_parser("spec", help="print an agent's spec")
    p.add_argument("agent")
    p.set_defaults(fn=cmd_spec)

    for name in NOT_MIGRATED:
        q = sub.add_parser(name, help=f"(not migrated — use ai.sh {name})")
        q.add_argument("rest", nargs=argparse.REMAINDER)
        q.set_defaults(fn=cmd_not_migrated)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
