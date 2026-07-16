"""warden: it reads WHO was cut off from the markers and WHEN the window lifts from
limits.json, and it never guesses a time it does not have. The wake path is exercised with
tmux and the doorbell mocked — no pane is touched, nothing is spawned.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import warden, tmux, drive, mailbox


class ResetsAt(TempFactory):
    def test_missing_file_is_unknown_not_now(self):
        # 0 means UNKNOWN. A rescuer that read a missing reset as "now" would wake every
        # cut-off agent straight back into the same wall.
        self.assertEqual(warden.resets_at(self.p), 0)

    def test_reads_five_hour_resets_at(self):
        self.p.limits_json.write_text(json.dumps(
            {"rate_limits": {"five_hour": {"resets_at": 1784066400, "used_percentage": 16}}}))
        self.assertEqual(warden.resets_at(self.p), 1784066400)
        self.assertEqual(warden.used_pct(self.p), 16)

    def test_corrupt_limits_is_unknown(self):
        self.p.limits_json.write_text("{not json")
        self.assertEqual(warden.resets_at(self.p), 0)


class Marker(TempFactory):
    def test_marker_carries_when_and_sid(self):
        self.p.limited("w").write_text("1784000000\tsid-abc\thook\n")
        when, sid = warden.marker("w", self.p)
        self.assertEqual((when, sid), (1784000000, "sid-abc"))

    def test_missing_marker_is_zeroes(self):
        self.assertEqual(warden.marker("w", self.p), (0, ""))


class WakePath(TempFactory):
    def _setup_cutoff(self, agent="w", sid="sid-1", reset_delta=-200):
        self.p.sid_file(agent).write_text(sid)
        self.p.limited(agent).write_text(f"{int(time.time()) - 600}\t{sid}\thook\n")
        self.p.limits_json.write_text(json.dumps(
            {"rate_limits": {"five_hour": {"resets_at": int(time.time()) + reset_delta}}}))

    def test_wake_mails_from_orchestrator_and_says_the_turn_was_lost(self):
        self._setup_cutoff()
        with mock.patch.object(drive, "ring", return_value=True):
            warden.wake("w", "at 09:00", self.p)
        msgs = mailbox.dump("w", self.p)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].frm, "orchestrator")
        self.assertIn("cut your turn off", msgs[0].body)
        self.assertIn("ask, rather than guessing", msgs[0].body)

    def test_squad_agents_scopes_to_this_slug(self):
        with mock.patch.object(tmux, "list_sessions",
                               return_value=["ai-aftest-w", "ai-other-x", "ai-aftest-boss"]):
            self.assertEqual(sorted(warden.squad_agents(self.p)), ["boss", "w"])

    def test_pane_still_limited_blocks_the_wake(self):
        # The 7-day cap can hold you down long past the 5-hour reset; a pane still showing the
        # wall means the window did not really lift.
        with mock.patch.object(tmux, "capture_pane",
                               return_value="You've hit your usage limit · resets 3:45pm"):
            self.assertTrue(warden.pane_limited("w", self.p))
        with mock.patch.object(tmux, "capture_pane", return_value="❯ all clear"):
            self.assertFalse(warden.pane_limited("w", self.p))


class PidfileLocation(TempFactory):
    """The warden's pidfile/logfile must be under durable_state (not /tmp) so a /tmp purge
    cannot drop the pidfile mid-run and spawn a second warden with no coordination."""

    def test_pidfile_is_under_durable_state(self):
        # durable_state is under SPEC_HOME (~/.claude/agent-factory/state/<slug>),
        # NOT under AF_ROOT (default /tmp/agent-factory).
        pf = warden.pidfile(self.p)
        self.assertTrue(str(pf).startswith(str(self.p.durable_state)))
        self.assertEqual(pf.name, "warden.pid")

    def test_logfile_is_under_durable_state(self):
        lf = warden.logfile(self.p)
        self.assertTrue(str(lf).startswith(str(self.p.durable_state)))
        self.assertEqual(lf.name, "warden.log")

    def test_pidfile_is_not_under_state(self):
        # The old location was p.state / "warden.pid" (under AF_ROOT, default /tmp).
        # It must NOT be there anymore.
        pf = warden.pidfile(self.p)
        self.assertFalse(str(pf).startswith(str(self.p.state)))


class PokeOnlyOrchestrator(TempFactory):
    """The rescue loop only actively pokes the orchestrator. Non-orchestrator agents
    get their baseline tracked via probe() (read-only) but are never ring()'d or mailed
    a WAKE message by the warden itself."""

    def test_find_orchestrator_reads_squad_role(self):
        # When squad.json has a station with role="orchestrator", that is the answer.
        from af import roster
        orc_st = mock.Mock()
        orc_st.name = "orc"
        orc_st.role = "orchestrator"
        w_st = mock.Mock()
        w_st.name = "w"
        w_st.role = "worker"
        with mock.patch.object(roster, "stations", return_value=[orc_st, w_st]):
            self.assertEqual(warden._find_orchestrator(self.p), "orc")

    def test_find_orchestrator_fallback_to_name(self):
        # If the roster has no record, fall back to a literally-named "orc" or "orchestrator".
        from af import roster
        with mock.patch.object(roster, "stations", return_value=[]):
            with mock.patch.object(tmux, "list_sessions",
                                   return_value=["ai-aftest-orc", "ai-aftest-w"]):
                self.assertEqual(warden._find_orchestrator(self.p), "orc")

    def test_find_orchestrator_returns_none_when_no_orc(self):
        from af import roster
        w_st = mock.Mock()
        w_st.name = "w"
        w_st.role = "worker"
        c_st = mock.Mock()
        c_st.name = "coder"
        c_st.role = "worker"
        with mock.patch.object(roster, "stations", return_value=[w_st, c_st]):
            with mock.patch.object(tmux, "list_sessions",
                                   return_value=["ai-aftest-w", "ai-aftest-coder"]):
                self.assertIsNone(warden._find_orchestrator(self.p))


class PokeInterval(unittest.TestCase):
    """poke_every is the capped interval between wakes: the warden never waits for a scraped
    reset clock, it re-pokes this often and watches the transcript. Default 300s; an operator
    can retune it with AI_LIMITS_POKE, and junk in that env must fall back rather than crash
    (the warden is a rescuer — a crash there is silent).

    The single-iteration rescue path (a marked agent gets poked; a climbed end_turn count
    clears the marker) is NOT tested here: that logic lives INLINE in warden.loop(), an
    hours-long sleep loop with no factored-out step to call, so exercising it would mean
    mocking tmux/probe/drive/mailbox and re-implementing the loop body in the test. That is
    too invasive to be a trustworthy check, so it is deliberately skipped.
    """

    def test_default_is_300(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_LIMITS_POKE", None)
            self.assertEqual(warden.poke_every(), 300)

    def test_env_overrides_the_default(self):
        with mock.patch.dict(os.environ, {"AI_LIMITS_POKE": "45"}):
            self.assertEqual(warden.poke_every(), 45)

    def test_junk_env_falls_back_to_the_default(self):
        for junk in ("notanumber", "", "-5", "3.5"):
            with self.subTest(junk=junk):
                with mock.patch.dict(os.environ, {"AI_LIMITS_POKE": junk}):
                    self.assertEqual(warden.poke_every(), 300)


if __name__ == "__main__":
    unittest.main()
