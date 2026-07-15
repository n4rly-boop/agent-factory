"""The writers into a pane, and the waits.

THE rule: never type into a pane that is mid-generation or on a permission prompt. A
keystroke there cancels a turn or silently APPROVES a tool call no human saw. These tests
assert the refusals — and assert the ONE deliberate exception: the doorbell does not check
the generation timer, because a just-rung agent has not painted its timer yet and the check
itself is what cancelled turns.

tmux is mocked throughout: nothing here touches a live pane.
"""

from __future__ import annotations

import unittest
from unittest import mock

from support import TempFactory, fixture   # imported first: it puts the af package on sys.path

from af import drive
from af.probe import Probe

IDLE = fixture("pane-idle-normal.txt")
BUSY = fixture("pane-generating-synth.txt")
PERM = fixture("pane-permission-synth.txt")
LIMITED = fixture("pane-limited-synth.txt")


def _p(phase="idle", endturns=1, alive=True, ctx=1000) -> Probe:
    return Probe(alive=alive, phase=phase, ctx=ctx, endturns=endturns, inputbox="")


class FakeClock:
    """A clock the wait loops read and a sleep that advances it. The loops are bounded by a
    DEADLINE, not a tick count, so a test that stubs sleep out to a no-op against the real
    clock would spin for the whole timeout in real seconds."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Say(TempFactory):
    def test_say_REFUSES_a_permission_prompt(self):
        # The prompt is a SELECT, not an input box: our text lands in the selector and the
        # Enter that follows confirms `❯ 1. Yes` — approving a tool call no human ever saw.
        with mock.patch("af.tmux.capture_pane", return_value=PERM), \
             mock.patch("af.tmux.send_keys") as sk:
            self.assertFalse(drive.say("worker", "hello", self.p))
        sk.assert_not_called()

    def test_say_clears_with_C_u_and_never_Escape(self):
        # Escape clears the box too — and CANCELS a turn in progress. C-u closes the popup
        # and clears the line while a running turn continues to completion.
        panes = [IDLE, IDLE]   # after the Enter the box no longer holds our text
        with mock.patch("af.tmux.capture_pane", side_effect=panes), \
             mock.patch("af.tmux.send_keys", return_value=True) as sk, \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(drive.say("worker", "hello", self.p))
        sent = [c.args[1] for c in sk.call_args_list]
        self.assertIn("C-u", sent)
        self.assertNotIn("Escape", sent)

    def test_say_retries_when_a_popup_ate_the_Enter(self):
        # Our text still sitting in the LIVE box after the Enter ⇒ it was never submitted.
        stuck = "❯ hello\n"
        with mock.patch("af.tmux.capture_pane", side_effect=[IDLE, stuck, stuck]), \
             mock.patch("af.tmux.send_keys", return_value=True), \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertFalse(drive.say("worker", "hello", self.p))

    def test_say_on_a_dead_agent_says_so(self):
        with mock.patch("af.tmux.capture_pane", return_value=None), \
             mock.patch("af.tmux.send_keys") as sk:
            self.assertFalse(drive.say("ghost", "hi", self.p))
        sk.assert_not_called()


class Doorbell(TempFactory):
    def test_the_doorbell_types_ONE_fixed_path_free_command(self):
        # The payload NEVER goes through the keyboard. `!cmd` both delivers and wakes, and
        # `$AF_MAIL` expands in the agent's own shell so the text has no slash — no slash,
        # no autocomplete popup to swallow the Enter.
        self.p.cap("worker").write_text("")
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", side_effect=[IDLE, IDLE]), \
             mock.patch("af.tmux.send_keys", return_value=True) as sk, \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(drive.ring("worker", self.p))
        typed = [c.args[1] for c in sk.call_args_list if c.kwargs.get("literal")]
        self.assertEqual(typed, ["!bash $AF_MAIL read"])

    def test_the_doorbell_does_NOT_check_the_generation_timer(self):
        # Deliberate, and the opposite of `say`. A just-rung agent has not painted its timer
        # yet, so a screen read calls it idle — the guard built on that reading is what
        # cancelled turns. Typed at a busy agent the command simply queues and fires at the
        # turn boundary, so there is nothing left for a busy-check to protect.
        self.p.cap("worker").write_text("")
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", side_effect=[BUSY, BUSY]), \
             mock.patch("af.tmux.send_keys", return_value=True) as sk, \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(drive.ring("worker", self.p))
        self.assertIn("!bash $AF_MAIL read",
                      [c.args[1] for c in sk.call_args_list if c.kwargs.get("literal")])

    def test_the_doorbell_REFUSES_a_permission_prompt(self):
        # Ringing there is destructive, not merely useless: the doorbell text would be typed
        # into a select prompt and the Enter would confirm the highlighted default.
        self.p.cap("worker").write_text("")
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", return_value=PERM), \
             mock.patch("af.tmux.send_keys") as sk:
            self.assertFalse(drive.ring("worker", self.p))
        sk.assert_not_called()

    def test_a_dead_agent_cannot_be_rung_and_the_mail_stays_queued(self):
        with mock.patch("af.tmux.has_session", return_value=False), \
             mock.patch("af.tmux.send_keys") as sk:
            self.assertFalse(drive.ring("worker", self.p))
        sk.assert_not_called()

    def test_an_agent_with_no_cap_marker_gets_the_degraded_prompt(self):
        # No AF_MAIL in its env ⇒ the path-free doorbell is impossible. We send an ordinary
        # prompt and let the agent decide to obey it — a model-judgment step, which is
        # exactly what the fast path removes. Legacy agents only.
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", side_effect=[IDLE, IDLE]), \
             mock.patch("af.tmux.send_keys", return_value=True) as sk, \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(drive.ring("worker", self.p))
        typed = [c.args[1] for c in sk.call_args_list if c.kwargs.get("literal")]
        self.assertTrue(typed and typed[0].startswith("NEW MAIL — run: bash "))
        self.assertNotIn("!bash $AF_MAIL read", typed)

    def test_the_registered_pane_of_an_orchestrator_wins_over_the_session_name(self):
        self.p.pane("orchestrator").write_text("mysess:0.%3")
        self.assertEqual(drive._target("orchestrator", self.p), "mysess:0.%3")
        self.assertEqual(drive._target("worker", self.p), self.p.session("worker"))


class DoorbellDedup(TempFactory):
    """One doorbell per busy period. Each `!…read` queued at a busy agent is its own model
    turn; the first reads ALL unread, so a second is a wasted empty turn. This is what made
    a chatty line look like it was spamming its agents with mail-reads."""

    def _literal(self, sk):
        return [c.args[1] for c in sk.call_args_list if c.kwargs.get("literal")]

    def test_a_second_doorbell_is_skipped_while_the_first_is_pending(self):
        self.p.cap("worker").write_text("")
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", return_value=BUSY), \
             mock.patch("af.tmux.send_keys", return_value=True) as sk, \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(drive.ring("worker", self.p))          # first: queues + marks
            self.assertTrue(self.p.ring_pending("worker").is_file())
            self.assertIn("!bash $AF_MAIL read", self._literal(sk))
            sk.reset_mock()
            self.assertTrue(drive.ring("worker", self.p))          # second: skipped
            self.assertEqual(self._literal(sk), [])                # nothing typed

    def test_an_idle_agent_is_always_rung_even_with_a_stale_marker(self):
        # An idle agent needs the nudge regardless — and its read will clear the marker.
        self.p.cap("worker").write_text("")
        self.p.ring_pending("worker").write_text("1")
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", return_value=IDLE), \
             mock.patch("af.tmux.send_keys", return_value=True) as sk, \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(drive.ring("worker", self.p))
        self.assertIn("!bash $AF_MAIL read", self._literal(sk))

    def test_an_idle_ring_does_not_set_the_marker(self):
        self.p.cap("worker").write_text("")
        with mock.patch("af.tmux.has_session", return_value=True), \
             mock.patch("af.tmux.capture_pane", return_value=IDLE), \
             mock.patch("af.tmux.send_keys", return_value=True), \
             mock.patch("af.tmux.send_enter", return_value=True), \
             mock.patch("time.sleep"):
            drive.ring("worker", self.p)
        self.assertFalse(self.p.ring_pending("worker").is_file())

    def test_reading_clears_the_pending_marker_even_with_no_new_mail(self):
        # The doorbell that triggered the read has fired; a stuck marker would silence the
        # next send forever.
        from af import mailbox
        self.p.ring_pending("worker").write_text("1")
        self.assertEqual(mailbox.read("worker", p=self.p), [])     # empty box
        self.assertFalse(self.p.ring_pending("worker").is_file())


class WaitTurn(TempFactory):
    """DONE requires BOTH a NEW end_turn in the transcript AND the live timer gone."""

    def _wait(self, observe, base=1, timeout=10):
        c = FakeClock()
        return drive.wait_turn("w", base=base, timeout=timeout, p=self.p, observe=observe,
                               sleep=c.sleep, clock=c)

    def test_done_needs_a_new_end_turn_AND_no_timer(self):
        self.assertEqual(
            self._wait(iter([_p("generating", 1), _p("generating", 2), _p("idle", 2)])), "DONE")

    def test_a_stale_end_turn_alone_is_not_done(self):
        # The baseline is what makes this honest: endturns==base means the only end_turn in
        # the log is the one from BEFORE we typed.
        self.assertEqual(self._wait(iter([_p("idle", 1)] * 4), timeout=2), "TIMEOUT")

    def test_a_new_end_turn_while_the_timer_is_STILL_UP_is_not_done(self):
        # A spurious early end_turn (a thinking block followed by more tool calls) must not
        # fool us into reading the result of a turn that is still running.
        self.assertEqual(self._wait(iter([_p("generating", 5)] * 4), timeout=2), "TIMEOUT")

    def test_a_permission_prompt_ends_the_wait_immediately(self):
        self.assertEqual(self._wait(iter([_p("permission", 1)])), "NEEDS_INPUT")

    def test_timeout_REPORTS_and_does_not_kill_the_turn(self):
        def forever():
            while True:
                yield _p("generating", 1)

        self.assertEqual(self._wait(forever(), timeout=3), "TIMEOUT")

    def test_the_wait_is_bounded_by_a_DEADLINE_not_a_tick_count(self):
        # bash could count ticks because a tick cost it a grep. Here a tick also reads the
        # transcript, so an iteration-counted loop would run for many times AI_TIMEOUT
        # seconds on a mature agent — a timeout that does not time out when the system is
        # slow is not a timeout. So: whatever a tick costs, we stop at AI_TIMEOUT.
        c = FakeClock()
        seen = []

        def slow():
            while True:
                seen.append(1)
                c.t += 4.0    # this probe took 4 seconds, not the nominal 0.5
                yield _p("generating", 1)

        self.assertEqual(
            drive.wait_turn("w", base=1, timeout=10, p=self.p, observe=slow(),
                            sleep=c.sleep, clock=c),
            "TIMEOUT")
        self.assertLessEqual(c.t, 10 + 4.5)   # stopped at the deadline, not at 20 ticks
        self.assertLess(len(seen), 20)


class WaitIdle(TempFactory):
    def _wait(self, observe, timeout=10):
        c = FakeClock()
        return drive.wait_idle("w", timeout=timeout, p=self.p, observe=observe,
                               sleep=c.sleep, clock=c)

    def test_idle_must_be_SUSTAINED(self):
        # The timer blinks out between tool calls; a single idle read there is a lie.
        self.assertEqual(
            self._wait(iter([_p("idle"), _p("generating"), _p("idle"), _p("idle"),
                             _p("idle"), _p("idle")])), "DONE")

    def test_a_flicker_of_idle_is_not_enough(self):
        self.assertEqual(self._wait(iter([_p("idle"), _p("generating")] * 8), timeout=8),
                         "TIMEOUT")

    def test_permission_wins(self):
        self.assertEqual(self._wait(iter([_p("idle"), _p("permission")])), "NEEDS_INPUT")


class Compact(TempFactory):
    """/compact is keystrokes into the input box. It is safe ONLY between turns."""

    def _refuses(self, phase):
        with mock.patch("af.drive.probemod.probe", return_value=_p(phase)), \
             mock.patch("af.drive.say") as say:
            self.assertFalse(drive.compact("w", p=self.p))
        say.assert_not_called()

    def test_refuses_mid_generation(self):
        self._refuses("generating")

    def test_refuses_on_a_permission_prompt(self):
        self._refuses("permission")

    def test_refuses_under_the_usage_limit(self):
        # /compact is a MODEL CALL, and the model is exactly what the agent has run out of.
        # It would bounce — and bounce forever: the context never drops, so every following
        # sweep re-sends it, every tick, until the quota returns.
        self._refuses("limited")

    def test_refuses_a_dead_agent(self):
        self._refuses_dead()

    def _refuses_dead(self):
        with mock.patch("af.drive.probemod.probe", return_value=_p(alive=False)), \
             mock.patch("af.drive.say") as say:
            self.assertFalse(drive.compact("ghost", p=self.p))
        say.assert_not_called()

    def test_a_compaction_stamps_the_cooldown_file(self):
        # The stamp, not the size, is what says "this one has been dealt with": the log keeps
        # the OLD size until the compaction turn lands, so without it the next sweep sees a
        # still-fat agent and compacts it again.
        with mock.patch("af.drive.probemod.probe", return_value=_p("idle", ctx=300000)), \
             mock.patch("af.drive.say", return_value=True) as say:
            self.assertTrue(drive.compact("w", nowait=True, p=self.p))
        self.assertEqual(say.call_args.args[1], "/compact")
        self.assertTrue(self.p.compacted("w").read_text().isdigit())


class MidTask(TempFactory):
    def test_mid_task_reads_the_flag_file_that_bash_writes(self):
        self.assertFalse(drive.mid_task("w", self.p))
        self.p.task_flag("w").write_text("busy")
        self.assertTrue(drive.mid_task("w", self.p))
        self.p.task_flag("w").write_text("idle")
        self.assertFalse(drive.mid_task("w", self.p))


if __name__ == "__main__":
    unittest.main()
