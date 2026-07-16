"""`af read-force` — the CLI entry point for the read-wall hook's one-shot escape hatch
(see test_hooks.py::ReadWall for the hook side).
"""

from __future__ import annotations

import unittest
from unittest import mock

from support import TempFactory   # imported first: puts the af package on sys.path

from af import __main__ as main_mod


def run(argv):
    return main_mod.main(argv)


class ReadForce(TempFactory):
    def test_dispatches_to_hooks_read_force(self):
        with mock.patch("af.hooks.read_force", return_value=0) as rf:
            rc = run(["read-force", "/some/file.py"])
        self.assertEqual(rc, 0)
        rf.assert_called_once_with("/some/file.py")


if __name__ == "__main__":
    unittest.main()
