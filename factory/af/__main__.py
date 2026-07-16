"""python3 -m af <cmd> — the Python CLI. Everything ai.sh does, plus mail.sh's transport.

The bash is still live and still correct; both implementations drive the SAME agents
through the SAME files on disk (mailboxes, cursors, specs, the manifest, the mkdir locks).
An agent spawned by `af up` is drivable by `ai.sh ask`, and an agent spawned by `ai.sh up`
is drivable by `af ask` — that interoperability is the whole safety net of the migration,
so nothing here may write a file the bash cannot read.

  writers into a pane : say  ask  wait  keys  approve  compact  screen  result  ctx
  lifecycle           : up  down  list  slug  attach  remote  revive  revivable  ledger
  mail                : post  mail/inbox  mailstat  register-self  unregister-self
  the context guard   : sweep  (post and mail run one automatically; ledger deliberately
                        does NOT — it is a look, and it reports what a sweep would do)
  delegation levers   : delegate (one-command delegate-to-local-model call)
                        read-force (one-shot escape hatch for the read-wall hook)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from . import drive, ledger as ledgermod, lifecycle, mailbox, postal
from . import spec as specmod, sweep as sweepmod
from .paths import paths
from .probe import probe as do_probe


# --- observation -----------------------------------------------------------------
def cmd_slug(a: argparse.Namespace) -> int:
    print(paths().slug)
    return 0


def cmd_probe(a: argparse.Namespace) -> int:
    p = do_probe(a.agent)
    if a.json:
        print(json.dumps(dataclasses.asdict(p)))
        return 0 if p.alive else 1
    print(f"agent    : {a.agent}  ({paths().session(a.agent)})")
    print(f"alive    : {'yes' if p.alive else 'no'}")
    print(f"phase    : {p.phase}")
    print(f"ctx      : {p.ctx if p.ctx is not None else '-'}")
    print(f"endturns : {p.endturns if p.endturns is not None else '-'}")
    print(f"inputbox : {p.inputbox!r}" if p.inputbox is not None else "inputbox : -")
    print(f"task     : {mailbox.task_state(a.agent)} "
          f"(flag file says: {mailbox.state_flag(a.agent) or '-'})")
    return 0 if p.alive else 1


def cmd_screen(a: argparse.Namespace) -> int:
    s = drive.screen(a.agent)
    if s is None:
        print(f"[af] no agent '{a.agent}'")
        return 1
    print(s, end="" if s.endswith("\n") else "\n")
    return 0


def cmd_result(a: argparse.Namespace) -> int:
    r = drive.result(a.agent)
    if r is None:
        print(f"[af] no log for '{a.agent}' (not spawned by this skill?)")
        return 1
    print(r)
    return 0


def cmd_ctx(a: argparse.Namespace) -> int:
    print(f"[af] '{a.agent}' context ≈ {drive.ctx(a.agent)} tokens")
    return 0


def cmd_spec(a: argparse.Namespace) -> int:
    try:
        s = specmod.read(a.agent)
    except specmod.SpecError as e:
        print(f"[af] {e}", file=sys.stderr)
        return 1
    print(json.dumps(s.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_ledger(a: argparse.Namespace) -> int:
    return ledgermod.ledger()


# --- writers ---------------------------------------------------------------------
def cmd_say(a: argparse.Namespace) -> int:
    return 0 if drive.say(a.agent, " ".join(a.text)) else 1


def cmd_ask(a: argparse.Namespace) -> int:
    return drive.ask(a.agent, " ".join(a.text))


def cmd_keys(a: argparse.Namespace) -> int:
    return 0 if drive.keys(a.agent, *a.keys) else 1


def cmd_wait(a: argparse.Namespace) -> int:
    p = paths()
    # `is None` — an empty STRING is a live pane that has not painted yet (the seconds right
    # after a launch), not a missing agent.
    if drive.screen(a.agent) is None:
        print(f"[af] no agent '{a.agent}'")
        return 1
    print(drive.wait_idle(a.agent, a.timeout, p=p))
    return 0


def cmd_approve(a: argparse.Namespace) -> int:
    return drive.approve(a.agent, a.choice)


def cmd_compact(a: argparse.Namespace) -> int:
    return 0 if drive.compact(a.agent) else 1


# --- lifecycle -------------------------------------------------------------------
def cmd_up(a: argparse.Namespace) -> int:
    if a.window:
        print("[af] note: -w/--window is gone — agents are tmux-only now.", file=sys.stderr)
    return lifecycle.up(a.agent)


def cmd_down(a: argparse.Namespace) -> int:
    return lifecycle.down(a.agent)


def cmd_list(a: argparse.Namespace) -> int:
    return lifecycle.list_agents()


def cmd_attach(a: argparse.Namespace) -> int:
    return lifecycle.attach(a.agent)


def cmd_remote(a: argparse.Namespace) -> int:
    return lifecycle.remote(a.agent, a.sid or "")


def cmd_revive(a: argparse.Namespace) -> int:
    return lifecycle.revive(a.agent, a.sid or "")


def cmd_revivable(a: argparse.Namespace) -> int:
    return lifecycle.revivable()


def cmd_register(a: argparse.Namespace) -> int:
    return lifecycle.register_self()


def cmd_unregister(a: argparse.Namespace) -> int:
    return lifecycle.unregister_self()


# --- mail ------------------------------------------------------------------------
def cmd_post(a: argparse.Namespace) -> int:
    # A dangling `--kind` with no value is the one input the two sides read differently:
    # bash's parser breaks on the orphan flag, files the message as `fyi` (marking nobody
    # busy) and puts the literal word "--kind" in the body. Refuse it rather than half-obey.
    if a.kind is not None and not a.kind:
        print("[af] --kind needs a value (task|question|blocked|result|done|fyi)",
              file=sys.stderr)
        return 1
    return postal.post(a.agent, " ".join(a.text), a.kind or "")


def cmd_mail(a: argparse.Namespace) -> int:
    who = a.agent or os.environ.get("AF_AGENT") or "orchestrator"
    if a.dump:
        msgs = mailbox.dump(who)
        if not msgs:
            print(f"[mail] mailbox of '{who}' is empty")
            return 0
        for m in msgs:
            print(f"── from: {m.frm}   kind: {m.kind}   id: {m.id}")
            print(m.body)
        return 0
    return postal.read_mail(a.agent, peek=a.peek)


def cmd_ring(a: argparse.Namespace) -> int:
    if drive.ring(a.agent):
        print(f"[af] rang '{a.agent}'")
        return 0
    print(f"[af] '{a.agent}' has no live/idle pane")
    return 1


def cmd_mailstat(a: argparse.Namespace) -> int:
    for who, n in mailbox.stat().items():
        print(f"  {who:<14} {n} unread")
    return 0


def cmd_unread(a: argparse.Namespace) -> int:
    print(mailbox.unread(a.agent or os.environ.get("AF_AGENT") or "orchestrator"))
    return 0


def cmd_line(a: argparse.Namespace) -> int:
    from . import line
    return line.main(a.rest)


def cmd_warden(a: argparse.Namespace) -> int:
    from . import warden
    return warden.main(a.rest)


def cmd_postmaster(a: argparse.Namespace) -> int:
    from . import postmaster
    return postmaster.main(a.rest)


def cmd_polling(a: argparse.Namespace) -> int:
    from . import polling
    return polling.main(a.rest)


def cmd_sweep(a: argparse.Namespace) -> int:
    return sweepmod.sweep(a.skip or "")


# --- delegation levers -------------------------------------------------------------
DELEGATE_SKILL = os.path.expanduser(
    "~/.claude/skills/delegate-to-local-model/scripts/agent.py")


def cmd_delegate(a: argparse.Namespace) -> int:
    """`af delegate "<spec>" [out]` — one tool call in place of the multi-step dance (recall
    the skill exists, build the agent.py invocation, stage a prompt, collect output) that
    made writing the code inline the ONE-step option, and delegating the several-step one.

    --root scopes the local model's sandbox: never the whole repo (the over-broad-root
    mistake this project's own author already made and documented once) — default to
    AF_WORK (a station's own scratch zone), not cwd."""
    import subprocess
    import tempfile

    if not os.path.isfile(DELEGATE_SKILL):
        print(f"[af] delegate-to-local-model skill not found at {DELEGATE_SKILL}",
              file=sys.stderr)
        return 1

    root = a.root or os.environ.get("AF_WORK") or os.getcwd()
    out = a.out
    if not out:
        fd, out = tempfile.mkstemp(prefix="af-delegate-", suffix=".txt")
        os.close(fd)

    argv = [sys.executable, DELEGATE_SKILL, "--root", root, "--out", out]
    if a.think:
        argv.append("--think")
    argv.append(a.spec)

    r = subprocess.run(argv)
    print(f"[af] delegated (root={root}) — full answer: {out}")
    return r.returncode


def cmd_read_force(a: argparse.Namespace) -> int:
    from . import hooks
    return hooks.read_force(a.path)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af", description="agent-factory (python core)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def agent_cmd(name: str, fn, help_: str, default: str = "claude"):
        q = sub.add_parser(name, help=help_)
        q.add_argument("agent", nargs="?", default=default)
        q.set_defaults(fn=fn)
        return q

    sub.add_parser("slug", help="the slug this directory resolves to").set_defaults(fn=cmd_slug)

    q = sub.add_parser("probe", help="one look: alive, phase, ctx, endturns, input box")
    q.add_argument("agent")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_probe)

    agent_cmd("screen", cmd_screen, "print the current TUI screen")
    agent_cmd("result", cmd_result, "the last completed turn's text (from the log)")
    agent_cmd("ctx", cmd_ctx, "estimated context size in tokens")
    agent_cmd("compact", cmd_compact, "run /compact (only at a safe point)")
    q = agent_cmd("up", cmd_up, "launch interactive claude in a detached tmux session")
    # Accepted and IGNORED, as in bash: an old call site (or an agent working from a stale
    # brief) must not die on it — nor have it swallowed as the NAME, which would spawn an
    # agent literally called "-w".
    q.add_argument("-w", "--window", action="store_true", help=argparse.SUPPRESS)
    agent_cmd("down", cmd_down, "quit claude + kill the session")
    agent_cmd("attach", cmd_attach, "print the command to attach a viewer")

    q = sub.add_parser("spec", help="print an agent's spec")
    q.add_argument("agent")
    q.set_defaults(fn=cmd_spec)

    # REMAINDER, not "+": bash passed the body as "$*", so a message that happens to start
    # with a dash ("-x is broken") is a message, not an unknown option to die on.
    q = sub.add_parser("say", help="type text into an agent and submit")
    q.add_argument("agent")
    q.add_argument("text", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_say)

    q = sub.add_parser("ask", help="say, wait for the turn to finish, print its result")
    q.add_argument("agent")
    q.add_argument("text", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_ask)

    q = sub.add_parser("keys", help="send raw tmux keys (Escape, C-c, …)")
    q.add_argument("agent")
    q.add_argument("keys", nargs="+")
    q.set_defaults(fn=cmd_keys)

    q = sub.add_parser("wait", help="block until the agent is idle or needs input")
    q.add_argument("agent", nargs="?", default="claude")
    q.add_argument("timeout", nargs="?", type=int, default=None)
    q.set_defaults(fn=cmd_wait)

    q = sub.add_parser("approve", help="answer a tool-permission prompt (default 2)")
    q.add_argument("agent", nargs="?", default="claude")
    q.add_argument("choice", nargs="?", default="2")
    q.set_defaults(fn=cmd_approve)

    q = sub.add_parser("remote", help="(re)launch with Remote Control")
    q.add_argument("agent", nargs="?", default="claude")
    q.add_argument("sid", nargs="?", default="")
    q.set_defaults(fn=cmd_remote)

    q = sub.add_parser("revive", help="relaunch a killed agent with its memory AND its role")
    q.add_argument("agent", nargs="?", default="claude")
    q.add_argument("sid", nargs="?", default="")
    q.set_defaults(fn=cmd_revive)

    sub.add_parser("revivable", help="downed agents with a surviving log").set_defaults(fn=cmd_revivable)
    sub.add_parser("list", help="list running interactive agents").set_defaults(fn=cmd_list)
    sub.add_parser("ledger", help="one view of the line: role, model, ctx, mail, alive?").set_defaults(fn=cmd_ledger)
    sub.add_parser("register-self", help="let agents WAKE this session by mail").set_defaults(fn=cmd_register)
    sub.add_parser("unregister-self", help="stop agents from waking this session").set_defaults(fn=cmd_unregister)

    q = sub.add_parser("post", help="send mail to an agent + ring its doorbell")
    q.add_argument("agent")
    # nargs="?" with no value ⇒ "" (the dangling --kind), which cmd_post REFUSES.
    q.add_argument("--kind", nargs="?", const="", default=None)
    q.add_argument("text", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_post)

    for nm in ("mail", "inbox"):   # `inbox` is the old name; it forwards
        q = sub.add_parser(nm, help="read YOUR mailbox and ack it (the cursor is the ack)")
        q.add_argument("--agent", default=None, help="default: $AF_AGENT, else orchestrator")
        q.add_argument("--peek", action="store_true", help="read without acking")
        # The recovery path, and the one `mailbox.read` names by hand when it has to report a
        # message it acked but could not decode. It must therefore EXIST.
        q.add_argument("--dump", action="store_true",
                       help="print the whole mailbox, read or unread (recovery)")
        q.set_defaults(fn=cmd_mail)

    q = sub.add_parser("ring", help="ring an agent's doorbell (deliver nothing, just wake it)")
    q.add_argument("agent")
    q.set_defaults(fn=cmd_ring)

    sub.add_parser("mailstat", help="unread count per mailbox").set_defaults(fn=cmd_mailstat)

    q = sub.add_parser("unread", help="unread count for one mailbox")
    q.add_argument("--agent", default=None)
    q.set_defaults(fn=cmd_unread)

    q = sub.add_parser("sweep", help="compact agents past their threshold")
    q.add_argument("--skip", default="", help="an agent the caller is about to touch")
    q.set_defaults(fn=cmd_sweep)

    # A whole line, the context guard, and per-agent timers each own a nested command tree
    # (`af line up …`, `af warden watch`, `af polling start …`). Their parsers live in their
    # own modules; here they are one passthrough each, so the module stays the single owner
    # of its own arg grammar.
    q = sub.add_parser("line", help="a team from a blueprint.yml (plan/up/status/down)")
    q.add_argument("rest", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_line)

    q = sub.add_parser("warden", help="the context guard + usage-limit rescue (watch/status/stop)")
    q.add_argument("rest", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_warden)

    q = sub.add_parser("postmaster", help="squad state + mail safety net (watch/status/stop)")
    q.add_argument("rest", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_postmaster)

    q = sub.add_parser("polling", help="re-poke an agent by mail on a clock (start/stop/list/status)")
    q.add_argument("rest", nargs=argparse.REMAINDER)
    q.set_defaults(fn=cmd_polling)

    q = sub.add_parser("delegate", help="one-command delegate-to-local-model call")
    q.add_argument("spec", help="the task/spec text for the local model")
    q.add_argument("out", nargs="?", default=None,
                   help="where to write the full answer (default: a temp file)")
    q.add_argument("--root", default=None, help="sandbox root (default: $AF_WORK, then cwd)")
    q.add_argument("--think", action="store_true", help="enable reasoning in the local model")
    q.set_defaults(fn=cmd_delegate)

    q = sub.add_parser("read-force",
                       help="one-shot escape hatch for the read-wall PreToolUse hook")
    q.add_argument("path")
    q.set_defaults(fn=cmd_read_force)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
