"""heal: diagnosing what broke on a squad, and the SAFE repairs.

The invariant these tests protect: a live claude is never killed, and a crashed one comes
back on its recorded session. `diagnose` is read-only and takes an injected `ps_out` and
`probe_fn`, so no process table and no live pane are touched. `repair` is exercised in
DRY-RUN only — the non-dry path spawns/kills real tmux sessions and has no place in a unit
test.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from support import TempFactory

from af import heal, squad


def _ps_line(slug: str, agent: str, *, session_id: str = "", resume: str = "") -> str:
    """One process line as `ps -A -o command=` would print it for an agent of a squad."""
    parts = ["claude"]
    if session_id:
        parts.append(f"--session-id {session_id}")
    if resume:
        parts.append(f"--resume /Users/x/.claude/projects/-x/{resume}.jsonl")
    parts.append(f"--settings /Users/x/.claude/agent-factory/lines/{slug}/settings-{agent}.json")
    return " ".join(parts)


IDLE = staticmethod(lambda name, p: SimpleNamespace(phase="idle"))


class Diagnose(TempFactory):
    slug = "aftest"

    def _write_sid(self, name: str, sid: str) -> None:
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.p.sid_file(name).write_text(sid, encoding="utf-8")

    def _good_settings(self, name: str) -> None:
        """A real settings file, hooks pointing at the repo's +x shims — including
        SessionStart. hooks_ok is True for it."""
        squad.write_settings(self.slug, name, self.p.settings_file(name))

    FORK = "7a612149-900e-4619-a020-c2d6d0636a04"
    PARENT = "11d8b710-ef92-456a-96db-59c316b9041b"

    def test_a_live_agent_whose_sid_file_names_the_parent_is_drift(self):
        self._write_sid("w", self.PARENT)
        self._good_settings("w")
        ps = "\n".join([
            _ps_line(self.slug, "w", session_id=self.PARENT),                 # launcher
            _ps_line(self.slug, "w", session_id=self.FORK, resume=self.PARENT),  # fork worker
        ])
        with mock.patch("af.tmux.has_session", return_value=True):
            f = heal.diagnose("w", self.p, ps, probe_fn=IDLE)
        self.assertTrue(f.claude_alive)
        self.assertTrue(f.session_alive)
        self.assertEqual(f.live_sid, self.FORK)
        self.assertTrue(f.drift)
        self.assertFalse(f.down)

    def test_a_live_agent_already_on_its_live_session_is_not_drift(self):
        self._write_sid("w", self.FORK)
        self._good_settings("w")
        ps = _ps_line(self.slug, "w", session_id=self.FORK, resume=self.PARENT)
        with mock.patch("af.tmux.has_session", return_value=True):
            f = heal.diagnose("w", self.p, ps, probe_fn=IDLE)
        self.assertFalse(f.drift)
        self.assertTrue(f.healthy)          # good settings + SessionStart + no drift + up

    def test_no_process_and_no_tmux_session_is_down(self):
        self._write_sid("w", self.PARENT)
        self._good_settings("w")
        with mock.patch("af.tmux.has_session", return_value=False):
            f = heal.diagnose("w", self.p, "", probe_fn=IDLE)   # empty ps → no live process
        self.assertFalse(f.claude_alive)
        self.assertFalse(f.session_alive)
        self.assertTrue(f.down)

    def test_a_session_that_survives_with_no_claude_is_crashed(self):
        # tmux session is up but no process for the agent → frozen pane, i.e. crashed.
        self._write_sid("w", self.PARENT)
        self._good_settings("w")
        other = _ps_line(self.slug, "other", session_id=self.FORK)   # some OTHER agent is alive
        with mock.patch("af.tmux.has_session", return_value=True):
            f = heal.diagnose("w", self.p, other, probe_fn=IDLE)
        self.assertTrue(f.session_alive)
        self.assertFalse(f.claude_alive)
        self.assertTrue(f.down)

    def test_settings_without_a_session_start_hook_are_flagged(self):
        self._write_sid("w", self.FORK)
        # Hand-write a settings file that installs a hook but NOT session-start.sh.
        stf = self.p.settings_file(str("w"))
        stf.parent.mkdir(parents=True, exist_ok=True)
        role = squad.ROLE_REMINDER
        stf.write_text(
            '{ "hooks": { "UserPromptSubmit": [ { "hooks": [ '
            f'{{ "type": "command", "command": "{role}" }} ] }} ] }} }}',
            encoding="utf-8")
        ps = _ps_line(self.slug, "w", session_id=self.FORK, resume=self.PARENT)
        with mock.patch("af.tmux.has_session", return_value=True):
            f = heal.diagnose("w", self.p, ps, probe_fn=IDLE)
        self.assertFalse(f.has_session_start)
        self.assertFalse(f.healthy)


class RepairDryRun(TempFactory):
    """Dry-run returns the actions it WOULD take, and touches nothing. The non-dry path is
    deliberately not exercised here."""

    slug = "aftest"
    FORK = "7a612149-900e-4619-a020-c2d6d0636a04"
    PARENT = "11d8b710-ef92-456a-96db-59c316b9041b"
    OPTS = heal.Options(dry_run=True)

    def _finding(self, **kw):
        base = dict(
            name="w", session_alive=True, claude_alive=True, file_sid=self.PARENT,
            live_sid=self.FORK, drift=False, settings_path="/x/settings-w.json",
            settings_ok=True, has_session_start=True, limits_seen=True, idle=True,
        )
        base.update(kw)
        return heal.Finding(**base)

    def test_drift_would_heal_the_sid(self):
        acts = heal.repair(self._finding(drift=True), self.slug, self.p, self.OPTS)
        self.assertTrue(any("WOULD heal sid" in a for a in acts))

    def test_a_crashed_agent_with_a_recorded_sid_would_be_revived(self):
        f = self._finding(session_alive=False, claude_alive=False, live_sid=None)
        acts = heal.repair(f, self.slug, self.p, self.OPTS)
        self.assertTrue(any("WOULD revive" in a for a in acts))
        self.assertFalse(any("FRESH" in a for a in acts))   # it HAS a sid — no memory loss

    def test_a_down_agent_with_no_recorded_sid_warns_about_memory_loss(self):
        f = self._finding(session_alive=False, claude_alive=False, live_sid=None, file_sid="")
        acts = heal.repair(f, self.slug, self.p, self.OPTS)
        self.assertTrue(any("FRESH" in a and "memory lost" in a for a in acts))

    def test_stale_settings_would_be_regenerated(self):
        f = self._finding(settings_ok=False, has_session_start=False)
        acts = heal.repair(f, self.slug, self.p, self.OPTS)
        self.assertTrue(any("WOULD regenerate settings" in a for a in acts))

    def test_a_healthy_finding_needs_no_repair(self):
        # repair is only called on non-healthy findings by heal(), but even if called it must
        # produce nothing for a clean agent.
        self.assertEqual(heal.repair(self._finding(), self.slug, self.p, self.OPTS), [])


if __name__ == "__main__":
    unittest.main()
