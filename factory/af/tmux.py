"""The only module in `af` that shells out to tmux.

Every pane read and every keystroke in the factory goes through here, so there is one
place to audit "what can write into a live agent's TUI" — and one place that knows a
missing session is a normal answer (None / False), not an exception.
"""

from __future__ import annotations

import subprocess


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """No tmux on PATH is "the agent is not there", not a traceback. The warden runs
    detached with a stripped env, where a missing tmux is a live failure mode — and a
    probe that dies there reports nothing at all instead of reporting the agent down."""
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=False,
        )
    except (FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr=str(e))


def has_session(target: str) -> bool:
    return _run(["has-session", "-t", target]).returncode == 0


def capture_pane(target: str) -> str | None:
    """The pane's visible screen, or None if there is no such session."""
    p = _run(["capture-pane", "-t", target, "-p"])
    if p.returncode != 0:
        return None
    return p.stdout


def send_keys(target: str, *keys: str, literal: bool = False) -> bool:
    """Send keys to a pane. literal=True types the text instead of interpreting it.

    Callers must decide whether the pane is safe to type into (a permission prompt is a
    SELECT — see patterns.PERMISSION); this function does not, because `approve` needs
    to type into exactly that.
    """
    args = ["send-keys", "-t", target]
    if literal:
        args.append("-l")
    args.extend(keys)
    return _run(args).returncode == 0


def send_enter(target: str) -> bool:
    return send_keys(target, "Enter")


def new_session(name: str, command: str, cwd: str, width: int = 220, height: int = 50) -> bool:
    return _run(
        ["new-session", "-d", "-s", name, "-x", str(width), "-y", str(height), "-c", cwd, command]
    ).returncode == 0


def kill_session(name: str) -> bool:
    return _run(["kill-session", "-t", name]).returncode == 0


def display(fmt: str) -> str:
    """Ask tmux about the pane THIS process is running in (register-self needs it)."""
    p = _run(["display", "-p", fmt])
    return p.stdout.strip() if p.returncode == 0 else ""


def list_sessions() -> list[str]:
    p = _run(["ls", "-F", "#{session_name}"])
    if p.returncode != 0:
        return []
    return [s for s in p.stdout.splitlines() if s]


def list_sessions_verbose() -> list[str]:
    """`tmux ls` as a human reads it — "ai-slug-name: 1 windows (created …)". `ai list` prints
    exactly this, so `af list` must too, or the same command in two implementations shows a
    human two different things."""
    p = _run(["ls"])
    if p.returncode != 0:
        return []
    return [s for s in p.stdout.splitlines() if s]
