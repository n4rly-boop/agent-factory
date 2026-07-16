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

from . import tmux
from .paths import Paths, paths

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SETTINGS = re.compile(r"--settings[=\s]+(\S+)")
_SESSION_ID = re.compile(r"--session-id[=\s]+(" + _UUID + r")")
# --resume takes either a bare sid or a path to <sid>.jsonl; grab the sid either way.
_RESUME = re.compile(r"--resume[=\s]+(?:\S*/)?(" + _UUID + r")")


def _ps() -> str:
    """Every process' full argv. `-o command=` is the full command line on both BSD (macOS)
    and GNU (Linux) ps; the trailing `=` drops the header row. A failure here is not fatal —
    it just means we cannot see the live session and fall back to the sid file.

    text=False + explicit replace-decode: an UNRELATED process elsewhere on a shared host can
    carry a non-UTF-8 byte in its argv (a stray git branch name, a binary blob passed as a CLI
    arg — observed in practice). `text=True` decodes strictly and raises on that byte, and the
    bare except below then returns "" — blinding live_sid/reconcile/heal for every agent on the
    machine, not just the one with the odd argv, and doing so non-deterministically depending on
    what else happens to be running. Decoding permissively confines the damage to that one
    process' line, which no --settings/--session-id/--resume match cares about anyway."""
    try:
        raw = subprocess.run(
            ["ps", "-A", "-o", "command="],
            capture_output=True, timeout=5,
        ).stdout
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def _ps_tree() -> list[tuple[str, str, str]]:
    """Every process as (pid, ppid, command) triples.

    Same bytes+errors="replace" decode discipline as `_ps()` — a non-UTF-8 byte in one
    process' argv must not blind the whole read. Returns an empty list on failure."""
    try:
        raw = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True, timeout=5,
        ).stdout
        text = raw.decode("utf-8", "replace")
    except Exception:
        return []
    out: list[tuple[str, str, str]] = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        # pid and ppid are right-justified numeric fields; command is the rest.
        # ps -o pid=,ppid=,command= produces lines like:
        # "  12345     1 /usr/bin/claude ..."
        # We split on whitespace: first two tokens are pid/ppid, rest is command.
        parts = ln.split(None, 2)
        if len(parts) >= 3:
            out.append((parts[0].strip(), parts[1].strip(), parts[2]))
        elif len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip(), ""))
    return out


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


def _resolve_sid(session_ids: set[str], resume_targets: set[str],
                 projects: Path) -> str | None:
    """Given collected session_ids and resume_targets, determine the live session id.

    The live session is the --session-id that is NOT anyone's --resume target (a fork, or a
    fresh spawn). If there is no such id, the --resume target IS the live session (a launcher
    that has not forked). If neither exists, there is nothing to resolve.

    This is the core resolution logic shared by `live_sid` (factory agents matched by
    --settings) and `sid_in_pane` (standalone targets matched by process tree).
    """
    live = session_ids - resume_targets  # a fork, or a fresh spawn
    if live:
        return _newest(live, projects)
    if resume_targets:  # a launcher that has not forked
        return _newest(resume_targets, projects)
    return None


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

    return _resolve_sid(session_ids, resume_targets, p.projects)


def pane_root_pid(target: str) -> str | None:
    """Resolve the OS pid of the process tmux is running in the named session/pane.

    Uses `tmux list-panes -t <target> -F '#{pane_pid}'` and returns the first line,
    stripped, or None if the tmux call fails or the target doesn't exist."""
    pids = tmux.list_panes(target)
    if not pids:
        return None
    return pids[0].strip()


def sid_in_pane(target: str, ps_out: str | None = None) -> str | None:
    """The live session id of whichever claude process is a descendant of that pane's root pid,
    or None if there isn't one, or if the process that IS there carries no --session-id/--resume
    in its argv at all.

    Algorithm:
    1. Get pane_root_pid(target); if None, return None.
    2. Get process tree (injectable via ps_out for tests).
    3. Build a ppid->children map, walk BFS/DFS from the pane's root pid to collect every
       descendant pid (including the root itself).
    4. Among those descendants' command lines, apply the same session-id resolution that
       live_sid uses.

    This deliberately does NOT match on a `claude` binary name filter — just walk the whole
    pid subtree and apply the session-id-argv logic to every line in it; a subtree with no
    --session-id/--resume anywhere simply yields None.

    ps_out is injectable for tests — when provided, it should be the output of
    `ps -A -o pid=,ppid=,command=` (same format as _ps_tree() returns, but as raw text
    for backward compatibility with test injection).
    """
    root_pid = pane_root_pid(target)
    if root_pid is None:
        return None

    # Get process tree
    if ps_out is not None:
        # For test injection: accept raw ps text in the same format as _ps_tree output
        # (pid ppid command per line)
        procs = _ps_tree_from_text(ps_out)
    else:
        procs = _ps_tree()

    if not procs:
        return None

    # Build ppid -> children map
    children: dict[str, list[str]] = {}
    pid_to_cmd: dict[str, str] = {}
    for pid, ppid, cmd in procs:
        children.setdefault(ppid, []).append(pid)
        pid_to_cmd[pid] = cmd

    # Walk BFS/DFS from root_pid to collect all descendant pids
    descendant_pids: set[str] = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        if current in descendant_pids:
            continue
        descendant_pids.add(current)
        for child in children.get(current, []):
            if child not in descendant_pids:
                stack.append(child)

    # Among descendants, collect session_ids and resume_targets
    session_ids: set[str] = set()
    resume_targets: set[str] = set()
    for pid in descendant_pids:
        cmd = pid_to_cmd.get(pid, "")
        for m in _SESSION_ID.finditer(cmd):
            session_ids.add(m.group(1).lower())
        for m in _RESUME.finditer(cmd):
            resume_targets.add(m.group(1).lower())

    if not session_ids and not resume_targets:
        return None

    # Use the same resolution logic as live_sid
    p = paths()
    return _resolve_sid(session_ids, resume_targets, p.projects)


def _ps_tree_from_text(text: str) -> list[tuple[str, str, str]]:
    """Parse ps output text (pid=,ppid=,command= format) into triples.

    Used for test injection where ps_out is provided as raw text."""
    out: list[tuple[str, str, str]] = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        parts = ln.split(None, 2)
        if len(parts) >= 3:
            out.append((parts[0].strip(), parts[1].strip(), parts[2]))
        elif len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip(), ""))
    return out


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
    # Keep the durable roster's live_sid in lockstep with the sid file: probe/sweep/warden
    # heal from the outside, and without this squad.json could lag the healed sid file after a
    # fork, so revive (which prefers squad) would resume the frozen parent. Only for a station
    # already on the roster; imported locally to avoid a cycle; never fatal.
    try:
        from . import squad
        if squad.get(agent, p) is not None:
            squad.set_live_sid(agent, live, p)
    except Exception:
        pass
    return live
