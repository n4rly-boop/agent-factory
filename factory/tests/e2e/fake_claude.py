#!/usr/bin/env python3
"""A FAKE `claude` binary for the hermetic e2e harness — no real Claude, no tokens, no auth.

It exists to make `af` believe a real interactive `claude` TUI is running in a tmux pane:

  * It carries the same argv shape a real launch does, so `af.live.live_sid` (which reads the
    live session out of `ps`) resolves the agent — the `--settings .../lines/<slug>/settings-<name>.json`
    token is the identity key, and a `--session-id` that is not a `--resume` target is the live sid.
  * It reproduces Claude Code's fork-on-resume: `claude --resume <parent>` (without --session-id
    and without --fork-session) re-launches itself as `--session-id <fork> --fork-session --resume
    <parent> …`, so after a revive `ps` shows the forked argv and live_sid returns the FORK.
  * It writes a transcript jsonl under $HOME/.claude/projects/af-e2e/<live>.jsonl in the exact
    shape af.probe._scan_log reads (ctx from the last usage record, endturns from stop_reason).
  * It prints something to stdout so capture-pane is non-empty, then stays alive (a SIGTERM-quit
    sleep loop) so the tmux session and its argv persist until `af down` / kill-server.

Env knobs: FAKE_CTX (default 1000) input_tokens on each usage record; FAKE_ENDTURNS (default 1)
number of end_turn assistant records; FAKE_CTXPCT (optional) emits a `Context: N%` statusline.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _extract_uuid(val):
    """--resume takes a bare uuid OR a path ending in <uuid>.jsonl. Grab the uuid either way."""
    if not val:
        return None
    m = _UUID.search(val)
    return m.group(0).lower() if m else None


def _parse(argv):
    """Values may be `--flag val` or `--flag=val`. We only care about the identity flags; every
    other flag (and its value, for the value-taking ones) is consumed and ignored."""
    vals = {"session_id": None, "resume": None, "fork": False}
    value_flags = {"--settings", "--append-system-prompt", "--model", "--permission-mode"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if "=" in a and a.startswith("--"):
            key, inline = a.split("=", 1)
        else:
            key, inline = a, None

        def take():
            nonlocal i
            if inline is not None:
                return inline
            i += 1
            return argv[i] if i < len(argv) else None

        if key == "--session-id":
            vals["session_id"] = (take() or "").lower() or None
        elif key == "--resume":
            vals["resume"] = _extract_uuid(take())
        elif key == "--fork-session":
            vals["fork"] = True
        elif key in value_flags:
            take()  # consume + ignore the value
        else:
            pass  # --dangerously-skip-permissions and any other bare flag: ignore
        i += 1
    return vals


def main():
    orig = sys.argv[1:]
    vals = _parse(orig)

    if vals["session_id"]:
        live = vals["session_id"]
    elif vals["resume"] and not vals["fork"]:
        # Claude Code forks the session on --resume: mint a new id and re-exec THIS script as a
        # fork worker, carrying the original argv (which still holds --resume <parent> and
        # --settings …). After the re-exec the --session-id branch sets live = fork.
        fork = str(uuid.uuid4()).lower()
        newargv = [sys.executable, os.path.abspath(__file__),
                   "--session-id", fork, "--fork-session"] + orig
        os.execv(sys.executable, newargv)
        return  # unreachable
    else:
        live = str(uuid.uuid4()).lower()

    home = os.environ.get("HOME") or str(Path.home())
    projdir = Path(home) / ".claude" / "projects" / "af-e2e"
    projdir.mkdir(parents=True, exist_ok=True)
    transcript = projdir / f"{live}.jsonl"

    ctx = int(os.environ.get("FAKE_CTX", "1000"))
    endturns = int(os.environ.get("FAKE_ENDTURNS", "1"))
    with transcript.open("a", encoding="utf-8") as fh:
        for _ in range(endturns):
            rec = {
                "type": "assistant",
                "message": {
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": ctx,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
            fh.write(json.dumps(rec) + "\n")
        fh.flush()

    print(f"fake-claude live={live} resume={vals['resume'] or 'none'} fork={vals['fork']}")
    pct = os.environ.get("FAKE_CTXPCT")
    if pct:
        print(f"Context: {pct}%")
    # A column-0 input box line: patterns.INPUT_BOX anchors on "❯ " / "! " at column 0.
    print("❯ ")
    sys.stdout.flush()

    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))
    try:
        signal.signal(signal.SIGINT, lambda *_a: sys.exit(0))
    except (ValueError, OSError):
        pass
    # Stay alive: keeps argv visible to ps (for live_sid) and keeps the tmux session up.
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
