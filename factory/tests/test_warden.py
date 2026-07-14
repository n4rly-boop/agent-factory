"""warden: it reads WHO was cut off from the markers and WHEN the window lifts from
limits.json, and it never guesses a time it does not have. The wake path is exercised with
tmux and the doorbell mocked — no pane is touched, nothing is spawned.
"""

from __future__ import annotations

import json
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

    def test_line_agents_scopes_to_this_slug(self):
        with mock.patch.object(tmux, "list_sessions",
                               return_value=["ai-aftest-w", "ai-other-x", "ai-aftest-boss"]):
            self.assertEqual(sorted(warden.line_agents(self.p)), ["boss", "w"])

    def test_pane_still_limited_blocks_the_wake(self):
        # The 7-day cap can hold you down long past the 5-hour reset; a pane still showing the
        # wall means the window did not really lift.
        with mock.patch.object(tmux, "capture_pane",
                               return_value="You've hit your usage limit · resets 3:45pm"):
            self.assertTrue(warden.pane_limited("w", self.p))
        with mock.patch.object(tmux, "capture_pane", return_value="❯ all clear"):
            self.assertFalse(warden.pane_limited("w", self.p))


if __name__ == "__main__":
    unittest.main()
