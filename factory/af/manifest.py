"""The manifest: one TSV line per spawned agent, in $HOME so it survives a /tmp wipe.

    epoch \t tool \t name \t session_id \t cwd

afctl.sh reads this file to find (and purge) the session logs the factory created, and
ai.sh's `revivable` scans it with `awk -F'\\t' '$2=="ai" && $5==cwd'`. So the TOOL COLUMN
STAYS "ai" even when Python writes the row: an agent spawned by `python3 -m af up` must
still be visible to `bash ai.sh revivable`, and the column names the FAMILY of agent
(ai-spawned, as opposed to a headless af.sh run), not which binary happened to type it.

Append-only, one line per spawn: a write this small to an O_APPEND fd cannot interleave
with the bash writer's.
"""

from __future__ import annotations

import time
from pathlib import Path

from .paths import Paths, paths


def append(name: str, sid: str, cwd: str | None = None, tool: str = "ai",
           p: Paths | None = None) -> None:
    p = p or paths()
    cwd = cwd if cwd is not None else str(p.cwd)
    f = p.manifest
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"{int(time.time())}\t{tool}\t{name}\t{sid}\t{cwd}\n")


def rows(p: Paths | None = None) -> list[list[str]]:
    p = p or paths()
    try:
        raw = p.manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        cols = line.split("\t")
        if len(cols) >= 5:
            out.append(cols[:5])
    return out


def last_sid(name: str, p: Paths | None = None) -> str:
    """The most recent session id recorded for this NAME — across every project.

    Name-only, because that is all the manifest is keyed on. `revive` therefore refuses to
    act on this answer alone unless a spec confirms the identity: run `revive orc` from the
    wrong directory and it would otherwise resurrect the real orc's memory into a
    differently-slugged session with no role, no wall and a mailbox nobody reads.
    """
    sid = ""
    for _ts, tool, n, s, _cwd in rows(p):
        if tool == "ai" and n == name:
            sid = s
    return sid


def spawned_here(cwd: str, p: Paths | None = None) -> dict[str, str]:
    """{name: latest sid} for agents spawned from THIS cwd — same cwd, same slug, so S()
    resolves to the session name they actually run under."""
    seen: dict[str, str] = {}
    for _ts, tool, name, sid, c in rows(p):
        if tool == "ai" and c == cwd and sid:
            seen[name] = sid
    return seen


def session_log_exists(sid: str, p: Paths | None = None) -> Path | None:
    """Does the transcript for this session id still exist? (`down` keeps it; only
    `afctl purge` deletes it — so revive works as long as this says yes.)"""
    import os
    p = p or paths()
    target = f"{sid}.jsonl"
    for root, _dirs, files in os.walk(p.projects):
        if target in files:
            return Path(root) / target
    return None
