"""One place to turn a string into an int, because `str.isdigit()` is not `[0-9]`.

'²'.isdigit() is True and int('²') raises ValueError — so `int(v) if v.isdigit() else d`,
written a dozen times across the config parsers, has a dozen places where a Unicode digit in
an env var takes the whole process down (the warden is a *rescuer*: a crash there is silent).
mailbox._read_cursor documents the same trap and fixes it with a regex; this is that fix,
factored out.
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"[0-9]+")


def intish(s: object, default: int | None = None, *, positive: bool = False) -> int | None:
    """`s` as a non-negative int, else `default`. `positive=True` also rejects 0.

    Only `[0-9]+` counts — no sign, no whitespace-in-the-middle, no Unicode digits — so a
    junk value falls back rather than either crashing or being misread."""
    if s is None:
        return default
    t = str(s).strip()
    if not _DIGITS.fullmatch(t):
        return default
    n = int(t)
    if positive and n <= 0:
        return default
    return n
