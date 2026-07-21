"""The one place to look: a JOIN of the durable specs against the LIVE world.

tmux for aliveness, the session log for context size AND for the model that actually
answered the last turn, the mailbox for unread and for busy/idle, squad.json for the
compact count, delegate_wall's own state file for the self-write counter — derived on
read, so it cannot drift from reality the way a cached status table would. The specs
supply only what the live world cannot say: role, chain of command, which wall.

Two rules this view exists to keep:

  * The WALL column is a LIVE CHECK of the hooks on disk (hooks.hooks_ok), never an echo of
    what the spec claims. It used to print [wall] for any agent whose spec merely SAID
    delegate:required — including one with no settings file at all. The single place an
    operator would look to notice a missing wall was reading the wrong column, and always
    said the wall was there. In this system safety mechanisms fail silently: a
    non-executable hook blocks nothing, Claude Code just prints an error and runs the tool.
  * The MODEL column is likewise a LIVE READ off the transcript, not an echo of the spec —
    the spec is what was PASSED at spawn; a runtime `/model` switch leaves it stale forever.
  * `ledger` does NOT sweep. It is a LOOK. It reports what a sweep WOULD compact, and — read
    off the warden's own on-disk timestamp, never by signaling the daemon — when it next will.
"""

from __future__ import annotations

from . import hooks, mailbox, roster, spec as specmod, warden
from .drive import resolve_thresholds
from .manifest import session_log_exists
from .nums import intish
from .paths import Paths, paths
from .probe import probe

HEADER = (f"{'NAME':<10} {'ROLE':<14} {'MODEL':<26} {'PARENT':<8} {'CTX':>8} {'MAIL':>5} "
          f"{'STATE':<6} {'CMP':>3} {'SELF':>4} SESSION")


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
        print(f"[af] no squad on '{p.slug}' yet (no specs in {p.specdir})")
        return 0
    names = specmod.all_specs(p)
    if not names:
        print(f"[af] no agents recorded for '{p.slug}'")
        return 0

    squad = roster.load(p)
    blueprint = squad.blueprint
    print(f"[af] squad '{p.slug}'" + (f"   blueprint: {blueprint}" if blueprint else "")
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
            # has_background reads straight off the agent's OWN transcript tail (an unresolved
            # Task/Agent dispatch), not a fold of the mail log — "task" now means "this agent
            # is actually running something in the background", not "someone mailed it once".
            state = "busy" if pr.phase == "generating" else ("task" if pr.has_background else "idle")
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

        # The model actually running is read off the transcript's last answered turn, not
        # the spec — the spec is what was PASSED at spawn and goes stale the moment a
        # runtime `/model` switch changes it. Fall back to the spec only when the
        # transcript has no usage-bearing record yet (a station that never ran a turn).
        model = pr.model or sp.model or "default"
        compacts = (squad.agents.get(name).compacts if squad.agents.get(name) else 0)

        # delegate_wall()'s cumulative advisory counter — read straight off the file the
        # hook itself writes, never through roster/Station (see module docstring: no daemon
        # staleness risk this way). "-" for a station with no delegate level configured, same
        # as _wall() staying silent on delegate=="".
        if sp.delegate in ("advised", "required"):
            try:
                self_lines = str(intish(p.self_lines(name).read_text(encoding="utf-8").strip(), 0))
            except OSError:
                self_lines = "0"
        else:
            self_lines = "-"

        print(f"{name:<10} {sp.role or '-':<14} {model:<26} "
              f"{sp.parent or '-':<8} {ctx or '-':>8} {unread:>5} {state or '-':<6} "
              f"{compacts:>3} {self_lines:>4} {alive}{_wall(sp.delegate, sp.settings)}")

    print()
    if fat:
        print(f"[af] ⚠ past their soft threshold: {' '.join(fat)} — 'af sweep' will compact "
              f"the idle ones.")
    eta = warden.next_sweep_in(p)
    if eta is None:
        print("[af] warden not watching this squad — no scheduled sweep")
    elif eta <= 0:
        print("[af] next warden sweep: due now")
    else:
        print(f"[af] next warden sweep: {eta}s")
    return 0
