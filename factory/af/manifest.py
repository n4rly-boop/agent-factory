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

import argparse
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

from .nums import intish
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
    p = p or paths()
    target = f"{sid}.jsonl"
    for root, _dirs, files in os.walk(p.projects):
        if target in files:
            return Path(root) / target
    return None


# --- prune ------------------------------------------------------------------------------
def _existing_sids(p: Paths) -> set[str]:
    """One walk of ~/.claude/projects → the set of session ids whose transcript still exists.
    Built once so prune does not os.walk once PER row (session_log_exists does that)."""
    sids: set[str] = set()
    for _root, _dirs, files in os.walk(p.projects):
        for f in files:
            if f.endswith(".jsonl"):
                sids.add(f[:-len(".jsonl")])
    return sids


def _write_atomic(p: Paths, rows_: list[list[str]]) -> None:
    """Replace the manifest in one os.replace, never a truncate-then-write. See prune's note
    on the append race — atomic replace keeps a concurrent reader from ever seeing half a file."""
    p.manifest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.manifest.parent), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in rows_:
                fh.write("\t".join(r) + "\n")
        os.replace(tmp, str(p.manifest))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prune(p: Paths | None = None, dry_run: bool = False) -> tuple[list[list[str]], list[list[str]]]:
    """Collapse the append-only manifest to reality and return (kept, dropped).

    The manifest grows one row per spawn forever; `last_sid`/`spawned_here` already read
    last-wins, so the staleness is invisible until you `revive` a name whose transcript was
    purged months ago. Prune makes the file say what it means:
      * one row per (tool, name, cwd) — the most recent, since a newer sid supersedes an older,
      * and only for sessions whose transcript STILL EXISTS. A row whose .jsonl is gone is
        un-revivable dead weight (revive already refuses it) — the whole point is to drop it.

    A row is NEVER dropped while its transcript survives, so nothing revivable is lost.

    DESTRUCTIVE REWRITE of a file the bash half appends to with O_APPEND. Run it when the line
    is quiet: a spawn that appends between our read and our atomic replace loses its row (rare,
    and the next spawn re-adds the name). `dry_run` reports without touching the file.
    """
    p = p or paths()
    all_rows = rows(p)
    have = _existing_sids(p)
    by_key: dict[tuple[str, str, str], list[list[str]]] = {}
    for ts, tool, name, sid, cwd in all_rows:
        by_key.setdefault((tool, name, cwd), []).append([ts, tool, name, sid, cwd])
    kept, dropped = [], []
    for group in by_key.values():
        group.sort(key=lambda r: intish(r[0], 0))
        # Keep the latest row whose transcript STILL EXISTS — not the latest row then tested,
        # which would drop a whole key when a dead respawn (a row with no .jsonl) shadows a
        # live older session. A row is never dropped while its transcript survives.
        live = [r for r in group if r[3] and r[3] in have]
        if live:
            kept.append(live[-1])
        else:
            dropped.append(group[-1])   # nothing revivable for this key; report the newest
    kept.sort(key=lambda r: intish(r[0], 0))
    if not dry_run and (dropped or len(kept) != len(all_rows)):
        _write_atomic(p, kept)
    return kept, dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="af.manifest", description="the spawn manifest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prune", help="collapse to one live row per agent; drop dead sessions")
    pp.add_argument("--dry-run", action="store_true", help="report what would change, touch nothing")
    a = ap.parse_args(argv)
    p = paths()
    if a.cmd == "prune":
        kept, dropped = prune(p, dry_run=a.dry_run)
        verb = "would drop" if a.dry_run else "dropped"
        print(f"[manifest] {len(kept)} kept, {verb} {len(dropped)}")
        for ts, tool, name, sid, cwd in dropped:
            print(f"  - {name} ({tool}) {sid[:8] or '—'} {cwd}  [transcript gone]")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
