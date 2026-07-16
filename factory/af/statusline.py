"""The statusline — and the one field it exists to smuggle out.

Claude Code pipes a JSON blob to the statusline command on every render. We are not here for
the pretty line; we are here for ONE key:

    "rate_limits": { "five_hour": {"used_percentage": 16, "resets_at": 1783993800},
                     "seven_day": {"used_percentage": 48, "resets_at": 1784034000} }

`resets_at` is the exact unix epoch at which the account-wide subscription limit lifts. That
number is the whole reason the warden can WAIT instead of guess — and it is available only
INSIDE a live session (no CLI reports it). So every agent drops it on disk here, and the
external watcher — which spends no tokens and therefore survives the limit — reads it from
$AF_ROOT/.ai/$AF_SLUG/limits.json. Without it the warden knows WHO was cut off but not WHEN
the wall lifts, and a rescuer that guesses wakes the agent straight back into it.

Written by whichever agent rendered last; the limit is account-wide, so one file is the truth
for the whole machine. Cheap: a few writes per turn, no network, no model.

    python3 -m af.statusline      (stdin: the harness JSON; stdout: the line)

It must ALWAYS print something. An empty status line is a broken-looking TUI, and a crash
here surfaces as a mysterious blank bar rather than as an error anyone can trace.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def save_limits(rate_limits: dict, state: Path) -> None:
    """Write + atomic rename, so the watcher never reads a half-written file."""
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / ".limits.json.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"rate_limits": rate_limits, "seen": int(time.time())}, f)
    os.replace(tmp, state / "limits.json")


def render(d: dict, now: int | None = None, ident: dict[str, str] | None = None) -> str:
    now = int(time.time()) if now is None else now
    rl = d.get("rate_limits") or {}
    src = ident if ident is not None else os.environ
    agent = src.get("AF_AGENT") or "agent"
    role = src.get("AF_ROLE") or ""
    model = ((d.get("model") or {}).get("display_name")) or ""
    ctx = d.get("context_window") or {}
    used = ctx.get("used_tokens") or ctx.get("used") or 0
    fh = rl.get("five_hour") or {}
    pct = fh.get("used_percentage")
    resets = fh.get("resets_at")

    bits = [agent + (f" ({role})" if role else "")]
    if model:
        bits.append(model)
    if used:
        bits.append(f"{int(used) // 1000}k")
    if pct is not None:
        s = f"5h {int(pct)}%"
        if resets:
            left = max(0, int(resets) - now)
            s += f" · {left // 3600}h{(left % 3600) // 60:02d}m left"
        bits.append(s)
    return " | ".join(bits)


def _state(slug: str = "") -> Path:
    # AF_SLUG defaults to the literal "proj" here, exactly as statusline.sh does — never
    # derived from the cwd, or the limits file lands in a state dir the warden does not read.
    # An argv slug wins over the env's: after a fork onto a pooled process $AF_SLUG can name
    # another squad, and limits.json is what the warden reads to time the 5-hour rescue.
    # TODO(merge): same fallback as hooks._state_paths; belongs on Paths.from_env.
    from .paths import paths
    return paths(slug or os.environ.get("AF_SLUG") or "proj").state


def _ident(argv: list[str]) -> tuple[str, dict[str, str] | None]:
    """`<slug> <agent>` from the settings file, resolved against that agent's spec — the same
    fork-proof identity the hooks use, and for the same reason (af.hooks._bind_identity).
    Returns (slug, vars-or-None); None means "no args, fall back to the environment"."""
    if len(argv) < 2:
        return "", None
    slug, agent = argv[0], argv[1]
    try:
        from . import spec as specmod
        from .paths import paths
        sp = specmod.read(agent, paths(slug))
    except Exception:
        return slug, None
    d = {k: v for k, v in (sp.env or {}).items() if v}
    d["AF_AGENT"] = agent
    return slug, d


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    slug, ident = _ident(argv)
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    line = ""
    try:
        d = json.loads(raw) if raw.strip() else {}
        if not isinstance(d, dict):
            d = {}
        rl = d.get("rate_limits") or {}
        if rl:
            try:
                save_limits(rl, _state(slug))
            except Exception:
                pass          # the drop is best-effort; the LINE is not optional
        line = render(d, ident=ident)
    except Exception:
        line = ""
    sys.stdout.write(line or "agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
