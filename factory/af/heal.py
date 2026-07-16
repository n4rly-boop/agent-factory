"""heal — walk a squad, find what broke, put it back WITHOUT losing context.

The failures this repairs, in the order it is safe to repair them:

  sid drift        The agent is alive but sid-<agent> names the frozen parent of a forked
                   session (see af/live.py). The warden is reading a dead context number.
                   Repair: rewrite the sid file to the live session id. No restart, zero risk.

  stale settings   The settings file is missing, or its hooks would fail open, or it predates
                   the SessionStart hook that keeps the sid file honest. Repair: regenerate it
                   on disk. A LIVE agent will not load the new file until it restarts — heal
                   says so and does NOT restart it unless asked.

  crashed agent    The tmux session is gone, or it survives but the claude process inside it
                   died (a frozen pane). Repair: revive it on its RECORDED session — the whole
                   memory comes back, because --resume replays the transcript. This is the
                   "агент упал, верни его не теряя контекст" case.

The safety rule that governs all of it: a LIVE claude is never killed. Reviving an agent
that is quietly working would throw away its in-flight turn. So revive fires only on an agent
that is provably down (its tmux session is gone) or provably crashed (its session is up but
no claude process is running for it, and we can see OTHER claude processes — proof that our
process scan works and the absence is real, not a failed `ps`).

Default is to apply the safe repairs and revive the dead. `--dry-run` reports and touches
nothing. `--restart-idle` additionally restarts a live-but-idle agent so it loads a
regenerated settings file — never a busy one.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from . import hooks, lifecycle, live, tmux
from .paths import Paths


@dataclass
class Finding:
    name: str
    session_alive: bool          # a tmux session exists for the agent
    claude_alive: bool           # a claude process is running for it
    file_sid: str
    live_sid: str | None
    drift: bool                  # alive, but the sid file names the wrong session
    settings_path: str
    settings_ok: bool            # file exists AND every installed hook is executable
    has_session_start: bool      # settings install the SessionStart sid-keeper
    limits_seen: bool            # limits.json present — usage-limit rescue is armed
    idle: bool                   # phase == idle (safe to restart)
    notes: list[str] = field(default_factory=list)

    @property
    def down(self) -> bool:
        return not self.session_alive or not self.claude_alive

    @property
    def healthy(self) -> bool:
        return (not self.down and not self.drift and self.settings_ok
                and self.has_session_start)


def _read_sid(name: str, p: Paths) -> str:
    try:
        return p.sid_file(name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _has_session_start(settings_path: str) -> bool:
    """True if the settings file installs the SessionStart sid-keeper. Checked by basename so
    it survives the hooks dir moving."""
    for cmd in hooks.hook_commands(settings_path):
        if Path(cmd).name == "session-start.sh":
            return True
    return False


def diagnose(name: str, p: Paths, ps_out: str, probe_fn=None) -> Finding:
    """Read-only. Everything heal knows about one agent, from tmux, the process table, the
    sid file and the settings file. `ps_out` is passed in (one scan for the whole squad);
    `probe_fn` is injectable so tests need no live pane."""
    session_alive = tmux.has_session(p.session(name))
    lsid = live.live_sid(name, p, ps_out=ps_out)
    claude_alive = lsid is not None
    file_sid = _read_sid(name, p)
    drift = bool(claude_alive and lsid and lsid != file_sid)

    stf = str(p.settings_file(name))
    settings_ok = hooks.hooks_ok(stf, quiet=True)
    has_ss = _has_session_start(stf)
    limits_seen = (p.state / "limits.json").is_file()

    idle = False
    if claude_alive:
        try:
            fn = probe_fn or _default_probe
            idle = fn(name, p).phase == "idle"
        except Exception:
            idle = False

    return Finding(
        name=name, session_alive=session_alive, claude_alive=claude_alive,
        file_sid=file_sid, live_sid=lsid, drift=drift, settings_path=stf,
        settings_ok=settings_ok, has_session_start=has_ss, limits_seen=limits_seen,
        idle=idle,
    )


def _default_probe(name: str, p: Paths):
    from . import probe as probemod
    return probemod.probe(name, p)


@dataclass
class Options:
    dry_run: bool = False
    restart_idle: bool = False


def repair(f: Finding, slug: str, p: Paths, opts: Options) -> list[str]:
    """Apply the safe repairs for one finding. Returns the actions taken (or, in dry-run, the
    actions that WOULD be taken). Ordered so the sid file is correct before anything resumes
    on it."""
    from . import squad as squadmod

    acts: list[str] = []
    dry = opts.dry_run

    # 1. sid drift — rewrite the pointer. Never touches the running session.
    if f.drift:
        if dry:
            acts.append(f"WOULD heal sid: {f.file_sid[:8] or '(none)'} → {f.live_sid[:8]}")
        else:
            new = live.heal_sid_file(f.name, p, ps_out=None)
            acts.append(f"healed sid: {f.file_sid[:8] or '(none)'} → {(new or f.live_sid)[:8]}")

    # 2. settings — regenerate if missing/broken or predating the SessionStart hook.
    need_settings = (not f.settings_ok) or (not f.has_session_start)
    if need_settings:
        why = "missing/fail-open" if not f.settings_ok else "predates SessionStart hook"
        if dry:
            acts.append(f"WOULD regenerate settings ({why})")
        else:
            squadmod.write_settings(slug, f.name, f.settings_path)
            ok = hooks.hooks_ok(f.settings_path, quiet=True)
            acts.append(f"regenerated settings ({why}){'' if ok else ' — STILL FAIL-OPEN'}")
            # A live agent keeps running its OLD in-memory settings until it restarts.
            if f.claude_alive and not f.has_session_start:
                if opts.restart_idle and f.idle:
                    acts.append(_restart(f.name, p, dry))
                else:
                    acts.append("↳ restart to load it (live agent still on old hooks) — "
                                "af down " + f.name + " && af squad up --resume <bp>, or "
                                "rerun heal with --restart-idle")

    # 3. crashed / down — revive on the recorded session. Context preserved via --resume.
    if f.down:
        if dry:
            src = "session gone" if not f.session_alive else "pane frozen (claude crashed)"
            tgt = f.file_sid[:8] or "(no recorded sid!)"
            acts.append(f"WOULD revive ({src}) → resume {tgt}")
            if not f.file_sid:
                acts.append("↳ ⚠ no recorded sid — revive would spawn FRESH (memory lost); "
                            "not safe to auto-heal")
        else:
            acts.append(_revive(f.name, p))

    return acts


def _restart(name: str, p: Paths, dry: bool) -> str:
    if dry:
        return "WOULD restart (idle) to load new settings"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lifecycle.down(name, p)
        rc = lifecycle.revive(name, p=p)
    return f"restarted idle agent to load new settings (rc={rc})"


def _revive(name: str, p: Paths) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lifecycle.revive(name, p=p)
    tail = " ".join(l for l in buf.getvalue().splitlines() if "refus" in l.lower())
    if rc != 0:
        return f"revive FAILED (rc={rc}) {tail}".strip()
    return "revived on its recorded session (memory kept)"


# ======================================================================================
# the command
# ======================================================================================
def _fmt(f: Finding) -> str:
    if f.down:
        state = "DOWN" if not f.session_alive else "CRASHED"
    elif f.drift:
        state = "drift"
    elif not f.healthy:
        state = "stale"
    else:
        state = "ok"
    live_s = (f.live_sid or "----")[:8]
    flags = []
    if f.drift:
        flags.append(f"sid {f.file_sid[:8] or '--'}→{live_s}")
    if not f.settings_ok:
        flags.append("settings-fail-open")
    elif not f.has_session_start:
        flags.append("no-SessionStart-hook")
    if not f.limits_seen and f.claude_alive:
        flags.append("no-limits.json(rescue-blind)")
    return f"  {f.name:<10} {state:<8} {'  '.join(flags)}"


def heal(stations, p: Paths, opts: Options) -> int:
    """Walk every station, diagnose it, repair it. `stations` is the parsed blueprint — the
    record of who is SUPPOSED to be on the squad."""
    slug = stations[0].slug if stations else p.slug
    ps_out = live._ps()
    ps_has_claude = "claude" in ps_out
    if not ps_out.strip():
        print("[heal] FATAL: could not read the process table — refusing to guess which "
              "agents are down (would risk reviving live ones).")
        return 1

    print(f"[heal] squad '{slug}' — {len(stations)} station(s)"
          f"{'  (dry-run — nothing will change)' if opts.dry_run else ''}")
    healed = broke = 0
    for st in stations:
        f = diagnose(st.name, p, ps_out)
        # If the scan sees no claude AT ALL, its absence is not evidence — a session-alive
        # agent must not be judged crashed on a blind scan.
        if f.session_alive and not f.claude_alive and not ps_has_claude:
            f.claude_alive = True
            f.notes.append("process scan saw no claude at all — assuming alive, not crashed")
        print(_fmt(f))
        for note in f.notes:
            print(f"       · {note}")
        if f.healthy:
            continue
        for act in repair(f, slug, p, opts):
            print(f"       → {act}")
            if "FAILED" in act or "STILL FAIL-OPEN" in act:
                broke += 1
            else:
                healed += 1

    if opts.dry_run:
        print("[heal] dry-run complete — rerun without --dry-run to apply.")
    else:
        print(f"[heal] done — {healed} repair(s) applied"
              f"{f', {broke} still broken' if broke else ''}.")
    return 1 if broke else 0
