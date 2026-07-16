"""squad.json — the one mutable source of truth for a team.

Everything here runs against a temp AF_ROOT / AF_SPECROOT (via TempFactory), so the durable
squad file lands under the temp spec home and nothing touches the real ~/.claude. The
reconcile tests fake tmux/live/mailbox rather than reaching for a live session: reconcile
imports them locally (`from . import tmux, live, mailbox`), so patching the module attributes
is what the running code actually calls.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import roster


# --- the dataclass model, no disk ---------------------------------------------------
class TestStationFromDict(unittest.TestCase):
    def test_missing_keys_default_and_non_int_falls_back_to_zero(self):
        st = roster.Station.from_dict({"name": "qa"})
        self.assertEqual(st.name, "qa")
        self.assertEqual(st.role, "")
        self.assertEqual(st.status, roster.PLANNED)
        self.assertEqual(st.spawned, 0)
        self.assertEqual(st.ctx_tokens, 0)
        self.assertEqual(st.unread, 0)

        junk = roster.Station.from_dict(
            {"name": "qa", "spawned": "soon", "ctx_tokens": None, "unread": "lots"})
        self.assertEqual(junk.spawned, 0)
        self.assertEqual(junk.ctx_tokens, 0)
        self.assertEqual(junk.unread, 0)

    def test_int_like_strings_are_accepted(self):
        st = roster.Station.from_dict({"name": "qa", "spawned": "42", "ctx_tokens": "1000"})
        self.assertEqual(st.spawned, 42)
        self.assertEqual(st.ctx_tokens, 1000)


class TestSquadFromDict(unittest.TestCase):
    def test_name_is_backfilled_from_the_dict_key(self):
        sq = roster.Squad.from_dict({"agents": {"orc": {"role": "orchestrator"}}}, "aftest")
        self.assertIn("orc", sq.agents)
        self.assertEqual(sq.agents["orc"].name, "orc")     # inner "name" was absent
        self.assertEqual(sq.agents["orc"].role, "orchestrator")

    def test_inner_name_wins_when_present(self):
        sq = roster.Squad.from_dict({"agents": {"orc": {"name": "orc", "role": "r"}}}, "aftest")
        self.assertEqual(sq.agents["orc"].name, "orc")

    def test_a_non_dict_becomes_an_empty_squad_for_the_slug(self):
        sq = roster.Squad.from_dict([1, 2, 3], "aftest")
        self.assertEqual(sq.slug, "aftest")
        self.assertEqual(sq.agents, {})


# --- persistence: round-trip, partial update, blank-sid guard -----------------------
class TestPersistence(TempFactory):
    def test_upsert_round_trips_every_field_through_disk(self):
        roster.upsert(
            self.p, name="qa", role="qa", parent="orc", model="opus",
            delegate="advised", spawn_flags="--x", settings_path="/s.json",
            live_sid="sid-1", status=roster.ALIVE, spawned=123,
            ctx_tokens=4000, unread=2)

        st = roster.load(self.p).agents["qa"]
        self.assertEqual(st, roster.Station(
            name="qa", role="qa", parent="orc", model="opus", delegate="advised",
            spawn_flags="--x", settings_path="/s.json", live_sid="sid-1",
            status=roster.ALIVE, spawned=123, ctx_tokens=4000, unread=2))

    def test_partial_update_does_not_clobber_other_fields(self):
        roster.upsert(self.p, name="qa", role="qa", live_sid="sid-1", status=roster.ALIVE)
        roster.upsert(self.p, name="qa", ctx_tokens=9999)      # a reconcile-shaped touch

        st = roster.load(self.p).agents["qa"]
        self.assertEqual(st.ctx_tokens, 9999)
        self.assertEqual(st.role, "qa")                       # survived
        self.assertEqual(st.live_sid, "sid-1")                # survived
        self.assertEqual(st.status, roster.ALIVE)              # survived

    def test_set_live_sid_ignores_a_blank_sid(self):
        roster.set_live_sid("qa", "sid-1", self.p)
        roster.set_live_sid("qa", "", self.p)                  # a transient read miss
        self.assertEqual(roster.get("qa", self.p).live_sid, "sid-1")

    def test_set_live_sid_advances_on_a_real_sid(self):
        roster.set_live_sid("qa", "sid-1", self.p)
        roster.set_live_sid("qa", "sid-2", self.p)
        self.assertEqual(roster.get("qa", self.p).live_sid, "sid-2")


# --- mark_down / remove -------------------------------------------------------------
class TestMarkDown(TempFactory):
    def test_mark_down_with_a_sid_records_down_and_writes_the_sid(self):
        roster.upsert(self.p, name="qa", role="qa", live_sid="old", status=roster.ALIVE)
        roster.mark_down("qa", "captured-at-kill", self.p)

        st = roster.get("qa", self.p)
        self.assertEqual(st.status, roster.DOWN)
        self.assertEqual(st.live_sid, "captured-at-kill")
        self.assertEqual(st.role, "qa")                       # unrelated fields survive

    def test_mark_down_with_none_keeps_the_stored_sid(self):
        roster.upsert(self.p, name="qa", live_sid="the-resume-record", status=roster.ALIVE)
        roster.mark_down("qa", None, self.p)

        st = roster.get("qa", self.p)
        self.assertEqual(st.status, roster.DOWN)
        self.assertEqual(st.live_sid, "the-resume-record")    # not erased

    def test_mark_down_on_an_unknown_station_creates_a_down_record(self):
        roster.mark_down("ghost", "s", self.p)
        st = roster.get("ghost", self.p)
        self.assertEqual(st.status, roster.DOWN)
        self.assertEqual(st.live_sid, "s")


class TestRemove(TempFactory):
    def test_remove_drops_the_station_entirely(self):
        roster.upsert(self.p, name="qa", role="qa")
        roster.upsert(self.p, name="orc", role="orchestrator")
        roster.remove("qa", self.p)

        agents = roster.load(self.p).agents
        self.assertNotIn("qa", agents)
        self.assertIn("orc", agents)                          # remove is targeted

    def test_remove_is_distinct_from_mark_down(self):
        roster.upsert(self.p, name="qa", live_sid="s", status=roster.ALIVE)
        roster.mark_down("qa", None, self.p)
        self.assertIsNotNone(roster.get("qa", self.p))         # mark_down keeps the record
        roster.remove("qa", self.p)
        self.assertIsNone(roster.get("qa", self.p))            # remove drops it

    def test_removing_a_missing_station_is_a_no_op(self):
        roster.remove("nobody", self.p)                        # must not raise
        self.assertEqual(roster.load(self.p).agents, {})


# --- a missing or corrupt squad.json ------------------------------------------------
class TestLoadTolerance(TempFactory):
    def test_a_missing_file_loads_an_empty_squad(self):
        self.assertFalse(self.p.squad_file.exists())
        sq = roster.load(self.p)
        self.assertEqual(sq.slug, "aftest")
        self.assertEqual(sq.agents, {})

    def test_garbage_json_loads_an_empty_squad(self):
        self.p.specdir.mkdir(parents=True, exist_ok=True)
        self.p.squad_file.write_text("{ not json at all")
        sq = roster.load(self.p)
        self.assertEqual(sq.slug, "aftest")
        self.assertEqual(sq.agents, {})

    def test_a_half_written_object_loads_an_empty_squad(self):
        self.p.specdir.mkdir(parents=True, exist_ok=True)
        self.p.squad_file.write_text('{"slug": "aftest", "agents": {"qa": ')  # truncated
        self.assertEqual(roster.load(self.p).agents, {})


# --- the edit context manager -------------------------------------------------------
class TestEdit(TempFactory):
    def test_two_sequential_edits_accumulate_and_leave_no_tmp_behind(self):
        with roster.edit(self.p) as sq:
            sq.agents["qa"] = roster.Station(name="qa", role="qa")
        with roster.edit(self.p) as sq:
            sq.agents["qa"] = replace(sq.agents["qa"], ctx_tokens=5)

        st = roster.load(self.p).agents["qa"]
        self.assertEqual(st.role, "qa")                       # first edit
        self.assertEqual(st.ctx_tokens, 5)                    # second edit

        tmp = self.p.squad_file.with_suffix(".json.tmp")
        self.assertFalse(tmp.exists(), "an atomic write left its .json.tmp behind")
        self.assertTrue(self.p.squad_file.exists())

    def test_the_write_is_atomic_no_tmp_after_upsert(self):
        roster.upsert(self.p, name="qa", role="qa")
        self.assertFalse(self.p.squad_file.with_suffix(".json.tmp").exists())


# --- reconcile ----------------------------------------------------------------------
class TestReconcile(TempFactory):
    """reconcile corrects the stored roster against ground truth. We fake the three sources
    it reads — tmux liveness, the real (post-fork) sid, and unread mail — and check the four
    status transitions plus the rule that live_sid only advances to a running session."""

    ALIVE_NAMES = {"aliveOk", "aliveLimited"}

    def _fake_has_session(self, target):
        # target is p.session(name) == f"ai-{slug}-{name}"
        return any(target.endswith(f"-{n}") for n in self.ALIVE_NAMES)

    def _fake_live_sid(self, agent, p=None, ps_out=None):
        # Only ever called for an alive agent; it returns the freshly-read running sid.
        return "sid-running"

    def _fake_unread(self, agent, p=None):
        return 7

    def _seed(self):
        roster.upsert(self.p, name="aliveOk", role="qa", status=roster.DOWN,
                     live_sid="stale", spawned=100)
        roster.upsert(self.p, name="aliveLimited", status=roster.ALIVE, spawned=100)
        roster.upsert(self.p, name="goneSpawned", status=roster.ALIVE,
                     live_sid="resume-me", spawned=200)
        roster.upsert(self.p, name="gonePlanned", status=roster.PLANNED, spawned=0)

    def _reconcile(self):
        self._seed()
        # aliveLimited carries the usage-limit marker; the warden owns clearing it.
        self.p.limited("aliveLimited").parent.mkdir(parents=True, exist_ok=True)
        self.p.limited("aliveLimited").write_text("limited")
        from af import tmux, live, mailbox
        with mock.patch.object(tmux, "has_session", self._fake_has_session), \
             mock.patch.object(live, "live_sid", self._fake_live_sid), \
             mock.patch.object(mailbox, "unread", self._fake_unread):
            return roster.reconcile(self.p)

    def test_status_transitions(self):
        sq = self._reconcile()
        self.assertEqual(sq.agents["aliveOk"].status, roster.ALIVE)
        self.assertEqual(sq.agents["aliveLimited"].status, roster.LIMITED)
        self.assertEqual(sq.agents["goneSpawned"].status, roster.DOWN)
        self.assertEqual(sq.agents["gonePlanned"].status, roster.PLANNED)

    def test_live_sid_only_advances_to_a_running_session(self):
        sq = self._reconcile()
        # An alive agent's sid is corrected to the running one...
        self.assertEqual(sq.agents["aliveOk"].live_sid, "sid-running")
        # ...but a gone agent keeps its stored resume record.
        self.assertEqual(sq.agents["goneSpawned"].live_sid, "resume-me")

    def test_unread_is_reconciled_from_the_mailbox(self):
        sq = self._reconcile()
        self.assertEqual(sq.agents["aliveOk"].unread, 7)

    def test_reconcile_returns_what_was_persisted(self):
        sq = self._reconcile()
        # The return value is a fresh load() — it must match disk.
        self.assertEqual(sq.to_dict(), roster.load(self.p).to_dict())


# --- quarantine: a mutation must not silently clobber an unparseable file ------------
class TestQuarantine(TempFactory):
    """`load` (read-only) tolerates a corrupt file by returning empty. But a mutation seeds from
    `_load_for_edit`, which refuses to overwrite a corrupt file: it renames the bad bytes aside to
    `squad.json.bad-<ts>` (data preserved) and starts a fresh roster, so a garbage file can never
    persist an empty roster over real state."""

    GARBAGE = "}{ not json at all — real state that must not be clobbered"

    def _write_garbage(self):
        self.p.specdir.mkdir(parents=True, exist_ok=True)
        self.p.squad_file.write_text(self.GARBAGE, encoding="utf-8")

    def test_a_mutation_quarantines_a_corrupt_file_and_starts_fresh(self):
        self._write_garbage()
        roster.upsert(self.p, name="x")

        bad = list(self.p.specdir.glob("squad.json.bad-*"))
        self.assertEqual(len(bad), 1, "the unparseable file was not quarantined")
        self.assertEqual(bad[0].read_text(encoding="utf-8"), self.GARBAGE)  # original preserved

        sq = roster.load(self.p)                       # the fresh roster parses...
        self.assertIn("x", sq.agents)                 # ...and holds the new station
        self.assertEqual(sq.agents["x"].name, "x")

    def test_a_plain_load_never_quarantines(self):
        self._write_garbage()
        sq = roster.load(self.p)                        # read-only: tolerant, empty
        self.assertEqual(sq.agents, {})
        # A read must not rename anything — the bad file stays exactly where it is.
        self.assertFalse(list(self.p.specdir.glob("squad.json.bad-*")))
        self.assertEqual(self.p.squad_file.read_text(encoding="utf-8"), self.GARBAGE)


# --- mark_up: idempotent spawn stamp, owns status/spawned ---------------------------
class TestMarkUp(TempFactory):
    def test_first_call_stamps_spawned_and_sets_alive(self):
        st = roster.mark_up("qa", self.p)
        self.assertEqual(st.status, roster.ALIVE)
        self.assertGreater(st.spawned, 0)             # time.time() is real; just nonzero

    def test_second_call_does_not_restamp_spawned(self):
        first = roster.mark_up("qa", self.p).spawned
        self.assertGreater(first, 0)
        again = roster.mark_up("qa", self.p).spawned   # idempotent stamp
        self.assertEqual(again, first)

    def test_status_and_spawned_in_fields_are_ignored(self):
        # mark_up owns these two; a caller passing them must not override.
        roster.mark_up("qa", self.p)
        first = roster.get("qa", self.p).spawned
        st = roster.mark_up("qa", self.p, status=roster.DOWN, spawned=1)
        self.assertEqual(st.status, roster.ALIVE)      # not DOWN
        self.assertEqual(st.spawned, first)           # not 1

    def test_extra_fields_are_applied(self):
        st = roster.mark_up("qa", self.p, settings_path="/s.json",
                           live_sid="sid-1", role="qa")
        self.assertEqual(st.settings_path, "/s.json")
        self.assertEqual(st.live_sid, "sid-1")
        self.assertEqual(st.role, "qa")


# --- reconcile: DOWN vs PLANNED for a gone station, decided by the resume record -----
class TestReconcileDownVsPlanned(TempFactory):
    """When a station is not alive, reconcile must tell 'was spawned' (DOWN) from 'never spawned'
    (PLANNED). spawned==0 alone is not enough: a non-empty live_sid IS a resume record, so it
    reconciles to DOWN even when the spawn stamp was lost."""

    def _reconcile_all_gone(self):
        from af import tmux, live, mailbox
        with mock.patch.object(tmux, "has_session", lambda target: False), \
             mock.patch.object(live, "live_sid", lambda *a, **k: ""), \
             mock.patch.object(mailbox, "unread", lambda *a, **k: 0):
            return roster.reconcile(self.p)

    def test_a_gone_station_holding_a_live_sid_is_down_not_planned(self):
        # spawned==0 but a resume record survives → it WAS spawned → DOWN.
        roster.upsert(self.p, name="orphan", status=roster.ALIVE,
                     live_sid="resume-me", spawned=0)
        sq = self._reconcile_all_gone()
        self.assertEqual(sq.agents["orphan"].status, roster.DOWN)
        self.assertEqual(sq.agents["orphan"].live_sid, "resume-me")   # not blanked

    def test_a_gone_station_with_no_sid_and_no_stamp_is_planned(self):
        roster.upsert(self.p, name="planned", status=roster.PLANNED,
                     live_sid="", spawned=0)
        sq = self._reconcile_all_gone()
        self.assertEqual(sq.agents["planned"].status, roster.PLANNED)


# --- reconcile: a failing liveness probe on one station must not abort the rest ------
class TestReconcilePartialProgress(TempFactory):
    """The compute loop wraps each station's probe in try/except, so a probe that raises for one
    session degrades only that station — the others still reconcile."""

    def test_one_raising_probe_does_not_stop_the_others(self):
        roster.upsert(self.p, name="boom", status=roster.ALIVE,
                     live_sid="resume-boom", spawned=100)
        roster.upsert(self.p, name="ok", status=roster.ALIVE,
                     live_sid="stale", spawned=200)

        def flaky_has_session(target):
            if target.endswith("-boom"):
                raise RuntimeError("tmux probe blew up")
            return False                              # "ok" is gone

        from af import tmux, live, mailbox
        with mock.patch.object(tmux, "has_session", flaky_has_session), \
             mock.patch.object(live, "live_sid", lambda *a, **k: ""), \
             mock.patch.object(mailbox, "unread", lambda *a, **k: 0):
            sq = roster.reconcile(self.p)

        # "ok" still reconciled despite "boom"'s probe raising: gone + spawned → DOWN.
        self.assertEqual(sq.agents["ok"].status, roster.DOWN)
        self.assertEqual(sq.agents["ok"].live_sid, "stale")
        # "boom" survived the raise (its status fell back to its ALIVE/LIMITED prior).
        self.assertIn("boom", sq.agents)


if __name__ == "__main__":
    unittest.main()
