"""revive REFUSES rather than half-restoring.

Every degraded path — no spec, corrupt spec, spec without flags, settings whose hooks will
not execute — produces an agent that LOOKS healthy and has no wall. A refusal is
recoverable; a silently unwalled mini-orchestrator writing to your repo is not. AI_FORCE=1
is the only way past, and it says so out loud.

Nothing here spawns: lifecycle.up is mocked, so the assertion is always "did it reach the
launch, or did it refuse before it".
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import hooks, lifecycle, spec as af_spec


class ReviveMatrix(TempFactory):
    def setUp(self):
        super().setUp()
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.p.sid_file("w").write_text("11111111-2222-3333-4444-555555555555")
        # revive RESTORES a spec into this process's env (that is how `up` spawns the agent
        # under its role), so every one of these must be cleaned or it leaks into the next
        # test — an AF_ROLE=orchestrator left behind would silently change who sweeps.
        for k in ("AI_FORCE", "AI_CLAUDE_FLAGS", "AF_ROLE", "AF_PARENT", "AF_DELEGATE",
                  "AI_COMPACT_SOFT", "AI_COMPACT_HARD"):
            os.environ.pop(k, None)
            self.addCleanup(os.environ.pop, k, None)
        # The transcript still exists — otherwise every case below refuses for that reason
        # and proves nothing.
        self.log = mock.patch("af.lifecycle.manifest.session_log_exists",
                              return_value="/logs/x.jsonl")
        self.log.start()
        self.addCleanup(self.log.stop)
        self.up = mock.patch("af.lifecycle.up", return_value=0)
        self.mock_up = self.up.start()
        self.addCleanup(self.up.stop)

    def _spec(self, **kw):
        d = dict(slug=self.slug, name="w", cwd=str(self.root),
                 sid="11111111-2222-3333-4444-555555555555", spawned=0,
                 flags="--model sonnet --settings /tmp/s.json")
        d.update(kw)
        af_spec.write(af_spec.Spec(**d), self.p)

    def _settings(self, hook_path: str) -> str:
        f = self.root / "settings.json"
        f.write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write",
                                       "hooks": [{"command": f"{hook_path} --arg"}]}]}}))
        return str(f)

    def _hook(self, executable=True) -> str:
        h = self.root / "wall.sh"
        h.write_text("#!/bin/sh\nexit 0\n")
        h.chmod(0o755 if executable else 0o644)
        return str(h)

    # --- the refusals ------------------------------------------------------------
    def test_NO_SPEC_refuses(self):
        # Memory without a constitution is the wrong agent: it would come back with no role,
        # no delegate-wall and no reminder hook, and nothing in its output would say so.
        self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_NO_SPEC_with_AI_FORCE_revives_anyway(self):
        os.environ["AI_FORCE"] = "1"
        self.assertEqual(lifecycle.revive("w", p=self.p), 0)
        self.mock_up.assert_called_once()

    def test_CORRUPT_SPEC_refuses(self):
        self.p.specdir.mkdir(parents=True, exist_ok=True)
        self.p.spec_file("w").write_text("{not json")
        self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_SPEC_WITH_NO_FLAGS_refuses(self):
        # No flags means no model, no --settings and no system prompt — an agent that revives
        # bare while every other field looks right.
        self._spec(flags="")
        self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_a_BOGUS_ENV_KEY_in_a_spec_is_refused(self):
        # bash eval'd these as `export K=V`, where `AF_ROLE=w; rm -rf ~; X` lands on the left
        # of an `=` inside an eval. Python cannot be injected that way, but a spec carrying
        # such a key is a tampered file, and a tampered constitution is refused, not cleaned.
        self.p.specdir.mkdir(parents=True, exist_ok=True)
        self.p.spec_file("w").write_text(json.dumps({
            "slug": self.slug, "name": "w", "cwd": str(self.root),
            "sid": "11111111-2222-3333-4444-555555555555", "spawned": 0,
            "flags": "--model sonnet", "env": {"AF_ROLE=x; rm -rf ~; Y": "w"}, "ai_env": {},
        }))
        self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_NO_SESSION_at_all_refuses(self):
        self.p.sid_file("w").unlink()
        self.assertEqual(lifecycle.revive("nobody", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_a_PURGED_LOG_refuses(self):
        self._spec()
        with mock.patch("af.lifecycle.manifest.session_log_exists", return_value=None):
            self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_a_MANIFEST_ONLY_sid_with_no_spec_refuses(self):
        # The manifest is keyed on NAME ONLY. Run this from the wrong directory and it would
        # resurrect the real agent's memory into a differently-slugged session: no role, no
        # wall, and a fresh mailbox nobody reads.
        self.p.sid_file("w").unlink()
        with mock.patch("af.lifecycle.manifest.last_sid", return_value="dead-beef"):
            self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    # --- the hook wall -----------------------------------------------------------
    def test_delegate_REQUIRED_with_broken_hooks_refuses(self):
        # A hook that cannot execute fails OPEN: Claude Code prints an error and runs the
        # tool anyway. The wall would be a wall-shaped hole, and nothing would say so.
        st = self._settings(self._hook(executable=False))
        # chmod is attempted first, so make the repair impossible: point at a missing file.
        st = self._settings(str(self.root / "gone.sh"))
        self._spec(flags=f"--model sonnet --settings {st}",
                   env={"AF_ROLE": "worker", "AF_DELEGATE": "required"})
        self.assertEqual(lifecycle.revive("w", p=self.p), 1)
        self.mock_up.assert_not_called()

    def test_delegate_REQUIRED_with_broken_hooks_and_AI_FORCE_revives_UNWALLED(self):
        os.environ["AI_FORCE"] = "1"
        st = self._settings(str(self.root / "gone.sh"))
        self._spec(flags=f"--model sonnet --settings {st}",
                   env={"AF_ROLE": "worker", "AF_DELEGATE": "required"})
        self.assertEqual(lifecycle.revive("w", p=self.p), 0)
        self.mock_up.assert_called_once()

    def test_delegate_ADVISED_with_broken_hooks_is_revived_with_a_warning(self):
        # An advised station loses a nudge, not a guarantee — worth saying, not worth
        # blocking on.
        st = self._settings(str(self.root / "gone.sh"))
        self._spec(flags=f"--model sonnet --settings {st}",
                   env={"AF_ROLE": "worker", "AF_DELEGATE": "advised"})
        self.assertEqual(lifecycle.revive("w", p=self.p), 0)
        self.mock_up.assert_called_once()

    def test_a_MISSING_settings_file_is_REGENERATED(self):
        gone = str(self.root / "settings-gone.json")
        self._spec(flags=f"--model sonnet --settings {gone}",
                   env={"AF_ROLE": "worker", "AF_DELEGATE": "advised"})
        with mock.patch("af.lifecycle._regen_settings") as regen:
            lifecycle.revive("w", p=self.p)
        regen.assert_called_once()

    def _spawned_env(self) -> dict:
        """The env `up` was handed. NOT os.environ: revive restores a spec into a COPY, so two
        revives in one process cannot leak a role (or a delegate wall) into each other."""
        return self.mock_up.call_args.args[2]

    def test_a_HEALTHY_spec_revives_and_restores_the_role(self):
        st = self._settings(self._hook(executable=True))
        self._spec(flags=f"--model sonnet --settings {st}",
                   env={"AF_ROLE": "qa", "AF_PARENT": "orc", "AF_DELEGATE": "required"},
                   ai_env={"AI_COMPACT_SOFT": "80000"})
        self.assertEqual(lifecycle.revive("w", p=self.p), 0)
        self.mock_up.assert_called_once()
        e = self._spawned_env()
        # The role env is what the agent's HOOKS read to enforce the chain of command.
        self.assertEqual(e["AF_ROLE"], "qa")
        self.assertEqual(e["AF_PARENT"], "orc")
        self.assertEqual(e["AI_COMPACT_SOFT"], "80000")
        # `up` detects --resume and reuses the id rather than minting an empty log.
        self.assertIn("--resume 11111111-2222-3333-4444-555555555555", e["AI_CLAUDE_FLAGS"])
        self.assertIn("--settings", e["AI_CLAUDE_FLAGS"])

    def test_the_restored_role_does_NOT_leak_into_this_process(self):
        # bash could mutate its own env because a bash command is a fresh process. This module
        # is importable and long-lived (the warden calls in-process), so a leak here would let
        # the NEXT agent inherit this one's AF_DELEGATE=required — or lose it.
        st = self._settings(self._hook())
        self._spec(flags=f"--model sonnet --settings {st}",
                   env={"AF_ROLE": "qa", "AF_DELEGATE": "required"})
        lifecycle.revive("w", p=self.p)
        self.assertIsNone(os.environ.get("AF_ROLE"))
        self.assertIsNone(os.environ.get("AF_DELEGATE"))

    def test_operator_flags_win_by_coming_last(self):
        st = self._settings(self._hook())
        self._spec(flags=f"--model sonnet --settings {st}")
        os.environ["AI_CLAUDE_FLAGS"] = "--model opus"
        lifecycle.revive("w", p=self.p)
        flags = self._spawned_env()["AI_CLAUDE_FLAGS"]
        self.assertLess(flags.index("--model sonnet"), flags.index("--model opus"))


class HooksAreLiveChecked(TempFactory):
    """`ledger`'s wall column and revive's refusal both come from here — a LIVE check of the
    files on disk, never a copy of what the spec claims."""

    def test_a_settings_file_that_installs_NO_hooks_is_not_a_wall(self):
        f = self.root / "s.json"
        f.write_text(json.dumps({"hooks": {}}))
        self.assertFalse(hooks.hooks_ok(f))

    def test_a_missing_settings_file_is_not_a_wall(self):
        self.assertFalse(hooks.hooks_ok(self.root / "nope.json"))
        self.assertFalse(hooks.hooks_ok(""))

    def test_a_hook_that_cannot_execute_is_not_a_wall(self):
        h = self.root / "h.sh"
        h.write_text("#!/bin/sh\n")
        h.chmod(0o644)
        f = self.root / "s.json"
        f.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"command": str(h)}]}]}}))
        # chmod is attempted first: a merely-missing +x bit is repaired, not refused.
        self.assertTrue(hooks.hooks_ok(f))
        self.assertTrue(os.stat(h).st_mode & stat.S_IXUSR)

    def test_a_hook_whose_file_is_GONE_is_not_a_wall(self):
        f = self.root / "s.json"
        f.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"command": str(self.root / "gone.sh") + " --x"}]}]}}))
        self.assertFalse(hooks.hooks_ok(f, quiet=True))

    def test_a_real_wall_passes(self):
        h = self.root / "h.sh"
        h.write_text("#!/bin/sh\n")
        h.chmod(0o755)
        f = self.root / "s.json"
        f.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Write", "hooks": [{"command": f"{h} --guard"}]}]}}))
        self.assertTrue(hooks.hooks_ok(f))


if __name__ == "__main__":
    unittest.main()
