"""Shared scaffolding: a hermetic factory rooted in a temp dir.

NOTHING in this suite may touch the real /tmp/agent-factory, the real
~/.claude/agent-factory or a live tmux session (other than a read-only capture-pane,
and even that is frozen into fixtures/ rather than run at test time). Every test that
touches state goes through TempFactory, which overrides AF_ROOT / AF_SLUG / AF_SPECROOT
in os.environ for the life of the test and restores them after.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

FACTORY = Path(__file__).resolve().parents[1]      # …/factory, which contains the af package
if str(FACTORY) not in sys.path:
    # `python3 -m unittest discover -s factory/tests` puts the TESTS dir on sys.path, not
    # factory/. Importing support first is what makes `import af` work; every test module
    # therefore imports it before it imports af.
    sys.path.insert(0, str(FACTORY))

from af import paths as af_paths  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The bash half of the system, for the interop tests.
MAIL_SH = FACTORY / "mail.sh"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TempFactory(unittest.TestCase):
    """A TestCase whose AF_ROOT is a fresh temp dir, torn down afterwards."""

    slug = "aftest"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="af-test-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self._saved = {k: os.environ.get(k) for k in
                       ("AF_ROOT", "AF_SLUG", "AF_MAILROOT", "AF_SPECROOT", "AF_AGENT", "AF_CWD")}
        self.addCleanup(self._restore)

        os.environ["AF_ROOT"] = str(self.root)
        os.environ["AF_SLUG"] = self.slug
        os.environ["AF_SPECROOT"] = str(self.root / "specs")
        os.environ.pop("AF_MAILROOT", None)
        os.environ.pop("AF_AGENT", None)
        os.environ["AF_CWD"] = str(self.root)

        self.p = af_paths.paths()
        self.p.mailroot.mkdir(parents=True, exist_ok=True)

        # Belt: if any of this leaked out of the temp dir the whole suite is unsafe.
        self.assertTrue(str(self.p.mailroot).startswith(str(self.root)))
        self.assertTrue(str(self.p.state).startswith(str(self.root)))

    def _restore(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def bash_env(self) -> dict:
        """The env a bash mail.sh subprocess must see to share our temp factory.

        TMUX is popped and TMUX_TMPDIR is aimed at the temp dir, so mail.sh's `ring` cannot
        reach the real tmux server AT ALL: `has-session` against an empty socket dir fails
        (without starting a server), `_alive` returns false, and every send-keys in mail.sh
        sits below that guard. Without this, the only thing keeping the suite off a live
        pane would be the absence of a session called `ai-aftest-<agent>` — a coincidence,
        not a guarantee.
        """
        env = dict(os.environ)
        env["AF_ROOT"] = str(self.root)
        env["AF_SLUG"] = self.slug
        env["AF_MAILROOT"] = str(self.p.mailroot)
        env.pop("TMUX", None)
        env["TMUX_TMPDIR"] = str(self.root / "tmux")
        (self.root / "tmux").mkdir(exist_ok=True)
        return env
