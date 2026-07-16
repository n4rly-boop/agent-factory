"""ledger: the STATE column must be folded fresh out of the mail log on every read, never
pinned by the on-disk state-<agent> flag file — that flag can go permanently stale (an
agent that dies without going through `af down` leaves a stale "busy" flag with nothing
left to clean it up, since the reaper that used to do so was removed in a related change).

These tests exercise `ledger.ledger()` end to end (probe mocked, everything else real and
hermetic under TempFactory) and read the STATE column back out of the printed table —
there is no separate pure function to unit-test against, the fix IS the wiring between
mailbox.task_state() and the print loop.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import ledger as ledgermod
from af import mailbox
from af import spec as specmod
from af.probe import Probe


def _spec(name: str, p) -> None:
    specmod.write(specmod.Spec(
        slug=p.slug, name=name, cwd=str(p.cwd), sid="", spawned=0, flags=""), p)


def _probe(alive=True, phase="idle", ctx=1000, endturns=1) -> Probe:
    return Probe(alive=alive, phase=phase, ctx=ctx, endturns=endturns, inputbox="")


def _run_ledger(p) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ledgermod.ledger(p)
    assert rc == 0
    return buf.getvalue()


def _state_column(output: str, name: str) -> str:
    """The STATE token for `name`'s row. Fields are single tokens padded to fixed widths and
    separated by single spaces, so splitting on whitespace and indexing is robust to the
    padding without hard-coding column offsets: name(0) role(1) model(2) parent(3) ctx(4)
    unread(5) state(6) alive...(7+)."""
    for line in output.splitlines():
        tokens = line.split()
        if tokens and tokens[0] == name:
            return tokens[6]
    raise AssertionError(f"no row for {name!r} in:\n{output}")


class LedgerStateColumn(TempFactory):
    def setUp(self) -> None:
        super().setUp()
        for name in ("alice", "bob", "carol", "dave"):
            _spec(name, self.p)

    def test_a_stale_busy_flag_does_not_pin_the_display_on_task(self):
        # A task went out and a done came back — the mail log says idle. Then simulate the
        # crash that leaves the raw flag file behind anyway: nothing reaps it any more.
        mailbox.send(to="alice", body="do the thing", kind="task", frm="boss", p=self.p)
        mailbox.send(to="boss", body="done", kind="done", frm="alice", p=self.p)
        self.assertEqual(mailbox.state_flag("alice", self.p), "idle")
        self.p.task_flag("alice").write_text("busy")  # the stale crash leftover
        self.assertEqual(mailbox.state_flag("alice", self.p), "busy")  # flag still lies
        self.assertEqual(mailbox.task_state("alice", self.p), "idle")  # fold is not fooled

        probes = {"alice": _probe(), "bob": _probe(), "carol": _probe(), "dave": _probe()}
        with mock.patch("af.ledger.probe", side_effect=lambda n, pp: probes[n]):
            out = _run_ledger(self.p)
        self.assertEqual(_state_column(out, "alice"), "idle")

    def test_a_genuinely_busy_agent_shows_task(self):
        # A task went out and nothing has answered it yet.
        mailbox.send(to="bob", body="do the other thing", kind="task", frm="boss", p=self.p)
        self.assertEqual(mailbox.task_state("bob", self.p), "busy")

        probes = {"alice": _probe(), "bob": _probe(), "carol": _probe(), "dave": _probe()}
        with mock.patch("af.ledger.probe", side_effect=lambda n, pp: probes[n]):
            out = _run_ledger(self.p)
        self.assertEqual(_state_column(out, "bob"), "task")

    def test_generating_wins_over_the_mail_fold_regardless_of_task_state(self):
        # carol has no mail at all — task_state("carol") folds to "idle" — but her pane is
        # mid-generation right now, which this branch treats as busy no matter what the
        # mailbox says: `"busy" if pr.phase == "generating" else (...)`.
        self.assertEqual(mailbox.task_state("carol", self.p), "idle")

        probes = {"alice": _probe(), "bob": _probe(), "carol": _probe(phase="generating"),
                  "dave": _probe()}
        with mock.patch("af.ledger.probe", side_effect=lambda n, pp: probes[n]):
            out = _run_ledger(self.p)
        self.assertEqual(_state_column(out, "carol"), "busy")

    def test_a_down_agent_shows_an_empty_state(self):
        probes = {"alice": _probe(), "bob": _probe(), "carol": _probe(),
                  "dave": _probe(alive=False, ctx=None, endturns=0)}
        with mock.patch("af.ledger.probe", side_effect=lambda n, pp: probes[n]):
            out = _run_ledger(self.p)
        # ledger.py prints `state or '-'` — the internal variable is "", displayed as '-'.
        self.assertEqual(_state_column(out, "dave"), "-")


if __name__ == "__main__":
    unittest.main()
