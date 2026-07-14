"""sweep: who must NOT be compacted, and when.

Every skip rule here is a bug that shipped once. The tests are on the pure decision
functions (skip_reason, compact_decision, resolve_thresholds) plus the reaper, with tmux
mocked out — NO agent is spawned and no live pane is ever touched.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import drive, sweep as sweepmod
from af.probe import Probe


def _p(alive=True, phase="idle", ctx=300000, endturns=3) -> Probe:
    return Probe(alive=alive, phase=phase, ctx=ctx, endturns=endturns, inputbox="")


class SkipRules(unittest.TestCase):
    def test_a_sweepable_agent_is_not_skipped(self):
        self.assertIsNone(sweepmod.skip_reason("worker", _p(), me="orchestrator"))

    def test_orchestrator_is_never_an_agent(self):
        # It is the mailbox of the DRIVING session. An agent by that name would be skipped by
        # every sweep AND would pass the sweep guard — it would start compacting its peers.
        self.assertEqual(sweepmod.skip_reason("orchestrator", _p()), "orchestrator")

    def test_a_sweeper_cannot_compact_itself(self):
        # The sweep runs inside its own agent's Bash tool: /compact would land in its own
        # pane, mid-turn — its own turn.
        self.assertEqual(sweepmod.skip_reason("orc", _p(), me="orc"), "self")

    def test_the_recipient_of_the_post_that_triggered_us_is_skipped(self):
        self.assertEqual(sweepmod.skip_reason("worker", _p(), skip="worker"), "recipient")

    def test_a_down_agent_has_no_pane_to_type_into(self):
        self.assertEqual(sweepmod.skip_reason("worker", _p(alive=False)), "down")

    def test_mid_generation_is_not_a_safe_point(self):
        self.assertEqual(sweepmod.skip_reason("worker", _p(phase="generating")), "generating")

    def test_a_permission_prompt_would_be_ANSWERED_by_the_keystrokes(self):
        self.assertEqual(sweepmod.skip_reason("worker", _p(phase="permission")), "permission")

    def test_a_usage_limited_agent_cannot_compact_at_all(self):
        # /compact is a model call and the model is what it has run out of. Sending it
        # achieves nothing, forever: the context never drops, so the next tick re-sends it.
        self.assertEqual(sweepmod.skip_reason("worker", _p(phase="limited")), "limited")

    def test_cooldown_stops_us_re_compacting_an_agent_that_is_already_compacting(self):
        now = 1_000_000
        self.assertEqual(
            sweepmod.skip_reason("w", _p(), last_compacted=now - 10, now=now, cooldown=600),
            "cooling")
        self.assertIsNone(
            sweepmod.skip_reason("w", _p(), last_compacted=now - 601, now=now, cooldown=600))

    def test_liveness_is_judged_before_the_cooldown(self):
        # A down agent must report "down", not be excused as merely cooling: the reaper's
        # decision depends on aliveness, and the two must not disagree.
        now = 1_000_000
        self.assertEqual(
            sweepmod.skip_reason("w", _p(alive=False), last_compacted=now, now=now), "down")


class Thresholds(unittest.TestCase):
    def test_the_agents_OWN_thresholds_win_over_the_sweepers_env(self):
        # A station on a 200k-window model is configured compact_soft: 80000. Judged by the
        # sweeper's 200000 it would never be compacted until it died.
        soft, hard = drive.resolve_thresholds(80000, 150000,
                                              env={"AI_COMPACT_SOFT": "200000"})
        self.assertEqual((soft, hard), (80000, 150000))

    def test_env_is_the_fallback(self):
        soft, hard = drive.resolve_thresholds(
            None, None, env={"AI_COMPACT_SOFT": "9000", "AI_COMPACT_HARD": "9999"})
        self.assertEqual((soft, hard), (9000, 9999))

    def test_defaults_when_nothing_says_otherwise(self):
        self.assertEqual(drive.resolve_thresholds(None, None, env={}), (200000, 500000))

    def test_junk_must_not_DISABLE_the_guard(self):
        # Only an explicit 0 turns a threshold off. "" and "abc" fall back to the default —
        # a guard silently disabled by a typo is the failure this system keeps having.
        self.assertEqual(drive.resolve_thresholds("abc", "", env={}), (200000, 500000))
        self.assertEqual(drive.resolve_thresholds(None, None,
                                                  env={"AI_COMPACT_SOFT": "-5"}), (200000, 500000))

    def test_zero_disables(self):
        soft, hard = drive.resolve_thresholds(0, 0, env={})
        self.assertEqual((soft, hard), (0, 0))
        self.assertEqual(drive.compact_decision(9_000_000, 0, 0, False), "none")


class Decision(unittest.TestCase):
    def test_soft_compacts_only_BETWEEN_tasks(self):
        self.assertEqual(drive.compact_decision(250_000, 200_000, 500_000, False), "soft")

    def test_soft_HOLDS_mid_task(self):
        # A task spans many turns; compacting in the middle of one throws away the working
        # state the agent still needs.
        self.assertEqual(drive.compact_decision(250_000, 200_000, 500_000, True), "hold")

    def test_hard_fires_mid_task_anyway(self):
        # Losing some working state is bad; running out of context loses everything.
        self.assertEqual(drive.compact_decision(600_000, 200_000, 500_000, True), "hard")

    def test_under_the_soft_threshold_nothing_happens(self):
        self.assertEqual(drive.compact_decision(100, 200_000, 500_000, False), "none")

    def test_an_agent_with_no_usage_yet_is_left_alone(self):
        self.assertEqual(drive.compact_decision(0, 200_000, 500_000, False), "none")


class SpecThresholds(TempFactory):
    def test_read_back_out_of_the_spec_written_at_spawn(self):
        from af import spec as af_spec
        af_spec.write(af_spec.Spec(
            slug=self.slug, name="qa", cwd=str(self.root), sid="s", spawned=0,
            flags="--model sonnet", ai_env={"AI_COMPACT_SOFT": "80000",
                                            "AI_COMPACT_HARD": "150000"}), self.p)
        self.assertEqual(drive.spec_thresholds("qa", self.p), (80000, 150000))

    def test_a_missing_spec_answers_None_None_not_zero(self):
        # (None, None) means "unknown — use the env"; (0, 0) would mean "disabled".
        self.assertEqual(drive.spec_thresholds("nobody", self.p), (None, None))


class Reaper(TempFactory):
    """A `busy` flag whose agent no longer exists silently exempts that NAME from soft
    compaction forever — and the next agent to take the name inherits the exemption."""

    def test_a_stale_busy_flag_of_a_dead_agent_is_reaped(self):
        self.p.task_flag("ghost").write_text("busy")
        self.p.tasker("ghost").write_text("orchestrator")
        with mock.patch("af.sweep.tmux.has_session", return_value=False):
            sweepmod._reap(self.p)
        self.assertFalse(self.p.task_flag("ghost").exists())
        self.assertFalse(self.p.tasker("ghost").exists())

    def test_a_LIVE_agents_busy_flag_is_left_alone(self):
        self.p.task_flag("worker").write_text("busy")
        self.p.tasker("worker").write_text("orchestrator")
        with mock.patch("af.sweep.tmux.has_session", return_value=True):
            sweepmod._reap(self.p)
        self.assertTrue(self.p.task_flag("worker").exists())

    def test_a_dead_agent_with_UNREAD_mail_keeps_its_flag(self):
        # That flag is not garbage: it is a task legitimately QUEUED for an agent that is
        # down (mail.sh promises it will be rung on the next up/revive). Reaping it would
        # also destroy the tasker record its eventual `done` needs to clear the busy state.
        from af import mailbox
        mailbox.send("queued", "do the thing", kind="task", frm="orchestrator", p=self.p)
        self.assertEqual(mailbox.unread("queued", self.p), 1)
        with mock.patch("af.sweep.tmux.has_session", return_value=False):
            sweepmod._reap(self.p)
        self.assertEqual(self.p.task_flag("queued").read_text(), "busy")
        self.assertTrue(self.p.tasker("queued").exists())


class Lock(TempFactory):
    def test_two_sweeps_cannot_run_at_once(self):
        first = sweepmod._Lock(self.p)
        self.assertTrue(first.take())
        second = sweepmod._Lock(self.p)
        self.assertFalse(second.take())    # would type /compact into the same pane twice
        first.release()
        self.assertTrue(second.take())
        second.release()

    def test_a_lock_older_than_ten_minutes_is_a_corpse_and_is_taken(self):
        # Its holder was killed mid-sweep. Left alone it would silently disable EVERY sweep.
        stale = sweepmod._Lock(self.p)
        self.assertTrue(stale.take())
        old = time.time() - (sweepmod.SWEEP_LOCK_STALE + 60)
        import os
        os.utime(self.p.sweep_lock, (old, old))
        taker = sweepmod._Lock(self.p)
        self.assertTrue(taker.take())
        taker.release()

    def test_the_lock_is_released_even_when_the_sweep_explodes(self):
        with mock.patch("af.sweep.probe", side_effect=RuntimeError("boom")):
            self.p.box("worker").write_text("")   # a mailbox to walk
            with self.assertRaises(RuntimeError):
                sweepmod.sweep(p=self.p)
        self.assertFalse(self.p.sweep_lock.exists())


class Autosweep(TempFactory):
    """Only an ORCHESTRATOR sweeps: a worker running `af mail` in its own session must not
    start compacting its peers."""

    def test_the_top_session_sweeps(self):
        with mock.patch("af.sweep.sweep") as sw:
            sweepmod.autosweep("", self.p)
        sw.assert_called_once()

    def test_a_worker_does_not(self):
        import os
        os.environ["AF_AGENT"] = "worker"
        with mock.patch("af.sweep.sweep") as sw:
            sweepmod.autosweep("", self.p)
        sw.assert_not_called()

    def test_an_agent_whose_ROLE_is_orchestrator_DOES(self):
        # A line's own orc is named whatever the blueprint called it. Testing the NAME left
        # the autonomous line — the case this was built for — with no sweeps at all.
        import os
        os.environ["AF_AGENT"] = "orc"
        os.environ["AF_ROLE"] = "orchestrator"
        self.addCleanup(os.environ.pop, "AF_ROLE", None)
        with mock.patch("af.sweep.sweep") as sw, mock.patch("af.sweep.self_ctx_warn"):
            sweepmod.autosweep("", self.p)
        sw.assert_called_once()

    def test_AI_SWEEP_OFF_stops_it(self):
        import os
        os.environ["AI_SWEEP_OFF"] = "1"
        self.addCleanup(os.environ.pop, "AI_SWEEP_OFF", None)
        with mock.patch("af.sweep.sweep") as sw:
            sweepmod.autosweep("", self.p)
        sw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
