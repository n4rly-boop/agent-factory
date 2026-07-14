"""The mailbox: the reliable channel. The cursor is the ack.

Everything here runs against a temp AF_ROOT. The bash interop tests shell out to the real
factory/mail.sh, but with AF_ROOT/AF_SLUG/AF_MAILROOT pointed at that same temp dir — the
slug is 'aftest', so mail.sh's doorbell resolves to a tmux session `ai-aftest-<agent>`
that does not exist, and `ring` bails before it can type anything anywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from support import MAIL_SH, TempFactory   # imported first: it puts the af package on sys.path

from af import mailbox


class FakeClock:
    """Stands in for the `time` module inside af.mailbox. Patching mailbox.time.time would
    swap the stdlib's clock process-wide; replacing the module reference does not."""

    def __init__(self, now: int):
        self.now = float(now)

    def time(self):
        return self.now

    def sleep(self, seconds):        # the lock's spin still sleeps for real
        time.sleep(seconds)


class MailTest(TempFactory):
    """A TempFactory with a clock we control: message ts has one-second resolution, and the
    interesting bugs all live in what happens inside one second."""

    def setUp(self):
        super().setUp()
        self.now = 1_700_000_000

    def send(self, to, body="body", kind="fyi", frm="orc", at=None):
        clock = FakeClock(at if at is not None else self.now)
        with mock.patch.object(mailbox, "time", clock):
            return mailbox.send(to, body, kind=kind, frm=frm, p=self.p)

    def tick(self, n=1):
        self.now += n


# --- the cursor is the ack ---------------------------------------------------------
class TestCursorAck(MailTest):
    def test_read_advances_the_cursor_and_is_not_redelivered(self):
        self.send("qa", "one")
        self.send("qa", "two")
        self.assertEqual(mailbox.unread("qa", self.p), 2)

        got = mailbox.read("qa", p=self.p)
        self.assertEqual([m.body for m in got], ["one", "two"])
        self.assertEqual(mailbox.unread("qa", self.p), 0)
        self.assertEqual(mailbox.read("qa", p=self.p), [])       # exactly once

        self.send("qa", "three")
        self.assertEqual(mailbox.unread("qa", self.p), 1)
        self.assertEqual([m.body for m in mailbox.read("qa", p=self.p)], ["three"])

    def test_peek_does_not_ack(self):
        self.send("qa", "one")
        self.assertEqual([m.body for m in mailbox.read("qa", peek=True, p=self.p)], ["one"])
        self.assertEqual(mailbox.unread("qa", self.p), 1)
        self.assertEqual([m.body for m in mailbox.read("qa", p=self.p)], ["one"])
        self.assertEqual(mailbox.unread("qa", self.p), 0)

    def test_unread_on_an_empty_or_missing_box(self):
        self.assertEqual(mailbox.unread("nobody", self.p), 0)
        self.p.box("empty").write_text("")
        self.assertEqual(mailbox.unread("empty", self.p), 0)
        self.assertEqual(mailbox.read("nobody", p=self.p), [])

    def test_cursor_ahead_of_a_truncated_box_does_not_go_negative(self):
        # The half of the claim that holds: unread never goes negative.
        self.send("qa", "one")
        self.send("qa", "two")
        mailbox.read("qa", p=self.p)
        self.p.box("qa").write_text("")            # box truncated, cursor file still says 2
        self.assertEqual(mailbox.unread("qa", self.p), 0)

    def test_FAILS_mail_sent_after_a_truncated_box_is_silently_swallowed(self):
        """FINDING (also true of mail.sh — this is bash parity, not a regression).

        _read_cursor() CLAMPS a too-far cursor but never writes the clamp back, so the
        stale cursor keeps swallowing. After a box truncation with a surviving cursor of
        N, the next N messages are dropped and never delivered by either half:

            send qa "one"; send qa "two"; read qa      -> cursor file = 2
            : > qa.jsonl                               -> box = 0 lines, cursor = 2
            send qa "after the purge"                  -> box = 1 line,  cursor = 2
            read qa                                    -> "no new mail"   (the message is gone)

        Reproduced against bash mail.sh verbatim. The docstring on _read_cursor says this
        case is handled ("no mail is ever delivered again — silently"); the negative unread
        is fixed, the silent loss is not.

        NOTE the fix is NOT "persist the clamp": by the time anything reads, the box already
        holds the new message, so the clamp computes min(2, 1) = 1 and read() still returns
        lines[1:1] = nothing. A cursor AHEAD of the box has to be read as evidence that the
        box was rotated — reset it to 0 (or fingerprint the box, e.g. inode+size, beside the
        cursor) instead of clamping it to the new length.
        """
        self.send("qa", "one")
        self.send("qa", "two")
        mailbox.read("qa", p=self.p)
        self.p.box("qa").write_text("")
        self.send("qa", "after the purge")
        self.assertEqual(mailbox.unread("qa", self.p), 1)
        self.assertEqual([m.body for m in mailbox.read("qa", p=self.p)], ["after the purge"])

    def test_dump_never_acks(self):
        self.send("qa", "one")
        self.assertEqual([m.body for m in mailbox.dump("qa", self.p)], ["one"])
        self.assertEqual(mailbox.unread("qa", self.p), 1)

    def test_stat(self):
        self.send("qa", "a")
        self.send("rag", "b")
        self.send("rag", "c")
        mailbox.read("qa", p=self.p)
        self.assertEqual(mailbox.stat(self.p), {"qa": 0, "rag": 2})

    def test_send_rejects_empty(self):
        with self.assertRaises(ValueError):
            mailbox.send("qa", "", p=self.p)
        with self.assertRaises(ValueError):
            mailbox.send("", "body", p=self.p)


# --- busy / idle -------------------------------------------------------------------
class TestTaskFlags(MailTest):
    def test_task_makes_the_recipient_busy(self):
        self.send("qa", "do the thing", kind="task", frm="orc")
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(self.p.tasker("qa").read_text(), "orc")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_done_to_the_tasker_makes_it_idle(self):
        self.send("qa", "do the thing", kind="task", frm="orc")
        self.tick()
        self.send("orc", "did it", kind="done", frm="qa")
        self.assertEqual(mailbox.state_flag("qa", self.p), "idle")
        self.assertEqual(mailbox.task_state("qa", self.p), "idle")

    def test_result_to_the_tasker_also_makes_it_idle(self):
        self.send("qa", "measure", kind="task", frm="orc")
        self.tick()
        self.send("orc", "42", kind="result", frm="qa")
        self.assertEqual(mailbox.state_flag("qa", self.p), "idle")
        self.assertEqual(mailbox.task_state("qa", self.p), "idle")

    def test_done_to_SOME_OTHER_peer_leaves_the_sender_busy(self):
        """Deliberate bash behaviour (mail.sh:194), and the Python must keep it: a side-reply
        to a peer must not mark an agent idle while its real task is still open."""
        self.send("qa", "do the thing", kind="task", frm="orc")
        self.tick()
        self.send("rag", "fyi, done with your side question", kind="done", frm="qa")
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_fyi_and_question_do_not_touch_state(self):
        self.send("qa", "task", kind="task", frm="orc")
        self.tick()
        self.send("orc", "quick question", kind="question", frm="qa")
        self.send("orc", "an fyi", kind="fyi", frm="qa")
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_no_mail_at_all_is_idle(self):
        self.assertEqual(mailbox.task_state("ghost", self.p), "idle")
        self.assertEqual(mailbox.state_flag("ghost", self.p), "")


# --- the fold vs the flag ----------------------------------------------------------
class TestTaskStateAgreesWithTheFlag(MailTest):
    """task_state() folds the answer out of the mail; state-<agent> is a cache of it that a
    crash or a purged /tmp can strand. Wherever both exist they must say the same thing."""

    def assertAgrees(self, agent, expected=None):
        flag = mailbox.state_flag(agent, self.p)
        fold = mailbox.task_state(agent, self.p)
        self.assertEqual(fold, flag, f"fold={fold!r} flag={flag!r} for {agent!r}")
        if expected:
            self.assertEqual(fold, expected)

    def test_long_sequence(self):
        self.send("qa", "t1", kind="task", frm="orc");      self.assertAgrees("qa", "busy")
        self.tick()
        self.send("orc", "progress", kind="fyi", frm="qa"); self.assertAgrees("qa", "busy")
        self.tick()
        self.send("orc", "d1", kind="done", frm="qa");      self.assertAgrees("qa", "idle")
        self.tick()
        self.send("qa", "t2", kind="task", frm="orc");      self.assertAgrees("qa", "busy")
        self.tick()
        self.send("orc", "d2", kind="result", frm="qa");    self.assertAgrees("qa", "idle")

    def test_two_taskers_interleaved(self):
        # orc tasks qa, then lead tasks qa. qa answers orc — the LEAD's task is still open,
        # so qa stays busy, and both the flag and the fold must know it.
        self.send("qa", "t-orc", kind="task", frm="orc")
        self.tick()
        self.send("qa", "t-lead", kind="task", frm="lead")
        self.assertAgrees("qa", "busy")
        self.assertEqual(self.p.tasker("qa").read_text(), "lead")
        self.tick()
        self.send("orc", "answering the OLD task", kind="done", frm="qa")
        self.assertAgrees("qa", "busy")
        self.tick()
        self.send("lead", "answering the live one", kind="done", frm="qa")
        self.assertAgrees("qa", "idle")

    def test_reply_to_a_peer_who_never_tasked_it(self):
        self.send("qa", "t", kind="task", frm="orc")
        self.tick()
        self.send("rag", "done with your favour", kind="done", frm="qa")
        self.assertAgrees("qa", "busy")
        self.tick()
        self.send("orc", "done for real", kind="done", frm="qa")
        self.assertAgrees("qa", "idle")

    def test_task_and_done_in_the_same_second(self):
        # Real tasks are minutes apart, but a fast agent can answer inside the same second.
        self.send("qa", "t", kind="task", frm="orc", at=self.now)
        self.send("orc", "d", kind="done", frm="qa", at=self.now)
        self.assertAgrees("qa", "idle")

    def test_two_tasks_in_the_same_second(self):
        self.send("qa", "t-orc", kind="task", frm="orc", at=self.now)
        self.send("qa", "t-lead", kind="task", frm="lead", at=self.now)
        self.assertAgrees("qa", "busy")

    def test_re_task_after_a_done_in_the_same_second_is_the_documented_blur(self):
        """The one place the fold and the flag CAN diverge, and mailbox.py says so out loud:
        "the only remaining blur is a `done` that closes an older task in the very second a
        new one arrives".

        task(orc→qa) / done(qa→orc) / task(orc→qa) all at ts T: the flag is busy (the last
        write was the new task), the fold sees a done from qa at ts >= T and calls it idle.
        Characterised, not fixed — a fix needs a per-message sequence the format does not
        carry. If this test ever starts failing, the format grew one.
        """
        t = self.now
        self.send("qa", "t1", kind="task", frm="orc", at=t)
        self.send("orc", "d1", kind="done", frm="qa", at=t)
        self.send("qa", "t2", kind="task", frm="orc", at=t)
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "idle")   # the blur

        # One second apart — a real line — and they agree again.
        self.send("qa", "t3", kind="task", frm="orc", at=t + 1)
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_the_fold_survives_a_purged_flag(self):
        # The flag is a cache. Delete it (a crash, a purged /tmp) and the fold still knows.
        self.send("qa", "t", kind="task", frm="orc")
        self.p.task_flag("qa").unlink()
        self.assertEqual(mailbox.state_flag("qa", self.p), "")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_the_fold_ignores_a_task_the_agent_merely_SENT(self):
        # orc sends a task TO qa. That does not make orc busy.
        self.send("qa", "t", kind="task", frm="orc")
        self.assertEqual(mailbox.task_state("orc", self.p), "idle")


# --- blobs -------------------------------------------------------------------------
class TestBlobs(MailTest):
    def test_small_body_stays_inline(self):
        m = self.send("qa", "short")
        line = json.loads(self.p.box("qa").read_text().splitlines()[0])
        self.assertEqual(line["body"], "short")
        self.assertNotIn("body_file", line)
        self.assertFalse(self.p.blob(m.id).exists())

    def test_large_cyrillic_body_spills_to_a_blob_and_round_trips(self):
        body = "Приборы врут успехом. " * 200           # ~4.4 KB of UTF-8
        self.assertGreater(len(body.encode("utf-8")), mailbox.BLOB_AT)
        m = self.send("qa", body, kind="task")

        raw = self.p.box("qa").read_text().splitlines()
        self.assertEqual(len(raw), 1, "the envelope must stay ONE physical line")
        line = json.loads(raw[0])
        self.assertNotIn("body", line)
        self.assertEqual(line["body_file"], str(self.p.blob(m.id)))
        self.assertLessEqual(len(raw[0].encode("utf-8")), mailbox.BLOB_AT)
        self.assertEqual(self.p.blob(m.id).read_text(encoding="utf-8"), body)

        # read() expands it, so the agent never has to go open a file.
        got = mailbox.read("qa", p=self.p)
        self.assertEqual([x.body for x in got], [body])

    def test_body_just_over_the_line_spills(self):
        body = "x" * (mailbox.BLOB_AT + 1)
        m = self.send("qa", body)
        self.assertTrue(self.p.blob(m.id).exists())
        self.assertEqual(mailbox.read("qa", p=self.p)[0].body, body)

    def test_a_missing_blob_is_reported_not_raised(self):
        body = "y" * 4000
        m = self.send("qa", body)
        self.p.blob(m.id).unlink()
        got = mailbox.read("qa", p=self.p)
        self.assertIn("body_file missing", got[0].body)

    def test_a_multiline_body_never_breaks_the_one_line_rule(self):
        body = "line one\nline two\nline three"
        self.send("qa", body)
        self.assertEqual(len(self.p.box("qa").read_bytes().splitlines()), 1)
        self.assertEqual(mailbox.read("qa", p=self.p)[0].body, body)

    def test_a_junk_line_in_the_box_is_skipped_not_fatal(self):
        self.send("qa", "good")
        with self.p.box("qa").open("a") as fh:
            fh.write("{not json at all\n")
        self.send("qa", "also good")
        got = mailbox.read("qa", p=self.p)
        self.assertEqual([m.body for m in got], ["good", "also good"])
        self.assertEqual(mailbox.unread("qa", self.p), 0)   # the junk line was still acked


# --- the lock ----------------------------------------------------------------------
class TestLock(MailTest):
    def test_a_held_lock_blocks_the_reader_until_it_is_released(self):
        """The reader must WAIT for a live lock — not reap it, and not sail through it.

        The budget below (100 spins × 0.02s = 2s) is deliberately far longer than the 0.15s
        the lock is held, and the test asserts the read finished well inside it: that is
        what tells a clean wait apart from a stale-reap, which would otherwise pass every
        assertion here while proving the opposite.
        """
        self.send("qa", "one")
        lock = self.p.mail_lock("qa")
        lock.mkdir(parents=True)

        held = threading.Event()
        blew_up = []

        def release():
            try:
                time.sleep(0.15)
                shutil.rmtree(lock)          # must exist: nobody else may touch it
                held.set()
            except Exception as e:           # a thread's exception cannot fail a test — record it
                blew_up.append(e)
                held.set()

        t = threading.Thread(target=release)
        started = time.monotonic()
        with mock.patch.object(mailbox, "LOCK_SPINS", 100), \
             mock.patch.object(mailbox, "LOCK_SLEEP", 0.02):
            t.start()
            got = mailbox.read("qa", p=self.p)
            elapsed = time.monotonic() - started
            budget = mailbox.LOCK_SPINS * mailbox.LOCK_SLEEP
        t.join(5)

        self.assertEqual(blew_up, [], "the reader reaped a lock that was still held")
        self.assertTrue(held.is_set())
        self.assertEqual([m.body for m in got], ["one"])
        self.assertGreaterEqual(elapsed, 0.15, "the reader did not wait for the lock")
        self.assertLess(elapsed, budget, "the reader stale-reaped instead of waiting")
        self.assertFalse(lock.exists(), "read() left the lock behind")

    def test_a_stale_lock_is_reaped_rather_than_wedging_the_mailbox_forever(self):
        self.send("qa", "one")
        lock = self.p.mail_lock("qa")
        lock.mkdir(parents=True)                       # nobody will ever release this

        with mock.patch.object(mailbox, "LOCK_SPINS", 3), \
             mock.patch.object(mailbox, "LOCK_SLEEP", 0.01):
            got = mailbox.read("qa", p=self.p)         # spins, gives up, takes the lock

        self.assertEqual([m.body for m in got], ["one"])
        self.assertFalse(lock.exists())
        self.assertEqual(mailbox.unread("qa", self.p), 0)

    def test_the_lock_is_released_even_when_there_is_no_mail(self):
        self.assertEqual(mailbox.read("qa", p=self.p), [])
        self.assertFalse(self.p.mail_lock("qa").exists())


# --- bash <-> python interop -------------------------------------------------------
@unittest.skipUnless(MAIL_SH.is_file() and shutil.which("bash"), "mail.sh / bash not available")
class TestBashInterop(MailTest):
    """Both halves of the system are live at once during the migration. They read and write
    the same files, so each must be able to decode the other's."""

    def mail_sh(self, *args, agent=None):
        env = self.bash_env()
        if agent:
            env["AF_AGENT"] = agent
        else:
            env.pop("AF_AGENT", None)
        r = subprocess.run(["bash", str(MAIL_SH), *args], env=env,
                           capture_output=True, text=True, timeout=60)
        # Without this, every interop test that asserts an ABSENCE of change stays green
        # when mail.sh does not run at all (syntax error, missing jq, bad env, timeout).
        self.assertEqual(r.returncode, 0,
                         f"mail.sh {' '.join(args)} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def test_bash_sends_python_reads(self):
        r = self.mail_sh("send", "--to", "qa", "--kind", "task", "тело письма", agent="orc")
        self.assertIn("id=", r.stdout, r.stdout + r.stderr)

        got = mailbox.read("qa", p=self.p)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].body, "тело письма")
        self.assertEqual(got[0].frm, "orc")
        self.assertEqual(got[0].to, "qa")
        self.assertEqual(got[0].kind, "task")
        self.assertIsInstance(got[0].ts, int)
        # And bash's busy bookkeeping is the one Python reads back.
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_python_sends_bash_reads(self):
        self.send("qa", "тело от питона", kind="task", frm="orc")
        r = self.mail_sh("read", "--agent", "qa")
        self.assertIn("тело от питона", r.stdout, r.stdout + r.stderr)
        self.assertIn("from: orc", r.stdout)
        # bash's read is an ack too — and Python must see the cursor it wrote.
        self.assertEqual(mailbox.unread("qa", self.p), 0)
        self.assertEqual(mailbox.read("qa", p=self.p), [])

    def test_python_reads_the_cursor_bash_wrote_and_vice_versa(self):
        self.send("qa", "one", frm="orc")
        self.send("qa", "two", frm="orc")
        self.mail_sh("read", "--agent", "qa")             # bash acks both
        self.assertEqual(mailbox.unread("qa", self.p), 0)

        self.send("qa", "three", frm="orc")
        self.assertEqual([m.body for m in mailbox.read("qa", p=self.p)], ["three"])  # python acks
        r = self.mail_sh("read", "--agent", "qa")
        self.assertIn("no new mail", r.stdout)            # bash agrees it was consumed

    def test_the_on_disk_keys_are_identical(self):
        self.mail_sh("send", "--to", "qa", "--kind", "task", "hello", agent="orc")
        self.send("qa", "hello", kind="task", frm="orc")
        bash_line, py_line = [json.loads(l) for l in self.p.box("qa").read_text().splitlines()]

        self.assertEqual(list(bash_line.keys()), list(py_line.keys()))
        self.assertEqual(list(py_line.keys()), ["id", "ts", "from", "to", "kind", "body"])
        for k in ("from", "to", "kind", "body"):
            self.assertEqual(bash_line[k], py_line[k])
        self.assertIsInstance(bash_line["ts"], int)
        self.assertIsInstance(py_line["ts"], int)         # a number, NOT a string
        self.assertTrue(py_line["id"].startswith("m-"))
        self.assertTrue(bash_line["id"].startswith("m-"))

    def test_bash_blob_is_read_by_python(self):
        body = "Ъ" * 3000
        self.mail_sh("send", "--to", "qa", body, agent="orc")
        line = json.loads(self.p.box("qa").read_text().splitlines()[0])
        self.assertIn("body_file", line)
        self.assertEqual(mailbox.read("qa", p=self.p)[0].body, body)

    def test_python_blob_is_read_by_bash(self):
        body = "Ж" * 3000
        m = self.send("qa", body, frm="orc")
        self.assertTrue(self.p.blob(m.id).exists())
        r = self.mail_sh("read", "--agent", "qa")
        self.assertIn(body, r.stdout, r.stdout[:400] + r.stderr[:400])

    def test_the_blob_keys_are_identical_too(self):
        self.mail_sh("send", "--to", "qa", "b" * 3000, agent="orc")
        self.send("qa", "b" * 3000, frm="orc")
        bash_line, py_line = [json.loads(l) for l in self.p.box("qa").read_text().splitlines()]
        self.assertEqual(list(bash_line.keys()), list(py_line.keys()))
        self.assertEqual(list(py_line.keys()), ["id", "ts", "from", "to", "kind", "body_file"])
        self.assertTrue(bash_line["body_file"].startswith(str(self.p.blobdir)))
        self.assertTrue(py_line["body_file"].startswith(str(self.p.blobdir)))

    def test_bash_done_to_the_tasker_clears_the_state_python_wrote(self):
        self.send("qa", "t", kind="task", frm="orc")            # python tasks qa
        self.mail_sh("send", "--to", "orc", "--kind", "done", "готово", agent="qa")  # bash answers
        self.assertEqual(mailbox.state_flag("qa", self.p), "idle")
        self.assertEqual(mailbox.task_state("qa", self.p), "idle")

    def test_bash_done_to_a_peer_leaves_the_sender_busy(self):
        self.send("qa", "t", kind="task", frm="orc")
        self.mail_sh("send", "--to", "rag", "--kind", "done", "side", agent="qa")
        # Prove the send really happened before reading anything into its non-effect.
        self.assertEqual([m.body for m in mailbox.dump("rag", self.p)], ["side"])
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")


if __name__ == "__main__":
    unittest.main()
