"""squad.json ↔ lifecycle wiring: the durable roster is now on the up/down/revive path.

These are the INTEGRATION seams between af.lifecycle and af.roster, not the roster store in
isolation (that is test_roster.py) nor revive's refusal matrix (that is test_revive.py):

  * down()   captures the LIVE (post-fork) session id via af.live.live_sid BEFORE it kills
             the pane, and writes it into the roster — so a killed-then-forked agent
             re-raises on the transcript that was actually growing, not the frozen parent.
  * revive() reads roster.get(name).live_sid FIRST in its sid-resolution chain, ahead of the
             once-written sid file and the spec's frozen spawn-time id.
  * up()     records the station in the roster, but ADDITIVELY: a roster-write failure must
             never fail a spawn (the sid file stays authoritative during the migration).

Nothing here launches: tmux is faked and, where the launch itself is not under test,
lifecycle.up is a spy. Everything is rooted in a temp AF_ROOT / AF_SPECROOT via TempFactory,
so the durable squad.json lands in the temp spec home and no real tmux/ps/claude is touched.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import lifecycle, spec as af_spec, roster
from af import paths as af_paths

FORK_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SPAWN_SID = "11111111-2222-3333-4444-555555555555"
SIDFILE_SID = "99999999-8888-7777-6666-555555555555"


class RosterWiring(TempFactory):
    def setUp(self):
        super().setUp()
        self.p.state.mkdir(parents=True, exist_ok=True)
        # revive restores a spec into a COPY of the env, but AI_FORCE / AI_CLAUDE_FLAGS are read
        # from os.environ — clear them so a value left by another test cannot change a decision.
        for k in ("AI_FORCE", "AI_CLAUDE_FLAGS", "AF_ROLE", "AF_PARENT", "AF_DELEGATE"):
            os.environ.pop(k, None)
            self.addCleanup(os.environ.pop, k, None)

    def _spec(self, **kw):
        """A HEALTHY spec so revive clears its refusal gates and reaches up(). No --settings, so
        there is no hooks_ok / regen path to fake — this suite is about the sid, not the wall."""
        d = dict(slug=self.slug, name="w", cwd=str(self.root), sid=SPAWN_SID,
                 spawned=0, flags="--model sonnet")
        d.update(kw)
        af_spec.write(af_spec.Spec(**d), self.p)

    # --- down() captures the live session before it kills the pane --------------------
    def test_down_captures_live_sid_before_kill(self):
        # The roster was seeded at spawn with the ORIGINAL id; the agent has since resumed, so
        # its live process is on a forked id. down() must record the fork, not the spawn id.
        roster.mark_up("w", self.p, live_sid=SPAWN_SID, role="worker")
        with mock.patch("af.live.live_sid", return_value=FORK_SID), \
                mock.patch("af.tmux.kill_session", return_value=True) as kill:
            self.assertEqual(lifecycle.down("w", self.p), 0)
            kill.assert_called_once()      # the pane really was (would have been) killed
        st = roster.get("w", self.p)
        self.assertEqual(st.status, roster.DOWN)
        self.assertEqual(st.live_sid, FORK_SID)          # the captured fork id …
        self.assertNotEqual(st.live_sid, SPAWN_SID)      # … not the frozen spawn id

    def test_down_with_blank_live_sid_preserves_the_stored_record(self):
        # live_sid can miss (ps race, process already reaped). A blank capture must NOT erase the
        # only resume record the roster holds — down leaves the stored live_sid intact.
        roster.mark_up("w", self.p, live_sid=SPAWN_SID, role="worker")
        with mock.patch("af.live.live_sid", return_value=""), \
                mock.patch("af.tmux.kill_session", return_value=True):
            self.assertEqual(lifecycle.down("w", self.p), 0)
        st = roster.get("w", self.p)
        self.assertEqual(st.status, roster.DOWN)
        self.assertEqual(st.live_sid, SPAWN_SID)          # unchanged

    # --- revive() prefers the roster's authoritative live_sid -------------------------
    def test_revive_prefers_roster_live_sid_over_the_sid_file(self):
        # The roster's live_sid is the post-fork truth; the sid file was written once at spawn
        # and rots on resume. When they disagree, revive must resume the ROSTER's session.
        self._spec()
        roster.mark_up("w", self.p, live_sid=FORK_SID, role="worker")
        self.p.sid_file("w").write_text(SIDFILE_SID)      # a DIFFERENT, stale id
        with mock.patch("af.lifecycle.manifest.session_log_exists", return_value=True), \
                mock.patch("af.lifecycle.up", return_value=0) as up:
            self.assertEqual(lifecycle.revive("w", p=self.p), 0)
        up.assert_called_once()
        flags = up.call_args.args[2]["AI_CLAUDE_FLAGS"]
        self.assertIn(f"--resume {FORK_SID}", flags)      # the roster won …
        self.assertNotIn(SIDFILE_SID, flags)              # … the sid file lost

    def test_revive_falls_through_to_the_sid_file_when_no_roster_station(self):
        # Back-compat: an agent spawned before the roster existed has no squad.json entry. The
        # roster lookup must return nothing and NOT crash — revive falls through to the sid file.
        self._spec()
        self.assertIsNone(roster.get("w", self.p))         # nothing on the roster
        self.p.sid_file("w").write_text(SIDFILE_SID)
        with mock.patch("af.lifecycle.manifest.session_log_exists", return_value=True), \
                mock.patch("af.lifecycle.up", return_value=0) as up:
            self.assertEqual(lifecycle.revive("w", p=self.p), 0)
        up.assert_called_once()
        flags = up.call_args.args[2]["AI_CLAUDE_FLAGS"]
        self.assertIn(f"--resume {SIDFILE_SID}", flags)   # the sid file, since the roster was empty

    # --- up() records the station, but a roster failure never fails the spawn ---------
    def test_up_survives_a_roster_write_failure(self):
        # The roster is an additive convenience view during the migration; the sid file stays
        # authoritative. So a roster.mark_up that throws must be swallowed — the launch itself
        # succeeded, and up() must still report 0.
        with mock.patch.object(af_paths, "SPEC_HOME", self.root / "spechome"), \
                mock.patch("af.lifecycle.tmux.kill_session", return_value=True), \
                mock.patch("af.lifecycle.tmux.new_session", return_value=True), \
                mock.patch("af.lifecycle.tmux.has_session", return_value=True), \
                mock.patch("af.lifecycle.mailbox.unread", return_value=0), \
                mock.patch("af.lifecycle.roster.mark_up",
                           side_effect=RuntimeError("roster is on fire")) as mu:
            self.assertEqual(lifecycle.up("w", self.p, env={}), 0)
            mu.assert_called_once()       # it really was attempted (and really did raise)


if __name__ == "__main__":
    unittest.main()
