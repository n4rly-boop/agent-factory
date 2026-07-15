"""The session an agent is ACTUALLY running right now — read from the live process, not
from the sid file written once at spawn.

The problem this solves. `sid-<agent>` is written exactly once, in lifecycle.up(), and never
again. But Claude Code FORKS the session on `--resume`: the pane launches
`claude --resume <parent>`, and the running session becomes a NEW id
(`--session-id <fork> --fork-session --resume <parent>.jsonl`). The parent's transcript
freezes at the fork point; the fork's transcript is the one that grows. So the moment an
agent is resumed — by the warden's usage-limit rescue, by a human, by a crash-and-restart —
the sid file names a DEAD transcript, and every reader that trusts it (probe, sweep, warden)
reads a frozen context number. That is the "warden keeps compacting an agent that is already
at 0%" bug: it reads 371k from the parent while the live fork is empty.

The fix is to ask the running process what session it is on. A claude process carries its
identity in argv:

    launcher, never forked : claude --resume <sid> --settings .../lines/<slug>/settings-<agent>.json
    fork worker            : ... --session-id <fork> --fork-session --resume <parent>.jsonl --settings …-<agent>.json
    fresh spawn            : claude --session-id <sid> --settings …-<agent>.json

We match an agent by the ONE unambiguous token: the --settings path, whose basename is
`settings-<agent>.json` and whose directory is the line's spec dir (…/lines/<slug>/). From
its process lines we gather every --session-id and every --resume target, and the live
session is:

    the --session-id that is NOT anyone's --resume target   (a fork, or a fresh spawn)
    else the --resume target                                (a launcher that has not forked)

A --session-id that IS a resume target is the frozen parent — never the live one. This holds
whether or not a fork happened, without racing on file mtimes (mtime is only a tiebreak
inside a set, which in practice has one element).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .paths import Paths, paths

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SETTINGS = re.compile(r"--settings[=\s]+(\S+)")
_SESSION_ID = re.compile(r"--session-id[=\s]+(" + _UUID + r")")
# --resume takes either a bare sid or a path to <sid>.jsonl; grab the sid either way.
_RESUME = re.compile(r"--resume[=\s]+(?:\S*/)?(" + _UUID + r")")


def _ps() -> str:
    """Every process' full argv. `-o command=` is the full command line on both BSD (macOS)
    and GNU (Linux) ps; the trailing `=` drops the header row. A failure here is not fatal —
    it just means we cannot see the live session and fall back to the sid file."""
    try:
        return subprocess.run(
            ["ps", "-A", "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return ""


def _agent_lines(ps_out: str, slug: str, agent: str) -> list[str]:
    """The process lines that belong to THIS agent on THIS line — matched by the --settings
    path, the only token that names both slug and agent unambiguously."""
    want = f"settings-{agent}.json"
    marker = f"/lines/{slug}/"
    out = []
    for ln in ps_out.splitlines():
        m = _SETTINGS.search(ln)
        if not m:
            continue
        st = m.group(1)
        if os.path.basename(st) != want:
            continue
        # The spec dir is …/lines/<slug>/settings-<agent>.json. Requiring the slug segment
        # keeps two lines that share an agent name (aae1/orc and inna/orc) apart.
        if marker not in st and f"{os.sep}lines{os.sep}{slug}{os.sep}" not in st:
            continue
        out.append(ln)
    return out


def _newest(sids: set[str], projects: Path) -> str | None:
    """Of a set of session ids, the one whose transcript was written most recently. Used only
    to break a tie inside `live` or `frozen` sets, which normally hold a single id."""
    best: str | None = None
    best_mt = -1.0
    for sid in sids:
        for root, _dirs, files in os.walk(projects):
            if f"{sid}.jsonl" in files:
                try:
                    mt = (Path(root) / f"{sid}.jsonl").stat().st_mtime
                except OSError:
                    mt = -1.0
                if mt > best_mt:
                    best_mt, best = mt, sid
                break
    # Nothing on disk (log not created yet): fall back to any element so a caller still gets
    # an answer rather than None.
    if best is None and sids:
        best = sorted(sids)[0]
    return best


def live_sid(agent: str, p: Paths | None = None, ps_out: str | None = None) -> str | None:
    """The session id the agent's live process is running, or None if it has no process.

    ps_out is injectable so the parsing is testable without a real `ps`.
    """
    p = p or paths()
    text = _ps() if ps_out is None else ps_out
    lines = _agent_lines(text, p.slug, agent)
    if not lines:
        return None

    session_ids: set[str] = set()
    resume_targets: set[str] = set()
    for ln in lines:
        for m in _SESSION_ID.finditer(ln):
            session_ids.add(m.group(1).lower())
        for m in _RESUME.finditer(ln):
            resume_targets.add(m.group(1).lower())

    live = session_ids - resume_targets          # a fork, or a fresh spawn
    if live:
        return _newest(live, p.projects)
    if resume_targets:                            # a launcher that has not forked
        return _newest(resume_targets, p.projects)
    return None


def heal_sid_file(agent: str, p: Paths | None = None, ps_out: str | None = None) -> str | None:
    """Point sid-<agent> at the session the agent is actually running. Returns the new sid if
    it CHANGED (drift was corrected), else None. A no-op when the agent has no live process —
    a down agent's sid file is the only record of what to resume, and must not be cleared.

    The log-path cache is invalidated on a change: it maps the OLD sid to a now-frozen
    transcript, and probe would keep serving it.
    """
    p = p or paths()
    live = live_sid(agent, p, ps_out=ps_out)
    if not live:
        return None
    try:
        current = p.sid_file(agent).read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if live == current:
        return None
    try:
        p.state.mkdir(parents=True, exist_ok=True)
        p.sid_file(agent).write_text(live, encoding="utf-8")
        p.log_cache(agent).unlink(missing_ok=True)
    except OSError:
        return None
    return live
