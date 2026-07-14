"""probe: one capture-pane, one pass over the transcript, one frozen fact.

The transcripts here are synthetic .jsonl written into a temp dir — including a torn
final line, which is the whole reason the bash used grep instead of jq: the file is being
appended to while it is read.

No live tmux session is touched: tmux.capture_pane is patched out.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from support import TempFactory, fixture   # imported first: it puts the af package on sys.path

from af import paths as af_paths
from af import probe


def _asst(inp=0, cache_read=0, cache_creation=0, output=0, stop_reason="end_turn"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": output,
            },
        },
    }


def _user(text="hi"):
    return {"type": "user", "message": {"role": "user", "content": text}}


class TestScanLog(TempFactory):
    def _write(self, records, trailing: str = "") -> "object":
        f = self.root / "session.jsonl"
        body = "".join(json.dumps(r) + "\n" for r in records) + trailing
        f.write_text(body, encoding="utf-8")
        return f

    def test_ctx_is_the_last_assistant_records_prompt(self):
        f = self._write([
            _asst(inp=10, cache_read=100, cache_creation=1000, output=7),
            _user(),
            _asst(inp=3, cache_read=40_000, cache_creation=5_000, output=999, stop_reason="tool_use"),
        ])
        ctx, _ = probe._scan_log(f)
        # input + cache_read + cache_creation of the LAST assistant record. Output is not
        # part of the next prompt; the cached buckets are.
        self.assertEqual(ctx, 3 + 40_000 + 5_000)

    def test_endturns_counts_only_end_turn(self):
        f = self._write([
            _asst(stop_reason="end_turn"),
            _asst(stop_reason="tool_use"),
            _asst(stop_reason="end_turn"),
            _asst(stop_reason="max_tokens"),
            _user(),
        ])
        _, endturns = probe._scan_log(f)
        self.assertEqual(endturns, 2)

    def test_truncated_final_line_does_not_raise(self):
        # The transcript is being appended to as we read it. bash feared exactly this.
        good = [_asst(inp=1, cache_read=2, cache_creation=3), _asst(inp=5, cache_read=6, cache_creation=7)]
        torn = json.dumps(_asst(inp=999_999))[:40]   # half a line, no newline
        f = self._write(good, trailing=torn)
        ctx, endturns = probe._scan_log(f)
        # The torn line is dropped, and NOTHING else: the last COMPLETE record still counts.
        self.assertEqual(ctx, 5 + 6 + 7)
        self.assertEqual(endturns, 2)

    def test_garbage_line_in_the_middle_is_dropped(self):
        f = self.root / "g.jsonl"
        f.write_text(
            json.dumps(_asst(inp=1, cache_read=1, cache_creation=1)) + "\n"
            + '{"type":"assistant" TORN GARBAGE\n'
            + json.dumps(_asst(inp=2, cache_read=2, cache_creation=2)) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(probe._scan_log(f), (6, 2))

    def test_no_assistant_messages(self):
        f = self._write([_user(), _user("more"), {"type": "summary", "summary": "x"}])
        self.assertEqual(probe._scan_log(f), (None, 0))

    def test_empty_and_missing_file(self):
        empty = self.root / "e.jsonl"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(probe._scan_log(empty), (None, 0))
        self.assertEqual(probe._scan_log(self.root / "nope.jsonl"), (None, 0))

    def test_assistant_without_usage_still_counts_its_end_turn(self):
        f = self._write([
            _asst(inp=9, cache_read=9, cache_creation=9),
            {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn"}},
        ])
        ctx, endturns = probe._scan_log(f)
        self.assertEqual(endturns, 2)
        # ctx sticks to the last record that actually CARRIED usage.
        self.assertEqual(ctx, 27)

    def test_missing_usage_buckets_default_to_zero(self):
        f = self.root / "p.jsonl"
        f.write_text(json.dumps({
            "type": "assistant",
            "message": {"stop_reason": "end_turn", "usage": {"input_tokens": 12}},
        }) + "\n", encoding="utf-8")
        self.assertEqual(probe._scan_log(f), (12, 1))


class TestPhasePrecedence(unittest.TestCase):
    """permission > generating > limited > idle.

    The first two are LIVE state, painted by what is happening now. The limit prose is
    scrollback — it outlives the window it describes, so it may only be believed when
    nothing live contradicts it.
    """

    PERM = "Do you want to proceed?"
    GEN = "✳ Cogitating… (4s · ↑ 1.2k tokens · esc to interrupt)"
    LIM = "You've hit your session limit · resets 10am"

    def test_all_three(self):
        self.assertEqual(probe._phase("\n".join([self.LIM, self.GEN, self.PERM])), "permission")

    def test_generating_beats_limited(self):
        # An agent that hit the limit an hour ago and is now mid-turn is GENERATING.
        # Reading it as "limited" is what parks a working agent forever.
        self.assertEqual(probe._phase("\n".join([self.LIM, self.GEN])), "generating")

    def test_permission_beats_generating(self):
        self.assertEqual(probe._phase("\n".join([self.GEN, self.PERM])), "permission")

    def test_limited_alone(self):
        self.assertEqual(probe._phase(self.LIM), "limited")

    def test_real_idle_panes_are_idle(self):
        for name in ("pane-idle-normal.txt", "pane-idle-shellmode.txt", "pane-idle-cyrillic.txt",
                     "pane-idle-queued-mail.txt", "pane-idle-compact-scrollback.txt"):
            with self.subTest(pane=name):
                self.assertEqual(probe._phase(fixture(name)), "idle")

    def test_real_fixture_phases(self):
        self.assertEqual(probe._phase(fixture("pane-generating-synth.txt")), "generating")
        self.assertEqual(probe._phase(fixture("pane-permission-synth.txt")), "permission")
        self.assertEqual(probe._phase(fixture("pane-limited-synth.txt")), "limited")


class TestSessionLog(TempFactory):
    SID = "b045d974-cf11-4979-955c-fd3f2ee9d37f"

    def setUp(self):
        super().setUp()
        self.projects = self.root / "projects"
        (self.projects / "-Users-x-proj").mkdir(parents=True)
        self.log = self.projects / "-Users-x-proj" / f"{self.SID}.jsonl"
        self.log.write_text("", encoding="utf-8")
        patcher = mock.patch.object(af_paths, "PROJECTS", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.p = af_paths.paths()          # re-resolve, so .projects is the patched one
        self.p.state.mkdir(parents=True, exist_ok=True)

    def test_no_sid_file(self):
        self.assertIsNone(probe.session_log("ghost", self.p))

    def test_finds_and_caches(self):
        self.p.sid_file("orc").write_text(self.SID)
        self.assertEqual(probe.session_log("orc", self.p), self.log)
        self.assertEqual(self.p.log_cache("orc").read_text().strip(), str(self.log))

    def test_stale_cache_naming_another_sid_is_not_believed(self):
        # A respawn under the same agent name must not be served the old agent's log.
        other = self.projects / "-Users-x-proj" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        other.write_text("", encoding="utf-8")
        self.p.sid_file("orc").write_text(self.SID)
        self.p.log_cache("orc").write_text(str(other))
        self.assertEqual(probe.session_log("orc", self.p), self.log)

    def test_cache_pointing_at_a_deleted_file_is_not_believed(self):
        self.p.sid_file("orc").write_text(self.SID)
        self.p.log_cache("orc").write_text(str(self.projects / f"{self.SID}.jsonl"))  # not there
        self.assertEqual(probe.session_log("orc", self.p), self.log)

    def test_unknown_sid_returns_none(self):
        self.p.sid_file("orc").write_text("dddddddd-dddd-dddd-dddd-dddddddddddd")
        self.assertIsNone(probe.session_log("orc", self.p))


class TestProbe(TempFactory):
    SID = "b045d974-cf11-4979-955c-fd3f2ee9d37f"

    def setUp(self):
        super().setUp()
        self.projects = self.root / "projects"
        (self.projects).mkdir(parents=True)
        (self.projects / f"{self.SID}.jsonl").write_text(
            json.dumps(_asst(inp=1, cache_read=100, cache_creation=10)) + "\n"
            + json.dumps(_asst(inp=2, cache_read=200, cache_creation=20, stop_reason="tool_use")) + "\n",
            encoding="utf-8",
        )
        patcher = mock.patch.object(af_paths, "PROJECTS", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.p = af_paths.paths()
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.p.sid_file("orc").write_text(self.SID)

    def test_live_idle_agent(self):
        with mock.patch("af.tmux.capture_pane", return_value=fixture("pane-idle-queued-mail.txt")) as cp:
            r = probe.probe("orc", self.p)
        cp.assert_called_once_with("ai-aftest-orc")   # the derived session name, read-only
        self.assertTrue(r.alive)
        self.assertEqual(r.phase, "idle")
        self.assertEqual(r.ctx, 222)
        self.assertEqual(r.endturns, 1)
        self.assertEqual(r.inputbox, "bash $AF_MAIL read")

    def test_dead_agent_keeps_its_transcript_numbers(self):
        # `ledger` and `revive` want to know how fat a dead agent's transcript is.
        with mock.patch("af.tmux.capture_pane", return_value=None):
            r = probe.probe("orc", self.p)
        self.assertFalse(r.alive)
        self.assertEqual(r.phase, "idle")
        self.assertEqual(r.ctx, 222)
        self.assertEqual(r.endturns, 1)
        self.assertIsNone(r.inputbox)

    def test_agent_with_no_transcript_yet(self):
        self.p.sid_file("orc").unlink()
        with mock.patch("af.tmux.capture_pane", return_value=fixture("pane-idle-normal.txt")):
            r = probe.probe("orc", self.p)
        self.assertTrue(r.alive)
        self.assertIsNone(r.ctx)
        # A count's zero is 0. `ctx` may honestly be unknown; `endturns` cannot — every
        # caller compares it with an int, and a log-less agent is the freshly-spawned one.
        self.assertEqual(r.endturns, 0)

    def test_generating_agent(self):
        with mock.patch("af.tmux.capture_pane", return_value=fixture("pane-generating-synth.txt")):
            r = probe.probe("orc", self.p)
        self.assertEqual(r.phase, "generating")
        self.assertEqual(r.inputbox, "")


if __name__ == "__main__":
    unittest.main()
