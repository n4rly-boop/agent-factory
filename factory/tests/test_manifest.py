"""manifest.prune: collapse the append-only manifest to reality.

The manifest grows one row per spawn forever, and `last_sid`/`spawned_here` read last-wins,
so the staleness is invisible until you `revive` a name whose transcript was purged. Prune
makes the file say what it means: one row per (tool, name, cwd) — the most recent — and ONLY
while the session's transcript still exists. A row whose .jsonl is gone is un-revivable dead
weight and is dropped; a row whose transcript survives is NEVER dropped.

Both the manifest file (SPEC_HOME/manifest.tsv) and the transcript store (~/.claude/projects)
live in the real home dir, so every test here redirects af.paths.SPEC_HOME and
af.paths.PROJECTS into the temp factory. Nothing touches the real home dir or the network.
"""

from __future__ import annotations

import unittest
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import manifest
from af import paths as af_paths


class Prune(TempFactory):
    def setUp(self):
        super().setUp()
        # Redirect BOTH stores into the temp root: the manifest file (SPEC_HOME) and the
        # transcript tree (PROJECTS). Without this the test reads the developer's real home.
        self.spec_home = self.root / "spec_home"
        self.spec_home.mkdir(parents=True, exist_ok=True)
        self.projects = self.root / "projects"
        (self.projects / "-Users-x-proj").mkdir(parents=True, exist_ok=True)
        for patcher in (mock.patch.object(af_paths, "SPEC_HOME", self.spec_home),
                        mock.patch.object(af_paths, "PROJECTS", self.projects)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.p = af_paths.paths()          # re-resolve, so .manifest / .projects are the patched ones

    def _transcript(self, sid: str) -> None:
        """Make a session's transcript 'exist' by dropping <sid>.jsonl somewhere under
        p.projects — in a project subdir, so the walk's recursion is exercised."""
        (self.projects / "-Users-x-proj" / f"{sid}.jsonl").write_text("", encoding="utf-8")

    def test_collapse_duplicates_keeps_the_latest(self):
        # Two spawns of the same (tool, name, cwd): the newer sid supersedes the older. Both
        # transcripts survive, so the ONLY reason a row drops is the collapse — kept is one row.
        self._transcript("sid-old")
        self._transcript("sid-new")
        manifest.append("orc", "sid-old", cwd="/w", p=self.p)
        manifest.append("orc", "sid-new", cwd="/w", p=self.p)
        kept, dropped = manifest.prune(self.p, dry_run=True)
        self.assertEqual([r[3] for r in kept], ["sid-new"])
        self.assertEqual(dropped, [])

    def test_row_whose_transcript_is_gone_is_dropped(self):
        # No .jsonl for this sid anywhere under projects → un-revivable dead weight.
        manifest.append("ghost", "sid-gone", cwd="/w", p=self.p)
        kept, dropped = manifest.prune(self.p, dry_run=True)
        self.assertEqual(kept, [])
        self.assertEqual([r[2] for r in dropped], ["ghost"])

    def test_row_whose_transcript_survives_is_kept(self):
        # A row is NEVER dropped while its transcript survives, so nothing revivable is lost.
        self._transcript("sid-live")
        manifest.append("qa", "sid-live", cwd="/w", p=self.p)
        kept, dropped = manifest.prune(self.p, dry_run=True)
        self.assertEqual([r[2] for r in kept], ["qa"])
        self.assertEqual(dropped, [])

    def test_dry_run_leaves_the_file_byte_identical(self):
        # dry_run reports without touching the file — even when it WOULD change (a dead row is
        # present, so a non-dry run would rewrite). The bytes on disk must be exactly as seeded.
        self._transcript("sid-live")
        manifest.append("qa", "sid-live", cwd="/w", p=self.p)
        manifest.append("ghost", "sid-gone", cwd="/w", p=self.p)
        before = self.p.manifest.read_bytes()
        kept, dropped = manifest.prune(self.p, dry_run=True)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(self.p.manifest.read_bytes(), before)

    def test_non_dry_run_persists_exactly_the_kept_rows(self):
        # A non-dry prune atomically rewrites the file to the kept rows only; re-reading via
        # manifest.rows must show the survivor and not the dropped one.
        self._transcript("sid-live")
        manifest.append("qa", "sid-live", cwd="/w", p=self.p)
        manifest.append("ghost", "sid-gone", cwd="/w", p=self.p)
        kept, dropped = manifest.prune(self.p, dry_run=False)
        self.assertEqual([r[2] for r in kept], ["qa"])
        self.assertEqual([r[2] for r in dropped], ["ghost"])
        persisted = manifest.rows(self.p)
        self.assertEqual([r[2] for r in persisted], ["qa"])
        self.assertEqual([r[3] for r in persisted], ["sid-live"])


if __name__ == "__main__":
    unittest.main()
