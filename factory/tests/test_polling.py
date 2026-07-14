"""polling: the three safeties, as pure decisions. No timer process is started and no pane
is touched — tick_verdict / done / parse_minutes are the whole policy, and they are pure.

  backpressure  a tick is SKIPPED while the previous one is still unread (one outstanding
                message, ever).
  sid-change    the timer EXITS if a different agent took the name (a new session id).
  down          the timer EXITS if the agent's session is gone.
  --times N     bounds the timer.
  floor         1 minute, and `5m` parses.
"""

from __future__ import annotations

import unittest

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import polling


class TickVerdict(unittest.TestCase):
    def test_a_clean_tick_sends(self):
        self.assertEqual(
            polling.tick_verdict(unread=0, alive=True, started_sid="s", now_sid="s"), "send")

    def test_unread_backlog_is_skipped_never_queued(self):
        # One outstanding message, ever: a tick behind an unread tick would build a backlog
        # the agent spends its life reading.
        self.assertEqual(
            polling.tick_verdict(unread=1, alive=True, started_sid="s", now_sid="s"), "skip")

    def test_a_dead_agent_exits_the_timer(self):
        self.assertEqual(
            polling.tick_verdict(unread=0, alive=False, started_sid="s", now_sid="s"),
            "exit-down")

    def test_a_changed_sid_exits_rather_than_ordering_a_stranger(self):
        # A fresh spawn minted a new session id: whoever holds this name now never asked for
        # this timer. Do NOT hand a ghost's orders to a stranger.
        self.assertEqual(
            polling.tick_verdict(unread=0, alive=True, started_sid="old", now_sid="new"),
            "exit-hijacked")

    def test_down_beats_hijack_and_backpressure(self):
        # If the session is gone there is no one to compare a sid against and no one to skip
        # for — down is checked first.
        self.assertEqual(
            polling.tick_verdict(unread=5, alive=False, started_sid="a", now_sid="b"),
            "exit-down")


class TimesBound(unittest.TestCase):
    def test_times_zero_is_unbounded(self):
        self.assertFalse(polling.done(sent=99, times=0))

    def test_times_n_stops_at_n(self):
        self.assertFalse(polling.done(sent=0, times=1))
        self.assertTrue(polling.done(sent=1, times=1))
        self.assertTrue(polling.done(sent=3, times=2))


class ParseMinutes(unittest.TestCase):
    def test_plain_and_suffixed(self):
        self.assertEqual(polling.parse_minutes("5"), 5)
        self.assertEqual(polling.parse_minutes("5m"), 5)
        self.assertEqual(polling.parse_minutes("20min"), 20)

    def test_garbage_is_rejected_not_read_as_zero(self):
        with self.assertRaises(ValueError):
            polling.parse_minutes("soon")
        with self.assertRaises(ValueError):
            polling.parse_minutes("")


class StartGuards(TempFactory):
    """start() refuses before it ever launches a loop — no session, no sid, sub-floor."""

    def test_below_the_floor_is_refused(self):
        # min_minutes() defaults to 1; 0 is below it.
        self.assertEqual(polling.start("w", "0", "hi", p=self.p), 1)

    def test_no_session_is_refused(self):
        # No tmux session called ai-aftest-w exists in the temp factory.
        self.assertEqual(polling.start("w", "5", "hi", p=self.p), 1)

    def test_start_parses_flags_anywhere_in_the_args(self):
        agent, mins, msg, times, kind = polling._parse_start(
            ["w", "5", "hello", "--times", "3", "world", "--kind", "task"])
        self.assertEqual((agent, mins, times, kind), ("w", "5", 3, "task"))
        self.assertEqual(msg, "hello world")


if __name__ == "__main__":
    unittest.main()
