"""live_sid — the session an agent is ACTUALLY on, read out of the process table.

The whole module is pure parsing behind the injectable `ps_out`: no test here runs a real
`ps` or touches ~/.claude/projects. The cases are the three shapes a claude process takes
in argv — a launcher that never forked, a fork worker, a fresh spawn — plus the two ways the
match must NOT bleed: across lines that share an agent name (slug isolation) and across
agents whose names are prefixes of one another (settings-eval must not answer to "ev").

The one token that names both slug and agent unambiguously is the --settings path, so every
line carries a `…/lines/<slug>/settings-<agent>.json`. The live session is the --session-id
that is nobody's --resume target; a --session-id that IS a resume target is the frozen parent
and must never be returned.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import FACTORY   # noqa: F401 — imported first: puts the af package on sys.path

from af import live
from af.paths import Paths

FORK = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PARENT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _settings(slug: str, agent: str) -> str:
    """The --settings path a spawned agent carries — the only token live_sid matches on."""
    return f"/Users/x/.claude/agent-factory/lines/{slug}/settings-{agent}.json"


class LiveSid(unittest.TestCase):
    def setUp(self) -> None:
        # _newest walks p.projects (~/.claude/projects) to break ties. Point it at an empty
        # tmp dir so a single-candidate set falls back to its lone id WITHOUT touching the
        # real home, and no stray on-disk transcript can perturb a result.
        self._proj = tempfile.TemporaryDirectory(prefix="af-live-proj-")
        self.addCleanup(self._proj.cleanup)
        patcher = mock.patch("af.paths.PROJECTS", Path(self._proj.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def paths(self, slug: str) -> Paths:
        # live_sid only reads p.slug and p.projects; the rest can be dummy.
        d = Path(self._proj.name)
        return Paths(slug=slug, root=d, cwd=d, mailroot=d, specroot=d)

    def sid(self, agent: str, slug: str, ps_out: str) -> str | None:
        return live.live_sid(agent, self.paths(slug), ps_out=ps_out)

    # --- the three argv shapes ---------------------------------------------
    def test_a_forked_session_returns_the_fork_not_the_frozen_parent(self):
        # The fork's --session-id is nobody's --resume target; the parent's IS. Returning the
        # parent is the "warden compacts an agent already at 0%" bug — it reads the frozen
        # transcript while the live fork grows.
        ps = "\n".join([
            f"claude --session-id {FORK} --fork-session --resume /p/{PARENT}.jsonl "
            f"--settings {_settings('s', 'orc')}",
            f"claude --session-id {PARENT} --settings {_settings('s', 'orc')}",
        ])
        self.assertEqual(self.sid("orc", "s", ps), FORK)

    def test_a_launcher_that_never_forked_returns_its_resume_target(self):
        # No non-resume --session-id exists, so the resume target IS the live session.
        ps = f"claude --resume {SID} --settings {_settings('s', 'orc')}"
        self.assertEqual(self.sid("orc", "s", ps), SID)

    def test_a_fresh_spawn_returns_its_session_id(self):
        ps = f"claude --session-id {SID} --settings {_settings('s', 'orc')}"
        self.assertEqual(self.sid("orc", "s", ps), SID)

    def test_the_returned_sid_is_lowercased(self):
        # session ids are compared and stored lowercased everywhere; argv may hold either case.
        ps = f"claude --session-id {SID.upper()} --settings {_settings('s', 'orc')}"
        self.assertEqual(self.sid("orc", "s", ps), SID)

    # --- isolation ---------------------------------------------------------
    def test_two_lines_that_share_an_agent_name_do_not_cross(self):
        # aae1/orc and inna/orc are different agents. The slug segment in the --settings path
        # is what keeps them apart; without it, a query for one picks up the other's session.
        ps = "\n".join([
            f"claude --session-id {SID} --settings {_settings('aae1', 'orc')}",
            f"claude --session-id {OTHER} --settings {_settings('inna', 'orc')}",
        ])
        self.assertEqual(self.sid("orc", "aae1", ps), SID)
        self.assertEqual(self.sid("orc", "inna", ps), OTHER)

    def test_an_agent_name_is_matched_whole_not_as_a_prefix(self):
        # settings-eval.json must answer to "eval" ALONE — not to "ev", nor to "evaluator".
        ps = f"claude --session-id {SID} --settings {_settings('s', 'eval')}"
        self.assertEqual(self.sid("eval", "s", ps), SID)
        self.assertIsNone(self.sid("ev", "s", ps))
        self.assertIsNone(self.sid("evaluator", "s", ps))

    def test_no_matching_process_is_None(self):
        self.assertIsNone(self.sid("orc", "s", ""))
        self.assertIsNone(self.sid("orc", "s", "claude --settings /other/thing.json"))

    # --- both --settings forms ---------------------------------------------
    def test_the_equals_form_of_settings_is_matched_like_the_space_form(self):
        space = f"claude --session-id {SID} --settings {_settings('s', 'orc')}"
        equals = f"claude --session-id={SID} --settings={_settings('s', 'orc')}"
        self.assertEqual(self.sid("orc", "s", space), SID)
        self.assertEqual(self.sid("orc", "s", equals), SID)


class AgentLines(unittest.TestCase):
    """_agent_lines — the filter that decides which process lines belong to one agent."""

    def test_it_keeps_only_the_lines_with_the_matching_settings_basename(self):
        ps = "\n".join([
            f"claude --settings {_settings('s', 'orc')}",     # keep
            f"claude --settings {_settings('s', 'coder')}",   # wrong agent
            "claude --resume abc",                            # no --settings at all
            "some unrelated process",
        ])
        lines = live._agent_lines(ps, "s", "orc")
        self.assertEqual(len(lines), 1)
        self.assertIn("settings-orc.json", lines[0])

    def test_the_slug_segment_is_required(self):
        # basename settings-orc.json but under a DIFFERENT line: not this agent.
        ps = f"claude --settings {_settings('other', 'orc')}"
        self.assertEqual(live._agent_lines(ps, "s", "orc"), [])


class Newest(unittest.TestCase):
    """_newest — the tiebreak inside a live/frozen set, which normally holds one element."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="af-newest-")
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name)

    def _write(self, sid: str, mtime: float) -> None:
        f = self.proj / "proj-x" / f"{sid}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (mtime, mtime))

    def test_the_most_recently_written_transcript_wins(self):
        now = time.time()
        self._write(SID, now - 100)
        self._write(OTHER, now)
        self.assertEqual(live._newest({SID, OTHER}, self.proj), OTHER)

    def test_a_lone_id_with_no_transcript_on_disk_still_comes_back(self):
        # The log may not be created yet; a caller must get an answer, not None.
        self.assertEqual(live._newest({SID}, self.proj), SID)

    def test_an_empty_set_is_None(self):
        self.assertIsNone(live._newest(set(), self.proj))


if __name__ == "__main__":
    unittest.main()
