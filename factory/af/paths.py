"""Every path in the factory, derived in one place.

No other module in `af` builds a path by string concatenation. The bash system and
this package write the SAME files during the migration, so a path that drifts by one
character is not a typo — it is an agent whose mail nobody reads and whose spec
nobody finds. Both sides derive from the same three env vars (AF_ROOT, AF_SLUG,
AF_SPECROOT); this file is the Python half of that contract.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = "/tmp/agent-factory"
SPEC_HOME = Path.home() / ".claude" / "agent-factory"
PROJECTS = Path.home() / ".claude" / "projects"

# The bash half of the factory, which is still live and which the AGENTS themselves run:
# every spawned agent gets AF_MAIL=<mail.sh> in its env, and the doorbell it is told to
# type is `!bash $AF_MAIL read`. That must keep pointing at the SHELL script — an agent
# spawned by Python is a plain `claude` with no PYTHONPATH, so a python -m af doorbell
# would die in its pane.
FACTORY_DIR = Path(__file__).resolve().parents[1]
MAIL_SH = FACTORY_DIR / "mail.sh"
POLL_SH = FACTORY_DIR / "polling.sh"

_SLUG_STRIP = re.compile(r"[^a-z0-9]")


def slugify(name: str) -> str:
    """ai.sh: basename $CWD | tr A-Z a-z | sed 's/[^a-z0-9]//g' | cut -c1-12."""
    s = _SLUG_STRIP.sub("", name.lower())[:12]
    return s or "proj"


@dataclass(frozen=True)
class Paths:
    """A resolved view of the factory's filesystem for one slug."""

    slug: str
    root: Path
    cwd: Path
    mailroot: Path
    specroot: Path

    @classmethod
    def from_env(cls, slug: str | None = None) -> "Paths":
        cwd = Path(os.environ.get("AF_CWD") or os.getcwd())
        slug = slug or os.environ.get("AF_SLUG") or slugify(cwd.name)
        root = Path(os.environ.get("AF_ROOT") or DEFAULT_ROOT)
        state = root / ".ai" / slug
        # AF_MAILROOT is honoured because mail.sh honours it: an agent's env carries it,
        # and a Python reader that ignored it would read a different mailbox than the
        # bash writer in the same session.
        mailroot = Path(os.environ.get("AF_MAILROOT") or (state / "mail"))
        specroot = Path(os.environ.get("AF_SPECROOT") or (SPEC_HOME / "lines"))
        return cls(slug=slug, root=root, cwd=cwd, mailroot=mailroot, specroot=specroot)

    # --- state -------------------------------------------------------------
    @property
    def state(self) -> Path:
        return self.root / ".ai" / self.slug

    def session(self, agent: str) -> str:
        return f"ai-{self.slug}-{agent}"

    def sid_file(self, agent: str) -> Path:
        return self.state / f"sid-{agent}"

    def log_cache(self, agent: str) -> Path:
        return self.state / f"log-{agent}"

    def compacted(self, agent: str) -> Path:
        return self.state / f"compacted-{agent}"

    def dead_win_state(self, agent: str) -> list[Path]:
        """Leftovers from the era when an agent could own a Terminal.app window. Removed on
        down/up so they cannot outlive the code that understood them; log-<agent> is the
        session-log path cache, which MUST die with the session id it was resolved for."""
        return [self.state / f"win-{agent}", self.state / f"tty-{agent}",
                self.log_cache(agent)]

    def limited(self, agent: str) -> Path:
        return self.state / f"limited-{agent}"

    def self_lines(self, agent: str) -> Path:
        # Cumulative "lines self-written outside work/ since last real delegation" —
        # advisory-only counter for delegate_wall()'s `advised` level. Deliberately a plain
        # per-agent file, not a roster.Station field: postmaster holds a long-lived, stale
        # copy of Station in memory and round-trips it every tick, silently dropping any
        # field a hook writes until the daemon restarts. A hook-only file has no daemon in
        # its write path at all.
        return self.state / f"self-lines-{agent}"

    @property
    def limits_json(self) -> Path:
        return self.state / "limits.json"

    @property
    def sweep_lock(self) -> Path:
        return self.state / "sweep.lock"

    @property
    def polldir(self) -> Path:
        return self.state / "poll"

    # --- mail --------------------------------------------------------------
    def box(self, agent: str) -> Path:
        return self.mailroot / f"{agent}.jsonl"

    def cursor(self, agent: str) -> Path:
        return self.mailroot / f"{agent}.cursor"

    def cap(self, agent: str) -> Path:
        return self.mailroot / f"cap-{agent}"

    def pane(self, agent: str) -> Path:
        return self.mailroot / f"pane-{agent}"

    def task_flag(self, agent: str) -> Path:
        return self.mailroot / f"state-{agent}"

    def tasker(self, agent: str) -> Path:
        return self.mailroot / f"tasker-{agent}"

    def ring_pending(self, agent: str) -> Path:
        """Set when a doorbell has been QUEUED into a busy agent, cleared when the agent next
        reads. While it exists, further sends to that busy agent skip the doorbell: the one
        already queued drains ALL unread on the turn boundary, so extra `!…read` keystrokes
        would only fire extra empty model turns."""
        return self.mailroot / f"ring-{agent}"

    @property
    def blobdir(self) -> Path:
        return self.mailroot / "blob"

    def blob(self, msg_id: str) -> Path:
        return self.blobdir / f"{msg_id}.txt"

    def mail_lock(self, agent: str) -> Path:
        return self.mailroot / f".lock-{agent}"

    def boxes(self) -> list[str]:
        if not self.mailroot.is_dir():
            return []
        return sorted(p.stem for p in self.mailroot.glob("*.jsonl"))

    # --- specs -------------------------------------------------------------
    @property
    def specdir(self) -> Path:
        return self.specroot / self.slug

    def spec_file(self, agent: str) -> Path:
        # agent-<name>.json, never <name>.json: an agent named `squad` would otherwise
        # collide with the team-level squad.json.
        return self.specdir / f"agent-{agent}.json"

    def settings_file(self, agent: str) -> Path:
        return self.specdir / f"settings-{agent}.json"

    @property
    def squad_file(self) -> Path:
        """The one mutable source of truth for a team — durable (under the spec home, NOT
        /tmp, so a purge cannot erase the roster) and flock-guarded. It is the sole record of
        which blueprint a team came from, when it was created, and who is on it (replacing the
        scattered sid-/state- files and a separate line.json that used to duplicate the same
        two facts). See af.roster."""
        return self.specdir / "squad.json"

    @property
    def squad_lock(self) -> Path:
        return self.specdir / ".squad.lock"

    @property
    def durable_state(self) -> Path:
        """A per-slug state dir that survives a /tmp purge — for the warden pidfile and other
        handles that must not vanish under a running daemon (the /tmp-purge -> two-wardens
        window). Distinct from `state`, which stays in AF_ROOT for the bash-era files."""
        return SPEC_HOME / "state" / self.slug

    @property
    def manifest(self) -> Path:
        return SPEC_HOME / "manifest.tsv"

    @property
    def projects(self) -> Path:
        return PROJECTS


def paths(slug: str | None = None) -> Paths:
    return Paths.from_env(slug)
