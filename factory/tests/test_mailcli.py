"""af.mailcli must be mail.sh's CLI on the Python core — same flag grammar, same defaults,
same bytes on disk, same doorbell. Live agents call `bash $AF_MAIL send/read/...` by path,
so when mail.sh becomes a shim that execs this module, nothing they type may behave
differently.

The load-bearing proof is the interop one: `mailcli send` and `bash mail.sh send` with the
SAME arguments must land byte-identical mailbox records (id/ts aside) and mark the same
busy/idle state. tmux is mocked, so nothing here touches a live pane.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support  # noqa: F401  (puts factory/ on sys.path)
from af import mailbox, mailcli
from support import MAIL_SH, TempFactory


class MailcliTest(TempFactory):
    def run_cli(self, *argv, agent=None):
        """Invoke `python3 -m af.mailcli <argv>` in-process. `agent` sets AF_AGENT (mail.sh's
        SELF) for the call, as a real spawned agent's env would."""
        saved = self._patch_agent(agent)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = mailcli.main(list(argv))
        finally:
            self._patch_agent(saved, restore=True)
        return rc, buf.getvalue()

    def _patch_agent(self, agent, restore=False):
        import os
        prev = os.environ.get("AF_AGENT")
        if restore:
            if agent is None:
                os.environ.pop("AF_AGENT", None)
            else:
                os.environ["AF_AGENT"] = agent
            return None
        if agent is not None:
            os.environ["AF_AGENT"] = agent
        return prev


class TestSendParsing(MailcliTest):
    def test_default_kind_is_fyi(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "hello", agent="orc")
        m = mailbox.dump("qa", self.p)[0]
        self.assertEqual(m.kind, "fyi")
        self.assertEqual(m.body, "hello")
        self.assertEqual(m.frm, "orc")

    def test_from_flag_overrides_self(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--from", "rag", "hi", agent="orc")
        self.assertEqual(mailbox.dump("qa", self.p)[0].frm, "rag")

    def test_self_defaults_to_orchestrator_when_no_agent(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "hi", agent=None)
        self.assertEqual(mailbox.dump("qa", self.p)[0].frm, "orchestrator")

    def test_flags_then_body_with_a_leading_dash(self):
        # bash stops flag-parsing at the first non-flag word; a later --kind is body, not a flag.
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--kind", "task", "-x is broken --kind y",
                         agent="orc")
        m = mailbox.dump("qa", self.p)[0]
        self.assertEqual(m.kind, "task")
        self.assertEqual(m.body, "-x is broken --kind y")

    def test_multiword_body_is_joined(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "one", "two", "three", agent="orc")
        self.assertEqual(mailbox.dump("qa", self.p)[0].body, "one two three")

    def test_empty_to_is_refused(self):
        rc, out = self.run_cli("send", "hello", agent="orc")
        self.assertEqual(rc, 1)
        self.assertEqual(mailbox.stat(self.p), {})

    def test_empty_body_is_refused(self):
        rc, out = self.run_cli("send", "--to", "qa", agent="orc")
        self.assertEqual(rc, 1)
        self.assertEqual(mailbox.dump("qa", self.p), [])


class TestSideEffects(MailcliTest):
    def test_task_marks_recipient_busy(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--kind", "task", "do it", agent="orc")
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")
        self.assertEqual(mailbox.task_state("qa", self.p), "busy")

    def test_done_to_the_tasker_clears_it(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--kind", "task", "do it", agent="orc")
            self.run_cli("send", "--to", "orc", "--kind", "done", "done", agent="qa")
        self.assertEqual(mailbox.state_flag("qa", self.p), "idle")

    def test_fyi_touches_no_state(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--kind", "fyi", "note", agent="orc")
        self.assertEqual(mailbox.state_flag("qa", self.p), "")

    def test_send_rings_the_doorbell(self):
        with mock.patch("af.drive.ring", return_value=True) as ring:
            rc, out = self.run_cli("send", "--to", "qa", "--kind", "task", "hi", agent="orc")
        ring.assert_called_once()
        self.assertEqual(ring.call_args.args[0], "qa")
        self.assertIn("delivered", out)

    def test_queued_when_no_pane(self):
        with mock.patch("af.drive.ring", return_value=False):
            rc, out = self.run_cli("send", "--to", "qa", "hi", agent="orc")
        self.assertIn("QUEUED", out)


class TestReadUnreadDump(MailcliTest):
    def _seed(self, to="qa", n=1, frm="orc"):
        for i in range(n):
            mailbox.send(to, f"body{i}", kind="fyi", frm=frm, p=self.p)

    def test_read_prints_and_acks(self):
        self._seed(n=2)
        rc, out = self.run_cli("read", "--agent", "qa")
        self.assertEqual(rc, 0)
        self.assertIn("body0", out)
        self.assertIn("body1", out)
        self.assertEqual(mailbox.unread("qa", self.p), 0)

    def test_read_peek_does_not_ack(self):
        self._seed(n=1)
        self.run_cli("read", "--agent", "qa", "--peek")
        self.assertEqual(mailbox.unread("qa", self.p), 1)

    def test_read_default_agent_is_self(self):
        self._seed(to="orc", n=1)
        rc, out = self.run_cli("read", agent="orc")
        self.assertIn("body0", out)

    def test_read_no_mail(self):
        rc, out = self.run_cli("read", "--agent", "qa")
        self.assertEqual(rc, 0)
        self.assertIn("no new mail", out)

    def test_unread_count(self):
        self._seed(n=3)
        rc, out = self.run_cli("unread", "--agent", "qa")
        self.assertEqual(out.strip(), "3")

    def test_unread_positional_agent(self):
        self._seed(n=2)
        rc, out = self.run_cli("unread", "qa")
        self.assertEqual(out.strip(), "2")

    def test_dump_shows_all_without_acking(self):
        self._seed(n=2)
        rc, out = self.run_cli("dump", "qa")
        self.assertIn("body0", out)
        self.assertIn("body1", out)
        self.assertEqual(mailbox.unread("qa", self.p), 2)  # dump never acks

    def test_dump_empty(self):
        rc, out = self.run_cli("dump", "qa")
        self.assertIn("is empty", out)

    def test_ring_subcommand_exits_zero_even_with_no_pane(self):
        # mail.sh's dispatch is `ring && echo || echo` — 0 either way.
        with mock.patch("af.drive.ring", return_value=False):
            rc, out = self.run_cli("ring", "qa")
        self.assertEqual(rc, 0)
        self.assertIn("no live pane", out)


@unittest.skipUnless(MAIL_SH.is_file() and shutil.which("bash"), "mail.sh / bash not available")
class TestBashParity(MailcliTest):
    """The contract that matters: `mailcli send` and `bash mail.sh send` with the SAME
    arguments land byte-identical records (id/ts aside). This is what lets mail.sh become a
    shim that execs af.mailcli without a live agent noticing."""

    def bash_send(self, *args, agent=None):
        env = self.bash_env()
        if agent:
            env["AF_AGENT"] = agent
        else:
            env.pop("AF_AGENT", None)
        r = subprocess.run(["bash", str(MAIL_SH), "send", *args], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_send_lands_identical_bytes(self):
        # bash → mailbox 'qa'; mailcli with the SAME args → same box. Two records, compared.
        self.bash_send("--to", "qa", "--kind", "fyi", "тело письма", agent="orc")
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--kind", "fyi", "тело письма", agent="orc")

        bash_line, py_line = [json.loads(l)
                              for l in self.p.box("qa").read_text().splitlines()]
        self.assertEqual(list(bash_line.keys()), list(py_line.keys()))
        self.assertEqual(list(py_line.keys()), ["id", "ts", "from", "to", "kind", "body"])
        for k in ("from", "to", "kind", "body"):
            self.assertEqual(bash_line[k], py_line[k], k)
        self.assertIsInstance(py_line["ts"], int)
        self.assertIsInstance(bash_line["ts"], int)

    def test_task_side_effect_matches(self):
        # bash tasks 'qa' → busy. Same call through the CLI leaves the same flag.
        self.bash_send("--to", "qa", "--kind", "task", "go", agent="orc")
        self.assertEqual(mailbox.state_flag("qa", self.p), "busy")

    def test_default_kind_matches_bash(self):
        self.bash_send("--to", "qa", "no kind here", agent="orc")
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qb", "no kind here", agent="orc")
        self.assertEqual(mailbox.dump("qa", self.p)[0].kind, "fyi")
        self.assertEqual(mailbox.dump("qb", self.p)[0].kind, "fyi")

    def test_bash_reads_what_mailcli_sent(self):
        with mock.patch("af.drive.ring", return_value=False):
            self.run_cli("send", "--to", "qa", "--kind", "task", "из питона", agent="orc")
        env = self.bash_env()
        r = subprocess.run(["bash", str(MAIL_SH), "read", "--agent", "qa"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("из питона", r.stdout)


if __name__ == "__main__":
    unittest.main()
