"""One look at an agent, answering every question the system asks about it.

The bash system scrapes an agent's state in nine places (_busy, _permission, _limited,
_ctx, _endturns, the sweep's copies, the warden's belt, mail.sh's own _permission), each
with its own capture-pane and its own transcript read. They race each other and they
disagree — one of them is what compacted an agent that was mid-turn.

`probe()` is ONE capture-pane and ONE pass over the session jsonl, returning a frozen
fact. Whatever acts on it acts on a single consistent observation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import patterns, tmux
from .paths import Paths, paths

Phase = Literal["generating", "permission", "limited", "idle"]


@dataclass(frozen=True)
class Probe:
    alive: bool
    phase: Phase
    ctx: int | None
    endturns: int | None
    inputbox: str | None


def session_log(agent: str, p: Paths | None = None) -> Path | None:
    """Resolve the agent's session .jsonl from the sid recorded at spawn.

    The find is a full walk of ~/.claude/projects (hundreds of MB of transcripts), so the
    answer is cached — the path never moves once claude has created the log. The cache is
    self-validating: it is only believed if it still names THIS sid and still exists, so a
    respawn under the same agent name cannot be served a stale log.
    """
    p = p or paths()
    try:
        sid = p.sid_file(agent).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not sid:
        return None

    cache = p.log_cache(agent)
    try:
        cached = Path(cache.read_text(encoding="utf-8").strip())
        if cached.name == f"{sid}.jsonl" and cached.is_file():
            return cached
    except (OSError, ValueError):
        pass

    for root, _dirs, files in os.walk(p.projects):
        if f"{sid}.jsonl" in files:
            found = Path(root) / f"{sid}.jsonl"
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(str(found), encoding="utf-8")
            except OSError:
                pass
            return found
    return None


def _scan_log(f: Path) -> tuple[int | None, int]:
    """(ctx, endturns) from one streaming pass over the transcript.

    ctx  = input + cache_read + cache_creation of the LAST assistant record that carries
           usage — i.e. the whole prompt the model last saw. Output tokens are not part of
           the next prompt; the cached buckets are.
    endturns = assistant records with stop_reason == "end_turn".

    Parsed with json.loads under try/except, not grepped. bash counted end_turns with
    `grep -c` explicitly because it feared a half-written final line — which is a real
    hazard (the file is being appended to as we read it) but grep is not the fix for it:
    a try/except drops exactly the torn line and nothing else.
    """
    ctx: int | None = None
    endturns = 0
    try:
        with f.open("rb") as fh:
            for raw in fh:
                # Only assistant records carry usage or a stop_reason; skipping the rest
                # unparsed is what keeps this cheap on a 40MB transcript.
                if b'"assistant"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                if msg.get("stop_reason") == "end_turn":
                    endturns += 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    ctx = (
                        int(usage.get("input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0)
                        + int(usage.get("cache_creation_input_tokens") or 0)
                    )
    except OSError:
        return None, 0
    return ctx, endturns


def _phase(pane: str) -> Phase:
    # Order matters. A permission prompt and a generation timer are LIVE state — they are
    # painted by the thing happening right now. The usage-limit prose is not: it stays in
    # the scrollback long after the window has reset, so it may only be believed when
    # nothing live contradicts it.
    if patterns.PERMISSION.search(pane):
        return "permission"
    if patterns.GENERATING.search(pane):
        return "generating"
    if patterns.USAGE_LIMIT.search(pane):
        return "limited"
    return "idle"


def probe(agent: str, p: Paths | None = None) -> Probe:
    p = p or paths()
    pane = tmux.capture_pane(p.session(agent))
    log = session_log(agent, p)
    # endturns is a COUNT: its zero is 0, not None. Every caller compares it with an
    # integer (`> base` to decide a turn landed), and a fresh agent whose log does not
    # exist yet is the common case, not the exotic one.
    ctx, endturns = _scan_log(log) if log else (None, 0)

    if pane is None:
        # A down agent still has a transcript, and its size is exactly what `ledger` and
        # `revive` want to know before bringing it back.
        return Probe(alive=False, phase="idle", ctx=ctx, endturns=endturns, inputbox=None)

    phase = _phase(pane)
    # A modal (permission prompt, resume chooser) REPLACES the input box, but the prompts
    # the human already submitted stay in the scrollback at column 0 — so a box parser run
    # against a modal pane happily returns a prompt from ten turns ago as if it were live.
    # There is no live box to read in those phases; say so.
    box = patterns.input_box(pane) if phase in ("idle", "generating") else None

    return Probe(alive=True, phase=phase, ctx=ctx, endturns=endturns, inputbox=box)
