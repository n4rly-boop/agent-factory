"""The statusline — and the one field it exists to smuggle out.

`rate_limits.five_hour.resets_at` is the exact epoch at which the account-wide limit lifts,
and a live session is the ONLY place it is visible: no CLI reports it. The statusline is
therefore the sole channel that carries it out to $STATE/limits.json, where the warden — a
shell loop that spends no tokens and so survives the limit — reads it. If this silently
fails to write, the warden knows WHO was cut off but not WHEN to wake them, and a rescuer
that guesses wakes the agent straight back into the wall.

So the tests are in two halves: the DROP must land (and land atomically), and the LINE must
print no matter what — an empty status line is a broken-looking TUI and a crash here surfaces
as a mysterious blank bar rather than as an error anyone can trace.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from support import TempFactory, FACTORY   # imported first: puts the af package on sys.path

from af import statusline

NOW = 1_700_000_000
HARNESS = {
    "model": {"display_name": "Opus 4.8"},
    "context_window": {"used_tokens": 45300},
    "rate_limits": {
        "five_hour": {"used_percentage": 16, "resets_at": NOW + 7530},   # 2h05m
        "seven_day": {"used_percentage": 48, "resets_at": NOW + 90000},
    },
}


class StatusLine(TempFactory):
    def run_line(self, payload, **env):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        out = io.StringIO()
        e = {"AF_ROOT": str(self.root), "AF_SLUG": self.slug, "AF_AGENT": "", "AF_ROLE": ""}
        e.update({k: str(v) for k, v in env.items()})
        with mock.patch.dict(os.environ, e), \
                mock.patch.object(sys, "stdin", io.StringIO(raw)), \
                mock.patch.object(sys, "stdout", out):
            rc = statusline.main([])
        return rc, out.getvalue()

    @property
    def limits(self):
        return self.root / ".ai" / self.slug / "limits.json"


# ======================================================================================
# the drop — the whole reason this file exists
# ======================================================================================
class TheDrop(StatusLine):
    def test_resets_at_reaches_disk(self):
        rc, line = self.run_line(HARNESS)
        self.assertEqual(rc, 0)
        d = json.loads(self.limits.read_text())
        self.assertEqual(d["rate_limits"]["five_hour"]["resets_at"], NOW + 7530)
        self.assertEqual(d["rate_limits"]["seven_day"]["resets_at"], NOW + 90000)

    def test_the_drop_is_stamped_so_the_warden_can_tell_stale_from_fresh(self):
        d = json.loads(self.run_line(HARNESS)[1] and self.limits.read_text()
                       or self.limits.read_text())
        self.assertAlmostEqual(d["seen"], int(time.time()), delta=5)

    def test_the_whole_rate_limits_block_is_kept_not_just_the_field_we_want(self):
        self.run_line(HARNESS)
        self.assertEqual(json.loads(self.limits.read_text())["rate_limits"],
                         HARNESS["rate_limits"])

    def test_the_write_is_atomic_so_the_watcher_never_reads_half_a_file(self):
        # The warden reads this file on a clock, from another process. A truncated write is a
        # JSON error in the one component that must not crash.
        self.run_line(HARNESS)
        self.assertTrue(self.limits.exists())
        self.assertFalse((self.root / ".ai" / self.slug / ".limits.json.tmp").exists(),
                         "the temp file must have been RENAMED over the target, not left")

    def test_the_state_dir_is_created_if_this_is_the_first_thing_to_run(self):
        self.assertFalse(self.limits.parent.exists() and self.limits.exists())
        self.run_line(HARNESS)
        self.assertTrue(self.limits.exists())

    def test_a_later_render_overwrites_an_earlier_one(self):
        # The limit is account-wide, so ONE file is the truth for the whole machine; whichever
        # agent rendered last wins, and that is fine — they all see the same number.
        self.run_line(HARNESS)
        later = json.loads(json.dumps(HARNESS))
        later["rate_limits"]["five_hour"]["resets_at"] = NOW + 99
        self.run_line(later)
        d = json.loads(self.limits.read_text())
        self.assertEqual(d["rate_limits"]["five_hour"]["resets_at"], NOW + 99)

    def test_no_rate_limits_in_the_payload_means_no_file(self):
        # Not an empty file, and not a file full of nulls: the warden treats "no limits.json"
        # as "nobody has seen a limit", which is the truth here.
        self.run_line({"model": {"display_name": "Opus"}})
        self.assertFalse(self.limits.exists())

    def test_an_unwritable_state_dir_costs_the_drop_but_never_the_line(self):
        with mock.patch.object(statusline, "save_limits", side_effect=OSError("read-only")):
            rc, line = self.run_line(HARNESS, AF_AGENT="coder")
        self.assertEqual(rc, 0)
        self.assertIn("coder", line)

    def test_the_slug_falls_back_to_proj_exactly_as_the_bash_does(self):
        # statusline.sh defaults AF_SLUG to the literal "proj" and never derives it from the
        # cwd. Deriving it would drop the file in a state dir the warden does not read.
        with mock.patch.dict(os.environ, {"AF_SLUG": ""}):
            self.assertTrue(str(statusline._state()).endswith("/.ai/proj"))

    def test_save_limits_can_be_called_straight(self):
        statusline.save_limits({"five_hour": {"resets_at": 42}}, self.limits.parent)
        self.assertEqual(json.loads(self.limits.read_text())
                         ["rate_limits"]["five_hour"]["resets_at"], 42)


# ======================================================================================
# the line — it must ALWAYS print something
# ======================================================================================
class TheLine(StatusLine):
    def test_the_full_line(self):
        # main() renders against the REAL clock (it is the live status bar), so anchor the
        # reset to now+2h05m rather than to the fixture's frozen NOW.
        live = json.loads(json.dumps(HARNESS))
        live["rate_limits"]["five_hour"]["resets_at"] = int(time.time()) + 7530
        rc, line = self.run_line(live, AF_AGENT="coder", AF_ROLE="worker")
        self.assertEqual(line, "coder (worker) | Opus 4.8 | 45k | 5h 16% · 2h05m left")

    def test_the_countdown_is_rendered_from_resets_at(self):
        d = dict(HARNESS, rate_limits={"five_hour": {"used_percentage": 90,
                                                     "resets_at": NOW + 3661}})
        self.assertIn("5h 90% · 1h01m left", statusline.render(d, now=NOW))

    def test_a_limit_already_lifted_does_not_count_backwards(self):
        d = {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": NOW - 500}}}
        self.assertIn("0h00m left", statusline.render(d, now=NOW))

    def test_a_percentage_without_a_reset_time_still_renders(self):
        d = {"rate_limits": {"five_hour": {"used_percentage": 5}}}
        self.assertIn("5h 5%", statusline.render(d, now=NOW))
        self.assertNotIn("left", statusline.render(d, now=NOW))

    def test_zero_percent_is_not_absent(self):
        # `if pct is not None`, not `if pct` — a fresh session at 0% must still show the meter.
        d = {"rate_limits": {"five_hour": {"used_percentage": 0}}}
        self.assertIn("5h 0%", statusline.render(d, now=NOW))

    def test_the_agent_name_is_always_there_even_with_nothing_else(self):
        with mock.patch.dict(os.environ, {"AF_AGENT": "coder", "AF_ROLE": ""}):
            self.assertEqual(statusline.render({}, now=NOW), "coder")

    def test_an_unnamed_session_is_called_agent(self):
        with mock.patch.dict(os.environ, {"AF_AGENT": "", "AF_ROLE": ""}):
            self.assertEqual(statusline.render({}, now=NOW), "agent")

    def test_garbage_on_stdin_still_prints_a_line(self):
        self.assertEqual(self.run_line("this is not json"), (0, "agent"))

    def test_empty_stdin_still_prints_a_line(self):
        self.assertEqual(self.run_line(""), (0, "agent"))

    def test_a_json_array_still_prints_a_line(self):
        self.assertEqual(self.run_line("[1,2,3]"), (0, "agent"))

    def test_a_malformed_rate_limits_block_costs_the_line_but_not_the_process(self):
        # Whatever happens, a status line. A crash here is a blank bar nobody can trace.
        rc, line = self.run_line({"rate_limits": ["not", "a", "dict"]}, AF_AGENT="coder")
        self.assertEqual(rc, 0)
        self.assertTrue(line)

    def test_it_prints_no_trailing_newline(self):
        # Claude Code renders this into a one-line bar.
        self.assertFalse(self.run_line(HARNESS)[1].endswith("\n"))

    def test_the_context_size_is_read_from_either_spelling(self):
        self.assertIn("45k", statusline.render({"context_window": {"used_tokens": 45300}}))
        self.assertIn("45k", statusline.render({"context_window": {"used": 45300}}))


# ======================================================================================
# the bash is still live: whichever renders last writes the file both halves read
# ======================================================================================
@unittest.skipUnless((FACTORY / "statusline.sh").exists(), "no bash half")
class BashParity(TempFactory):
    def both(self, payload, **env):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        outs = []
        for tag, cmd in (("bash", ["bash", str(FACTORY / "statusline.sh")]),
                         ("py", [sys.executable, "-m", "af.statusline"])):
            root = self.root / tag
            root.mkdir(exist_ok=True)
            e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                 "PYTHONPATH": str(FACTORY), "AF_ROOT": str(root), "AF_SLUG": self.slug}
            e.update({k: str(v) for k, v in env.items()})
            r = subprocess.run(cmd, input=raw, capture_output=True, text=True, env=e,
                               cwd=str(FACTORY))
            f = root / ".ai" / self.slug / "limits.json"
            outs.append((r.stdout, json.loads(f.read_text()) if f.exists() else None))
        return outs

    def test_the_line_and_the_drop_are_the_same_from_both_halves(self):
        (bl, bf), (pl, pf) = self.both(HARNESS, AF_AGENT="coder", AF_ROLE="worker")
        self.assertEqual(bl, pl)
        self.assertEqual(bf["rate_limits"], pf["rate_limits"])
        self.assertEqual(sorted(bf), sorted(pf), "the warden parses these KEYS")

    def test_both_survive_garbage(self):
        (bl, bf), (pl, pf) = self.both("not json")
        self.assertEqual(bl, pl)
        self.assertEqual((bf, pf), (None, None))

    def test_neither_writes_a_limits_file_without_rate_limits(self):
        (bl, bf), (pl, pf) = self.both({"model": {"display_name": "Opus"}}, AF_AGENT="x")
        self.assertEqual(bl, pl)
        self.assertEqual((bf, pf), (None, None))


if __name__ == "__main__":
    unittest.main()
