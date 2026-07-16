"""`af delegate` / `af read-force` — the CLI-level delegation levers.

`af delegate` is a thin wrapper around the delegate-to-local-model skill's agent.py: it must
build the right subprocess argv and relay its exit code, never re-implement the tool loop
that already lives there. `af read-force` is just the CLI entry point for the read-wall
hook's one-shot escape hatch (see test_hooks.py::ReadWall for the hook side).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from support import TempFactory   # imported first: puts the af package on sys.path

from af import __main__ as main_mod


def run(argv):
    return main_mod.main(argv)


class Delegate(TempFactory):
    def test_builds_the_expected_subprocess_argv(self):
        with mock.patch.object(main_mod, "DELEGATE_SKILL", "/skill/agent.py"), \
                mock.patch("os.path.isfile", return_value=True), \
                mock.patch("subprocess.run") as run_mock, \
                mock.patch("tempfile.mkstemp", return_value=(99, "/tmp/out.txt")), \
                mock.patch("os.close"):
            run_mock.return_value = mock.Mock(returncode=0)
            rc = run(["delegate", "do the thing", "--root", "/scratch"])
        self.assertEqual(rc, 0)
        argv = run_mock.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "/skill/agent.py")
        self.assertIn("--root", argv)
        self.assertEqual(argv[argv.index("--root") + 1], "/scratch")
        self.assertIn("--out", argv)
        self.assertEqual(argv[-1], "do the thing")
        self.assertNotIn("--think", argv)

    def test_think_flag_is_passed_through(self):
        with mock.patch.object(main_mod, "DELEGATE_SKILL", "/skill/agent.py"), \
                mock.patch("os.path.isfile", return_value=True), \
                mock.patch("subprocess.run") as run_mock, \
                mock.patch("tempfile.mkstemp", return_value=(99, "/tmp/out.txt")), \
                mock.patch("os.close"):
            run_mock.return_value = mock.Mock(returncode=0)
            run(["delegate", "do it", "--root", "/scratch", "--think"])
        self.assertIn("--think", run_mock.call_args.args[0])

    def test_relays_the_subprocess_exit_code(self):
        with mock.patch.object(main_mod, "DELEGATE_SKILL", "/skill/agent.py"), \
                mock.patch("os.path.isfile", return_value=True), \
                mock.patch("subprocess.run") as run_mock, \
                mock.patch("tempfile.mkstemp", return_value=(99, "/tmp/out.txt")), \
                mock.patch("os.close"):
            run_mock.return_value = mock.Mock(returncode=7)
            rc = run(["delegate", "x", "--root", "/scratch"])
        self.assertEqual(rc, 7)

    def test_an_explicit_out_path_is_used_verbatim(self):
        with mock.patch.object(main_mod, "DELEGATE_SKILL", "/skill/agent.py"), \
                mock.patch("os.path.isfile", return_value=True), \
                mock.patch("subprocess.run") as run_mock, \
                mock.patch("tempfile.mkstemp") as mkstemp:
            run_mock.return_value = mock.Mock(returncode=0)
            run(["delegate", "x", "/my/out.txt", "--root", "/scratch"])
        mkstemp.assert_not_called()
        self.assertIn("/my/out.txt", run_mock.call_args.args[0])

    def test_root_defaults_to_AF_WORK_not_the_whole_repo(self):
        with mock.patch.object(main_mod, "DELEGATE_SKILL", "/skill/agent.py"), \
                mock.patch("os.path.isfile", return_value=True), \
                mock.patch("subprocess.run") as run_mock, \
                mock.patch("tempfile.mkstemp", return_value=(99, "/tmp/out.txt")), \
                mock.patch("os.close"), \
                mock.patch.dict(os.environ, {"AF_WORK": "/agent/work"}):
            run_mock.return_value = mock.Mock(returncode=0)
            run(["delegate", "x"])
        argv = run_mock.call_args.args[0]
        self.assertEqual(argv[argv.index("--root") + 1], "/agent/work")

    def test_missing_skill_is_a_clean_error_not_a_crash(self):
        with mock.patch.object(main_mod, "DELEGATE_SKILL", "/nowhere/agent.py"), \
                mock.patch("subprocess.run") as run_mock:
            rc = run(["delegate", "x", "--root", "/scratch"])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()


class ReadForce(TempFactory):
    def test_dispatches_to_hooks_read_force(self):
        with mock.patch("af.hooks.read_force", return_value=0) as rf:
            rc = run(["read-force", "/some/file.py"])
        self.assertEqual(rc, 0)
        rf.assert_called_once_with("/some/file.py")


if __name__ == "__main__":
    unittest.main()
