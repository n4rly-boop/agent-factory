"""session-start — the SessionStart hook that keeps sid-<agent> honest.

Claude Code forks the session on --resume, so the id written once at spawn names a frozen
transcript the moment the agent is resumed. This hook fires INSIDE the session with its real,
live id and rewrites the sid file, so probe/sweep/warden stop reading a dead context number.

It is informational: Claude Code ignores its output, so it must never fail the session. A
missing session_id, blank stdin, malformed JSON — it writes nothing and returns 0. The one
thing it always does on a real id is lowercase it, because every other reader compares
lowercased.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock

from support import TempFactory   # imported first: puts the af package on sys.path

from af import hooks


class SessionStart(TempFactory):
    def start(self, payload, agent="orchestrator"):
        """Run session_start with a fake stdin and a controlled AF_AGENT. `agent=None` leaves
        AF_AGENT unset so the default is exercised."""
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        env = {"AF_ROOT": str(self.root), "AF_SLUG": self.slug}
        if agent is not None:
            env["AF_AGENT"] = agent
        else:
            env["AF_AGENT"] = ""
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdin", io.StringIO(raw)):
            return hooks.session_start()

    def sid_file(self, agent="orchestrator"):
        return self.root / ".ai" / self.slug / f"sid-{agent}"

    def test_a_valid_session_id_is_written_lowercased(self):
        rc = self.start({"session_id": "AAAABBBB-CCCC-DDDD-EEEE-FFFF00001111"}, agent="coder")
        self.assertEqual(rc, 0)
        self.assertEqual(self.sid_file("coder").read_text(encoding="utf-8"),
                         "aaaabbbb-cccc-dddd-eeee-ffff00001111")

    def test_blank_stdin_writes_nothing(self):
        self.assertEqual(self.start("", agent="coder"), 0)
        self.assertFalse(self.sid_file("coder").exists())

    def test_whitespace_only_stdin_writes_nothing(self):
        self.assertEqual(self.start("   \n  ", agent="coder"), 0)
        self.assertFalse(self.sid_file("coder").exists())

    def test_malformed_json_writes_nothing(self):
        self.assertEqual(self.start("{not json at all", agent="coder"), 0)
        self.assertFalse(self.sid_file("coder").exists())

    def test_a_missing_session_id_key_writes_nothing(self):
        self.assertEqual(self.start({"other": "field"}, agent="coder"), 0)
        self.assertFalse(self.sid_file("coder").exists())

    def test_an_empty_session_id_value_writes_nothing(self):
        self.assertEqual(self.start({"session_id": ""}, agent="coder"), 0)
        self.assertFalse(self.sid_file("coder").exists())

    def test_AF_AGENT_unset_writes_to_sid_orchestrator(self):
        # An orchestrator session carries no AF_AGENT; its sid must still be kept honest.
        rc = self.start({"session_id": "DEADBEEF-0000-1111-2222-333344445555"}, agent=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sid_file("orchestrator").read_text(encoding="utf-8"),
                         "deadbeef-0000-1111-2222-333344445555")


if __name__ == "__main__":
    unittest.main()
