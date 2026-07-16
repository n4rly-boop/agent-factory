"""The one place to look: a JOIN of the durable specs against the LIVE world.

tmux for aliveness, the session log for context size, the mailbox for unread and for
busy/idle — derived on read, so it cannot drift from reality the way a cached status table
would. The specs supply only what the live world cannot say: role, chain of command, which
model, which wall.

Two rules this view exists to keep:

  * The WALL column is a LIVE CHECK of the hooks on disk (hooks.hooks_ok), never an echo of
    what the spec claims. It used to print [wall] for any agent whose spec merely SAID
    delegate:required — including one with no settings file at all. The single place an
    operator would look to notice a missing wall was reading the wrong column, and always
    said the wall was there. In this system safety mechanisms fail silently: a
    non-executable hook blocks nothing, Claude Code just prints an error and runs the tool.
  * `ledger` does NOT sweep. It is a LOOK. It reports what a sweep WOULD compact.
"""

from __future__ import annotations

from . import hooks, mailbox, spec as specmod
from .drive import resolve_thresholds
from .manifest import session_log_exists
from .paths import Paths, paths
from .probe import probe

HEADER = f"{'NAME':<10} {'ROLE':<14} {'MODEL':<8} {'PARENT':<8} {'CTX':>8} {'MAIL':>5} {'STATE':<6} SESSION"


def _wall(delegate: str, settings: str) -> str:
    if delegate == "required":
        if settings and hooks.hooks_ok(settings, quiet=True):
            return "  [wall]"
        return "  !! NO WALL (hooks missing/not executable)"
    if delegate == "advised":
        if settings and hooks.hooks_ok(settings, quiet=True):
            return "  [advise]"
        return "  [advise: hooks broken]"
    return ""


def ledger(p: Paths | None = None) -> int:
    p = p or paths()
    if not p.specdir.is_dir():
        print(f"[af] no line on '{p.slug}' yet (no specs in {p.specdir})")
        return 0
    names = specmod.all_specs(p)
    if not names:
        print(f"[af] no agents recorded for '{p.slug}'")
        return 0

    blueprint = ""
    try:
        import json
        blueprint = str(json.loads(p.line_file.read_text(encoding="utf-8")).get("blueprint", ""))
    except Exception:
        pass
    print(f"[af] line '{p.slug}'" + (f"   blueprint: {blueprint}" if blueprint else "")
          + f"   specs: {p.specdir}")
    print()
    print(HEADER)

    fat = []
    for name in names:
        try:
            sp = specmod.read(name, p)
        except specmod.SpecError:
            sp = None
        if sp is None or not sp.name:
            # A spec that will not parse used to be skipped silently — so the one agent whose
            # wall is gone was the one agent missing from the view meant to reveal that.
            print(f"{name:<10} !! SPEC CORRUPT ({p.spec_file(name)}) — will refuse to revive; "
                  f"fix or delete it")
            continue

        pr = probe(name, p)
        if pr.alive:
            alive = "● alive"
            ctx = str(pr.ctx or 0)
            # task_state() folds the answer out of the mail log itself on every read, so a
            # dead agent's stale busy flag (nothing reaps it anymore — see af.sweep) cannot
            # pin this display on "task" forever the way reading the raw flag would.
            state = ("busy" if pr.phase == "generating"
                     else ("task" if mailbox.task_state(name, p) == "busy" else "idle"))
        else:
            alive = "○ down"
            ctx = ""
            state = ""
            if sp.sid and session_log_exists(sp.sid, p):
                alive = "○ down (revivable)"

        unread = mailbox.unread(name, p)
        # Each agent judged by ITS OWN soft threshold (from its spec), exactly as sweep does.
        aso, _ahard = sp.thresholds()
        soft, _h = resolve_thresholds(aso, None)
        if soft != 0 and pr.alive and (pr.ctx or 0) > soft:
            fat.append(name)

        print(f"{name:<10} {sp.role or '-':<14} {sp.model or 'default':<8} "
              f"{sp.parent or '-':<8} {ctx or '-':>8} {unread:>5} {state or '-':<6} "
              f"{alive}{_wall(sp.delegate, sp.settings)}")

    print()
    if fat:
        print(f"[af] ⚠ past their soft threshold: {' '.join(fat)} — 'af sweep' will compact "
              f"the idle ones.")
    return 0
