"""postmaster: the daemon that reconciles squad.json and catches missed doorbells while
nobody is driving. Structurally the same shape as warden (pidfile, watch/stop/status, a
tick loop) — see tests/test_warden.py for the sibling conventions this file matches.

`durable_state` (pidfile/logfile) lives under SPEC_HOME, not AF_ROOT, so — like
tests/test_manifest.py — every test here redirects af.paths.SPEC_HOME into the temp
factory. Nothing touches the real ~/.claude/agent-factory, no real subprocess is spawned
(subprocess.Popen is always mocked), and no real tmux/ps is touched (roster.reconcile and
drive.ring are always mocked or bypassed).
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import drive, mailbox, postmaster, roster
from af import paths as af_paths


class PostmasterTest(TempFactory):
    """Redirects SPEC_HOME so postmaster.pidfile/logfile (which live under p.durable_state,
    itself derived from SPEC_HOME rather than AF_ROOT) land in the temp factory too."""

    def setUp(self):
        super().setUp()
        self.spec_home = self.root / "spec_home"
        self.spec_home.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(af_paths, "SPEC_HOME", self.spec_home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.p = af_paths.paths()   # re-resolve, so .durable_state is the patched one
        self.assertTrue(str(self.p.durable_state).startswith(str(self.root)))


def _read_stdout(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


# --- pidfile liveness ---------------------------------------------------------------
class TestPidLive(PostmasterTest):
    def test_pid_of_a_missing_pidfile_is_zero(self):
        self.assertEqual(postmaster._pid(self.p), 0)

    def test_pid_reads_the_int_the_file_holds(self):
        postmaster.pidfile(self.p).parent.mkdir(parents=True, exist_ok=True)
        postmaster.pidfile(self.p).write_text("4242", encoding="utf-8")
        self.assertEqual(postmaster._pid(self.p), 4242)

    def test_pid_of_a_junk_file_is_zero(self):
        postmaster.pidfile(self.p).parent.mkdir(parents=True, exist_ok=True)
        postmaster.pidfile(self.p).write_text("not-a-pid", encoding="utf-8")
        self.assertEqual(postmaster._pid(self.p), 0)

    def test_live_is_false_for_zero_or_negative(self):
        self.assertFalse(postmaster._live(0))
        self.assertFalse(postmaster._live(-1))

    def test_live_is_true_for_a_running_process(self):
        self.assertTrue(postmaster._live(os.getpid()))

    def test_live_is_false_when_the_kernel_says_no_such_process(self):
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(postmaster._live(999999))


# --- watch / stop ---------------------------------------------------------------------
class TestWatchStop(PostmasterTest):
    def test_watch_refuses_when_already_live(self):
        postmaster.pidfile(self.p).parent.mkdir(parents=True, exist_ok=True)
        postmaster.pidfile(self.p).write_text(str(os.getpid()), encoding="utf-8")

        with mock.patch.object(postmaster, "subprocess") as fake_subprocess:
            rc, out = _read_stdout(postmaster.watch, self.p)

        self.assertEqual(rc, 0)
        self.assertIn("already watching", out)
        fake_subprocess.Popen.assert_not_called()
        # The pidfile is untouched — still the pid we planted, not overwritten.
        self.assertEqual(postmaster.pidfile(self.p).read_text(encoding="utf-8"), str(os.getpid()))

    def test_watch_starts_a_detached_process_and_writes_its_pid(self):
        self.assertFalse(postmaster.pidfile(self.p).exists())
        os.environ["AF_AGENT"] = "someagent"   # must be stripped from the child's env
        os.environ["AF_ROLE"] = "somerole"

        fake_proc = mock.Mock()
        fake_proc.pid = 13579
        with mock.patch.object(postmaster, "subprocess") as fake_subprocess:
            fake_subprocess.Popen.return_value = fake_proc
            fake_subprocess.DEVNULL = "DEVNULL"
            rc, out = _read_stdout(postmaster.watch, self.p)

        self.assertEqual(rc, 0)
        self.assertIn("watching", out)
        self.assertIn(str(fake_proc.pid), out)
        self.assertEqual(postmaster.pidfile(self.p).read_text(encoding="utf-8"), "13579")

        fake_subprocess.Popen.assert_called_once()
        args, kwargs = fake_subprocess.Popen.call_args
        argv = args[0]
        self.assertIn("af.postmaster", argv)
        self.assertIn("_loop", argv)
        env = kwargs["env"]
        self.assertEqual(env["AF_ROOT"], str(self.p.root))
        self.assertEqual(env["AF_SLUG"], self.p.slug)
        self.assertEqual(env["AF_CWD"], str(self.p.cwd))
        self.assertNotIn("AF_AGENT", env)
        self.assertNotIn("AF_ROLE", env)
        self.assertTrue(kwargs.get("start_new_session"))

    def test_stop_when_not_running_is_a_no_op(self):
        with mock.patch("os.kill") as fake_kill:
            rc, out = _read_stdout(postmaster.stop, self.p)
        self.assertEqual(rc, 0)
        self.assertIn("not watching", out)
        fake_kill.assert_not_called()

    def test_stop_sends_sigterm_and_removes_the_pidfile(self):
        postmaster.pidfile(self.p).parent.mkdir(parents=True, exist_ok=True)
        postmaster.pidfile(self.p).write_text("24680", encoding="utf-8")

        with mock.patch("os.kill") as fake_kill:
            rc, out = _read_stdout(postmaster.stop, self.p)

        self.assertEqual(rc, 0)
        self.assertIn("stopped", out)
        fake_kill.assert_called_once_with(24680, signal.SIGTERM)
        self.assertFalse(postmaster.pidfile(self.p).exists())

    def test_stop_tolerates_a_dead_pid_still_on_disk(self):
        # os.kill on an already-dead pid raises OSError; stop must swallow it and still
        # clean up the pidfile rather than crash.
        postmaster.pidfile(self.p).parent.mkdir(parents=True, exist_ok=True)
        postmaster.pidfile(self.p).write_text("999999", encoding="utf-8")

        with mock.patch("os.kill", side_effect=ProcessLookupError):
            rc, out = _read_stdout(postmaster.stop, self.p)

        self.assertEqual(rc, 0)
        self.assertFalse(postmaster.pidfile(self.p).exists())


# --- status ----------------------------------------------------------------------------
class TestStatus(PostmasterTest):
    def test_not_running_no_logfile_yet(self):
        rc, out = _read_stdout(postmaster.status, self.p)
        self.assertEqual(rc, 0)
        self.assertIn("not running", out)
        self.assertIn("never", out)

    def test_live_reports_the_pid(self):
        postmaster.pidfile(self.p).parent.mkdir(parents=True, exist_ok=True)
        postmaster.pidfile(self.p).write_text(str(os.getpid()), encoding="utf-8")
        rc, out = _read_stdout(postmaster.status, self.p)
        self.assertIn("LIVE", out)
        self.assertIn(str(os.getpid()), out)

    def test_status_reflects_the_log_contents(self):
        postmaster.log("postmaster up (tick 5s)", self.p)
        postmaster.log("reconcile failed: boom", self.p)
        postmaster.log("ring-catch: qa, rag", self.p)

        rc, out = _read_stdout(postmaster.status, self.p)
        self.assertIn("ring-catch: qa, rag", out)
        self.assertIn("reconcile errors seen: 1", out)
        self.assertIn("boom", out)


# --- _ring_catch: the per-tick core -----------------------------------------------------
class TestRingCatch(PostmasterTest):
    """Growth is keyed on `mailbox.total` (the append-only line count), NOT `unread` —
    see the fix in `_ring_catch`'s docstring. `unread` alone is not monotonic: it drops the
    instant the recipient reads, so a read and a fresh arrival landing in the same tick
    window could mask each other under an unread-only heuristic and strand a message
    permanently. These tests drive both signals independently to prove the fix."""

    def setUp(self):
        super().setUp()
        roster.mark_up("qa", self.p)
        roster.mark_up("rag", self.p)

    def _run(self, total_map, unread_map, last_total, ring_ok=None):
        ring_ok = ring_ok or {}
        rung_calls = []

        def fake_total(agent, p):
            return total_map.get(agent, 0)

        def fake_unread(agent, p):
            return unread_map.get(agent, 0)

        def fake_ring(agent, p):
            rung_calls.append(agent)
            return ring_ok.get(agent, True)

        with mock.patch.object(mailbox, "total", side_effect=fake_total), \
             mock.patch.object(mailbox, "unread", side_effect=fake_unread), \
             mock.patch.object(drive, "ring", side_effect=fake_ring):
            rung = postmaster._ring_catch(self.p, last_total)
        return rung, rung_calls

    def test_zero_growth_never_rings(self):
        last_total: dict = {}
        rung, calls = self._run({"qa": 0, "rag": 0}, {"qa": 0, "rag": 0}, last_total)
        self.assertEqual(rung, [])
        self.assertEqual(calls, [])
        self.assertEqual(last_total, {"qa": 0, "rag": 0})

    def test_growth_from_zero_rings_once(self):
        last_total: dict = {}
        rung, calls = self._run({"qa": 2, "rag": 0}, {"qa": 2, "rag": 0}, last_total)
        self.assertEqual(rung, ["qa"])
        self.assertEqual(calls, ["qa"])
        self.assertEqual(last_total["qa"], 2)

    def test_staying_at_the_same_total_does_not_re_ring(self):
        last_total = {"qa": 2, "rag": 0}
        rung, calls = self._run({"qa": 2, "rag": 0}, {"qa": 2, "rag": 0}, last_total)
        self.assertEqual(rung, [])
        self.assertEqual(calls, [])
        self.assertEqual(last_total["qa"], 2)

    def test_growth_with_nothing_unread_does_not_ring(self):
        """The total grew (mail was appended) but it was already read via the normal
        synchronous path between ticks — nothing left to catch, so no ring."""
        last_total = {"qa": 2}
        rung, calls = self._run({"qa": 3}, {"qa": 0}, last_total)
        self.assertEqual(rung, [])
        self.assertEqual(calls, [])
        self.assertEqual(last_total["qa"], 3)

    def test_a_read_and_a_new_arrival_in_the_SAME_tick_still_rings(self):
        """THE BUG THIS REPLACES: under an unread-only heuristic, unread going
        3 (stuck) -> 0 (agent drains it) -> 1 (new message, ring failed) all inside one
        tick window would compare the new 1 against the stale baseline 3, read 1 > 3 as
        false, and never catch the stranded message again. Keying growth on the
        append-only total instead: total went 3 -> 4 (one new message appended) regardless
        of the read in between, so growth is detected and, since unread is genuinely > 0,
        it rings.
        """
        last_total = {"qa": 3}
        rung, calls = self._run({"qa": 4}, {"qa": 1}, last_total)
        self.assertEqual(rung, ["qa"])
        self.assertEqual(calls, ["qa"])
        self.assertEqual(last_total["qa"], 4)

    def test_dropping_then_rising_again_rings_again(self):
        # total never drops (append-only) — model two SEPARATE ticks where mail keeps
        # arriving after being read in between.
        last_total = {"qa": 2, "rag": 0}
        # tick: no new mail appended; whatever was there got read — no ring.
        rung, calls = self._run({"qa": 2, "rag": 0}, {"qa": 0, "rag": 0}, last_total)
        self.assertEqual(rung, [])
        self.assertEqual(last_total["qa"], 2)

        # next tick: one more message arrives (total 2 -> 3) and is still unread — rings.
        rung, calls = self._run({"qa": 3, "rag": 0}, {"qa": 1, "rag": 0}, last_total)
        self.assertEqual(rung, ["qa"])
        self.assertEqual(calls, ["qa"])
        self.assertEqual(last_total["qa"], 3)

    def test_last_total_updates_even_when_ring_fails(self):
        last_total: dict = {}
        rung, calls = self._run({"qa": 2, "rag": 0}, {"qa": 2, "rag": 0}, last_total,
                                 ring_ok={"qa": False})
        # drive.ring was attempted (growth happened) but returned False — not counted as rung.
        self.assertEqual(calls, ["qa"])
        self.assertEqual(rung, [])
        self.assertEqual(last_total["qa"], 2)

    def test_a_mailbox_total_exception_is_skipped_not_fatal(self):
        last_total: dict = {}

        def fake_total(agent, p):
            if agent == "qa":
                raise OSError("boom")
            return 1

        with mock.patch.object(mailbox, "total", side_effect=fake_total), \
             mock.patch.object(mailbox, "unread", return_value=1), \
             mock.patch.object(drive, "ring", return_value=True) as fake_ring:
            rung = postmaster._ring_catch(self.p, last_total)

        self.assertEqual(rung, ["rag"])
        self.assertNotIn("qa", last_total)   # skipped entirely, never recorded
        self.assertEqual(last_total["rag"], 1)
        fake_ring.assert_called_once_with("rag", self.p)

    def test_a_mailbox_unread_exception_after_growth_is_treated_as_nothing_unread(self):
        """Growth is detected via `total`, but the `unread` re-check right before ringing
        can itself fail (a transient read error) — it must not crash the tick or lose the
        agent from `last_total`; it should just skip ringing this round."""
        last_total = {"qa": 2}

        def fake_unread(agent, p):
            raise OSError("boom")

        with mock.patch.object(mailbox, "total", return_value=3), \
             mock.patch.object(mailbox, "unread", side_effect=fake_unread), \
             mock.patch.object(drive, "ring") as fake_ring:
            rung = postmaster._ring_catch(self.p, last_total)

        self.assertEqual(rung, [])
        fake_ring.assert_not_called()
        self.assertEqual(last_total["qa"], 3)


# --- loop(): one tick, then a clean exit -------------------------------------------------
class TestLoop(PostmasterTest):
    class FakeTime:
        """Stands in for the `time` module inside af.postmaster (see FakeClock in
        test_mailbox.py for why the whole module reference is swapped, not just `.sleep`
        patched: that would touch the real stdlib clock process-wide)."""

        def __init__(self, on_sleep):
            self.calls = 0
            self.on_sleep = on_sleep

        def sleep(self, seconds):
            self.calls += 1
            self.on_sleep(self.calls)

    def test_one_tick_then_exits_when_the_pidfile_disappears(self):
        pf = postmaster.pidfile(self.p)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="utf-8")

        def on_sleep(n):
            # The pidfile exists through the first iteration's check; it is gone by the
            # time the second sleep call returns, so the loop's very next check exits it.
            if n >= 2:
                pf.unlink(missing_ok=True)

        fake_time = self.FakeTime(on_sleep)

        with mock.patch.object(postmaster, "time", fake_time), \
             mock.patch.object(roster, "reconcile") as fake_reconcile, \
             mock.patch.object(postmaster, "_ring_catch", return_value=[]) as fake_ring_catch:
            rc = postmaster.loop(self.p)

        self.assertEqual(rc, 0)
        self.assertEqual(fake_time.calls, 2)
        fake_reconcile.assert_called_once_with(self.p)
        fake_ring_catch.assert_called_once()
        # _ring_catch's second positional arg is the per-agent last_total dict.
        self.assertEqual(fake_ring_catch.call_args.args[0], self.p)
        self.assertIsInstance(fake_ring_catch.call_args.args[1], dict)

    def test_reconcile_failure_does_not_crash_the_loop(self):
        pf = postmaster.pidfile(self.p)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="utf-8")

        def on_sleep(n):
            if n >= 2:
                pf.unlink(missing_ok=True)

        fake_time = self.FakeTime(on_sleep)

        with mock.patch.object(postmaster, "time", fake_time), \
             mock.patch.object(roster, "reconcile", side_effect=RuntimeError("boom")), \
             mock.patch.object(postmaster, "_ring_catch", return_value=[]) as fake_ring_catch:
            rc = postmaster.loop(self.p)

        self.assertEqual(rc, 0)
        fake_ring_catch.assert_called_once()

    def test_pidfile_gone_before_the_first_sleep_returns_exits_with_zero_ticks_of_work(self):
        # The pidfile never existed at all: the very first check fails and nothing else runs.
        def on_sleep(n):
            pass

        fake_time = self.FakeTime(on_sleep)
        with mock.patch.object(postmaster, "time", fake_time), \
             mock.patch.object(roster, "reconcile") as fake_reconcile, \
             mock.patch.object(postmaster, "_ring_catch") as fake_ring_catch:
            rc = postmaster.loop(self.p)

        self.assertEqual(rc, 0)
        self.assertEqual(fake_time.calls, 1)
        fake_reconcile.assert_not_called()
        fake_ring_catch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
