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
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import FACTORY   # noqa: F401 — imported first: puts the af package on sys.path

from af import live, tmux as tmux_mod
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


class Ps(unittest.TestCase):
    """_ps — regression: a non-UTF-8 byte in ONE unrelated process' argv used to blow up
    strict `text=True` decoding, and the bare `except Exception: return ""` around it then
    blinded live_sid/reconcile/heal for every agent on the host, not just the odd one. `_ps`
    must decode permissively (errors="replace") so the damage stays confined to that one
    line."""

    def test_a_non_utf8_byte_in_one_line_does_not_blank_the_whole_read(self):
        # A stray process with an invalid UTF-8 continuation byte (\xe2 alone, no continuation)
        # sitting next to an otherwise normal line. Old code: subprocess.run(text=True) raises
        # UnicodeDecodeError here, the except swallows it, _ps() returns "".
        bad = b"some-other-proc --flag \xe2 garbage-arg\n"
        good = f"claude --session-id {SID} --settings {_settings('s', 'orc')}\n".encode()
        raw = bad + good
        with mock.patch(
            "af.live.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=raw),
        ):
            out = live._ps()
        # Must not raise (already implied by reaching here) and must not come back empty.
        self.assertNotEqual(out, "")
        self.assertIn("�", out)  # the replacement character, not a swallowed exception

    def test_the_bad_byte_does_not_corrupt_or_drop_an_unrelated_good_line(self):
        bad = b"some-other-proc --flag \xe2 garbage-arg\n"
        good = f"claude --session-id {SID} --settings {_settings('s', 'orc')}\n".encode()
        raw = bad + good
        with mock.patch(
            "af.live.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=raw),
        ):
            ps_out = live._ps()
        # The unrelated bad line must not stop the real agent's line from parsing correctly
        # once fed through the same helpers live_sid uses.
        lines = live._agent_lines(ps_out, "s", "orc")
        self.assertEqual(len(lines), 1)
        self.assertIn("settings-orc.json", lines[0])
        d = tempfile.TemporaryDirectory(prefix="af-ps-proj-")
        self.addCleanup(d.cleanup)
        p = Paths(slug="s", root=Path(d.name), cwd=Path(d.name), mailroot=Path(d.name),
                   specroot=Path(d.name))
        with mock.patch("af.paths.PROJECTS", Path(d.name)):
            self.assertEqual(live.live_sid("orc", p, ps_out=ps_out), SID)

    def test_ps_raising_is_still_caught_and_returns_empty(self):
        # A genuine failure to run ps at all (binary missing, timeout, etc.) is still handled
        # by the outer except — only the UnicodeDecodeError path was ever the bug.
        with mock.patch("af.live.subprocess.run", side_effect=OSError("no such file")):
            self.assertEqual(live._ps(), "")


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


class PsTree(unittest.TestCase):
    """_ps_tree — returns (pid, ppid, command) triples."""

    def test_parses_pid_ppid_command(self):
        raw = b"  12345     1 /usr/bin/claude --session-id abc\n  12346 12345 /usr/bin/python\n"
        with mock.patch(
            "af.live.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=raw),
        ):
            result = live._ps_tree()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], ("12345", "1", "/usr/bin/claude --session-id abc"))
        self.assertEqual(result[1], ("12346", "12345", "/usr/bin/python"))

    def test_empty_output_returns_empty_list(self):
        raw = b""
        with mock.patch(
            "af.live.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=raw),
        ):
            result = live._ps_tree()
        self.assertEqual(result, [])

    def test_non_utf8_byte_does_not_crash(self):
        raw = b"  12345     1 /usr/bin/proc --arg \xe2\n"
        with mock.patch(
            "af.live.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=raw),
        ):
            result = live._ps_tree()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "12345")

    def test_ps_failure_returns_empty_list(self):
        with mock.patch("af.live.subprocess.run", side_effect=OSError("no ps")):
            self.assertEqual(live._ps_tree(), [])


class ResolveSid(unittest.TestCase):
    """_resolve_sid — the shared session-id resolution logic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="af-resolve-")
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name)

    def test_fork_returns_fork_not_parent(self):
        # fork is a session-id that is NOT anyone's resume target
        self.assertEqual(
            live._resolve_sid({FORK, PARENT}, {PARENT}, self.proj),
            FORK,
        )

    def test_launcher_returns_resume_target(self):
        # No session-id that is not a resume target -> the resume target IS live
        self.assertEqual(
            live._resolve_sid(set(), {SID}, self.proj),
            SID,
        )

    def test_nothing_to_resolve_returns_none(self):
        self.assertIsNone(
            live._resolve_sid(set(), set(), self.proj),
        )

    def test_fresh_spawn_returns_its_session_id(self):
        # session-id exists, no resume targets -> it is the live session
        self.assertEqual(
            live._resolve_sid({SID}, set(), self.proj),
            SID,
        )


class PaneRootPid(unittest.TestCase):
    """pane_root_pid — resolves the OS pid of a tmux pane's root process."""

    def test_returns_first_pid(self):
        with mock.patch.object(tmux_mod, "list_panes", return_value=["12345", "67890"]):
            self.assertEqual(live.pane_root_pid("my-session"), "12345")

    def test_returns_none_when_no_panes(self):
        with mock.patch.object(tmux_mod, "list_panes", return_value=[]):
            self.assertIsNone(live.pane_root_pid("my-session"))


class SidInPane(unittest.TestCase):
    """sid_in_pane — resolves the live session id from a pane's process tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="af-sid-pane-")
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch("af.paths.PROJECTS", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_session_id_from_descendant(self):
        # Pane root pid 100, child 101 has --session-id
        ps_text = f"100     1 /usr/bin/bash\n101   100 claude --session-id {SID}"
        with mock.patch.object(live, "pane_root_pid", return_value="100"):
            result = live.sid_in_pane("my-session", ps_out=ps_text)
        self.assertEqual(result, SID)

    def test_returns_resume_target_when_no_fork(self):
        ps_text = f"100     1 /usr/bin/bash\n101   100 claude --resume {SID}"
        with mock.patch.object(live, "pane_root_pid", return_value="100"):
            result = live.sid_in_pane("my-session", ps_out=ps_text)
        self.assertEqual(result, SID)

    def test_returns_fork_not_parent(self):
        ps_text = (
            f"100     1 /usr/bin/bash\n"
            f"101   100 claude --session-id {FORK} --fork-session --resume {PARENT}"
        )
        with mock.patch.object(live, "pane_root_pid", return_value="100"):
            result = live.sid_in_pane("my-session", ps_out=ps_text)
        self.assertEqual(result, FORK)

    def test_returns_none_when_no_session_id(self):
        ps_text = "100     1 /usr/bin/bash\n101   100 vim file.txt"
        with mock.patch.object(live, "pane_root_pid", return_value="100"):
            result = live.sid_in_pane("my-session", ps_out=ps_text)
        self.assertIsNone(result)

    def test_returns_none_when_pane_root_pid_is_none(self):
        with mock.patch.object(live, "pane_root_pid", return_value=None):
            result = live.sid_in_pane("my-session", ps_out="anything")
        self.assertIsNone(result)

    def test_walks_descendants_not_siblings(self):
        # pid 200 is NOT a descendant of 100 (its ppid is 1, not 100)
        ps_text = (
            f"100     1 /usr/bin/bash\n"
            f"101   100 claude --session-id {SID}\n"
            f"200     1 other --session-id {OTHER}"
        )
        with mock.patch.object(live, "pane_root_pid", return_value="100"):
            result = live.sid_in_pane("my-session", ps_out=ps_text)
        self.assertEqual(result, SID)  # NOT OTHER


if __name__ == "__main__":
    unittest.main()
