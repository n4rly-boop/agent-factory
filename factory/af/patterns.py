"""Every regex in the system, written once.

The bash system carries six of these across four files, and they have already
diverged: warden.sh's usage-limit belt learned two wordings ("limit reached … resets",
"limit will reset") and matches case-insensitively, while ai.sh's — the one that stops
`sweep` from typing /compact into an agent that has run out of quota — never did. An
agent hitting the newer wording was therefore invisible to sweep and got /compact
re-sent every tick, forever. The union below is deliberate: a scraper that under-matches
fails silently and keeps acting, which is the worse half of the trade.
"""

from __future__ import annotations

import re

# The generation timer the TUI paints while a turn is in flight: "(4s · ↑ 1.2k tokens…)".
# Its presence is the ONLY honest "this agent is working right now" signal — the session
# jsonl says nothing until the turn lands. A turn past 60s rolls the timer to "(1m 5s · …)"
# and a compaction paints "Coalescing… (7m 12s · …)" — both are STILL working, so the
# hours/minutes prefix is optional. Matching only "(\d+s" read every long turn as idle,
# which silently disarmed the doorbell dedup for exactly the busy agents it protects.
GENERATING = re.compile(r"\((?:\d+h )?(?:\d+m )?\d+s · ")

# A tool-permission pause. This prompt is a SELECT, not an input box: text typed here
# lands in the selector and the Enter that follows confirms the highlighted default.
# Anything that writes to a pane must check this first.
PERMISSION = re.compile(r"Do you want to proceed\?|❯ 1\. Yes")

# The account-wide usage limit, as Claude Code actually prints it (2.1.x). UNION of
# ai.sh:526 and warden.sh:74 — see the module docstring. Case-insensitive because the
# warden's copy was, and the wordings are prose.
USAGE_LIMIT = re.compile(
    r"hit your (?:session|usage) limit"
    r"|usage limit reached"
    r"|limit reached .*resets"
    r"|limit will reset",
    re.IGNORECASE,
)

# The chooser `claude --resume` pauses on for a large session:
#   ❯ 1. Resume from summary   2. Resume full session as-is   3. Don't ask again
RESUME_CHOOSER = re.compile(r"Resume full session as-is|Resume from summary")

# Claude Code's OWN context-fullness readout, painted into the statusline: "Context: 12%".
# It is the ground truth the transcript estimate cannot match: after a /clear or /compact,
# CC resets this to ~0 immediately, but the transcript's last usage record still shows the
# pre-clear size until the next real turn writes a new one. When the pane says the context is
# near-empty, no /compact can do anything (CC answers "Not enough messages to compact") —
# believing the fat transcript there is what makes the warden re-send /compact every tick.
CONTEXT_PCT = re.compile(r"Context:\s*(\d+)%")


def context_pct(pane: str) -> int | None:
    """The LAST Context% the statusline painted (the freshest render), or None if the
    statusline is not in the captured window."""
    hits = CONTEXT_PCT.findall(pane)
    if not hits:
        return None
    try:
        return int(hits[-1])
    except ValueError:
        return None

# The live input box. THREE hand-rolled parsers of this existed (ai.sh:446, mail.sh:121,
# mail.sh:291) with three different anchorings; this is the one they were all reaching for.
#
# Anchored at COLUMN ZERO, which is where the TUI renders the box — in both modes: "❯ " in
# normal mode, "! " in shell mode (the space after the bang is real; an exact-match check
# written without it silently never fires). The two bash parsers that allowed leading
# whitespace pick up the footer hint "  ! for shell mode" instead of the box, and mail.sh's
# `tail -1` lands on it: in shell mode its "did the Enter get eaten?" check compares against
# that hint, never matches, and therefore always reports success. Verified on the live line
# — every one of seven agents renders its box at column 0, and the hint is the only
# indented prompt-like line in any of them.
#
# A submitted prompt also stays in the scrollback as "❯ text", so only the LAST match is the
# live box (see input_box below). \s, not [ \t]: an empty box renders as "❯ \xa0", and the
# BSD sed the bash used strips that too.
INPUT_BOX = re.compile(r"^[❯!]\s*(?P<text>.*)$")

# --- launch flags ---------------------------------------------------------------
# The spec regex-extracts these back OUT of the flags string; see spec.py.
FLAG_MODEL = re.compile(r"--model\s+(\S+)")
FLAG_SETTINGS = re.compile(r"--settings\s+(\S+)")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Eats the whitespace PRECEDING the flag, so removing it leaves no double space behind.
# Bytes, not str: the flags carry a %q-quoted system prompt that may not decode as UTF-8,
# and a decode error here would blank the flags — i.e. an agent that revives with no role.
STRIP_SID = re.compile(rb"\s*--(?:session-id|resume)\s+[0-9a-fA-F-]{36}")


def input_box(pane: str) -> str | None:
    """What is sitting in the agent's live input box, or None if there is no box.

    The last prompt-anchored line in the capture wins: earlier ones are the transcript
    of prompts already submitted, and searching the whole pane matches those forever.
    """
    text: str | None = None
    for line in pane.splitlines():
        m = INPUT_BOX.match(line)
        if m:
            text = m.group("text")
    return text
