"""squad: blueprint loading, count expansion, parent defaulting, threshold + delegate
resolution. The fatal-typo case is the one that, when it was NOT fatal, produced a
silently unwalled agent.

No agent is spawned. The resolver is pure; the only I/O is reading a blueprint written
into a temp dir.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import roster, squad


def _bp(tmp, doc: dict):
    f = tmp / "bp.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return str(f)


class LoadJSON(unittest.TestCase):
    def test_valid_json_round_trips(self):
        self.assertEqual(squad.load_from_string('{"k": "v"}'), {"k": "v"})

    def test_invalid_json_is_fatal(self):
        with self.assertRaises(squad.SquadSpecError):
            squad.load_from_string("{ not json at all")

    def test_unreadable_path_is_fatal(self):
        with self.assertRaises(squad.SquadSpecError):
            squad.load("/no/such/blueprint.json")


class DelegateLevels(unittest.TestCase):
    def test_the_three_levels_and_their_aliases(self):
        self.assertEqual(squad.dlevel("required"), "required")
        self.assertEqual(squad.dlevel("hard"), "required")
        self.assertEqual(squad.dlevel("full"), "required")     # meant required before, still does
        self.assertEqual(squad.dlevel("advised"), "advised")
        self.assertEqual(squad.dlevel("advise"), "advised")
        self.assertEqual(squad.dlevel(True), "advised")        # bare `delegate: true` = advised
        self.assertEqual(squad.dlevel("no"), "")
        self.assertEqual(squad.dlevel(False), "")
        self.assertEqual(squad.dlevel(None, "advised"), "advised")   # default falls through

    def test_a_typo_is_FATAL(self):
        # The whole point. `delegate: requird` used to fall through to '' — no wall, no
        # advisory, no complaint — on a station meant to be walled. It must refuse.
        with self.assertRaises(squad.SquadSpecError):
            squad.dlevel("requird")
        with self.assertRaises(squad.SquadSpecError):
            squad.dlevel("blockk")


class Plan(TempFactory):
    def plan(self, doc: dict):
        return squad.plan(_bp(self.root, doc), cwd=str(self.root))

    def test_count_expands_sharing_one_brief(self):
        st = self.plan({
            "slug": "s",
            "agents": {"abl": {"count": 3, "role": "ablation", "brief": "one hypothesis"}},
        })
        self.assertEqual([s.name for s in st], ["abl1", "abl2", "abl3"])
        self.assertTrue(all(s.brief == "one hypothesis" for s in st))

    def test_no_count_keeps_the_bare_name(self):
        st = self.plan({"slug": "s", "agents": {"solo": {"role": "worker"}}})
        self.assertEqual([s.name for s in st], ["solo"])

    def test_parent_defaults_to_the_role_orchestrator_by_role_not_name(self):
        # Top station named 'boss', role orchestrator. Everyone else must report to 'boss',
        # not to the literal 'orc' that used to be hard-coded.
        st = {s.name: s for s in self.plan({
            "slug": "s",
            "agents": {"boss": {"role": "orchestrator"}, "w": {"role": "worker"}},
        })}
        self.assertEqual(st["boss"].parent, "")       # the orchestrator reports to no station
        self.assertEqual(st["w"].parent, "boss")

    def test_explicit_parent_wins(self):
        st = {s.name: s for s in self.plan({
            "slug": "s",
            "agents": {
                "boss": {"role": "orchestrator"},
                "a": {"parent": "boss"},
                "b": {"parent": "a"},
            },
        })}
        self.assertEqual(st["b"].parent, "a")

    def test_orchestrator_is_a_reserved_station_name(self):
        with self.assertRaises(squad.SquadSpecError):
            self.plan({"slug": "s", "agents": {"orchestrator": {"role": "worker"}}})

    def test_delegate_default_is_advised(self):
        st = self.plan({"slug": "s", "agents": {"w": {"role": "worker"}}})
        self.assertEqual(st[0].delegate, "advised")

    def test_defaults_delegate_flows_to_stations(self):
        st = {s.name: s for s in self.plan({
            "slug": "s",
            "defaults": {"delegate": "required"},
            "agents": {
                "a": {"role": "worker"},
                "b": {"role": "worker", "delegate": "no"},
            },
        })}
        self.assertEqual(st["a"].delegate, "required")   # inherited
        self.assertEqual(st["b"].delegate, "")           # per-agent override wins

    def test_a_typo_in_a_station_delegate_aborts_the_whole_plan(self):
        with self.assertRaises(squad.SquadSpecError):
            self.plan({"slug": "s", "agents": {"w": {"delegate": "requird"}}})

    def test_work_is_resolved_absolute(self):
        st = self.plan({"slug": "s", "work": "./out", "agents": {"w": {"role": "worker"}}})
        self.assertEqual(st[0].work, str(self.root / "out"))

    def test_thresholds_resolve_per_agent_over_defaults(self):
        st = {s.name: s for s in self.plan({
            "slug": "s",
            "defaults": {"compact_soft": 120000, "compact_hard": 400000},
            "agents": {
                "a": {"role": "worker"},
                "b": {"role": "worker", "compact_soft": 80000},
            },
        })}
        self.assertEqual((st["a"].soft, st["a"].hard), ("120000", "400000"))
        self.assertEqual(st["b"].soft, "80000")          # per-agent wins
        self.assertEqual(st["b"].hard, "400000")         # falls back to default

    def test_slug_defaults_to_the_cwd_basename(self):
        st = self.plan({"agents": {"w": {"role": "worker"}}})
        self.assertEqual(st[0].slug, self.root.name)

    def test_a_non_mapping_agent_value_is_fatal(self):
        with self.assertRaises(squad.SquadSpecError):
            self.plan({"slug": "s", "agents": {"w": "not a mapping"}})


class BulkLines(TempFactory):
    def test_bulk_lines_read_from_defaults_only(self):
        f = _bp(self.root, {"defaults": {"bulk_lines": 25}, "agents": {"w": {"role": "worker"}}})
        self.assertEqual(squad.bulk_lines(f), 25)

    def test_top_level_bulk_lines_is_ignored_as_in_bash(self):
        # A top-level `bulk_lines:` was silently ignored by the bash (it read
        # defaults.bulk_lines). Kept, not fixed — the hooks read AF_BULK_LINES from the env
        # `up` fills, and both sides must agree on where the number lives.
        f = _bp(self.root, {"bulk_lines": 999, "agents": {"w": {"role": "worker"}}})
        self.assertEqual(squad.bulk_lines(f), squad.DEFAULT_BULK_LINES)


class Rendering(TempFactory):
    def test_required_station_gets_the_hard_wall_brief_and_sysprompt(self):
        st = squad.plan(_bp(self.root, {
            "slug": "s",
            "agents": {"w": {"role": "worker", "delegate": "required", "brief": "do the thing"}},
        }), cwd=str(self.root))[0]
        md = squad.entrypoint_md(st, bulk=40)
        self.assertIn("MINI-ORCHESTRATOR (hard wall)", md)
        self.assertIn("do the thing", md)
        sp = squad.sysprompt(st, self.root / "entrypoint-w.md")
        self.assertIn("you do NOT do work directly", sp)

    def test_advised_station_names_the_bulk_threshold(self):
        st = squad.plan(_bp(self.root, {"slug": "s", "agents": {"w": {"role": "worker"}}}),
                        cwd=str(self.root))[0]
        self.assertIn("(25+ lines)", squad.entrypoint_md(st, bulk=25))

    def test_settings_json_is_valid_and_installs_four_hooks(self):
        d = json.loads(squad.settings_json("s", "w"))
        self.assertEqual(set(d["hooks"]),
                         {"SessionStart", "UserPromptSubmit", "PreToolUse", "StopFailure"})
        self.assertIn("statusLine", d)


class DropFromBlueprint(TempFactory):
    def test_removes_the_named_agent_and_rewrites_the_file(self):
        f = _bp(self.root, {"slug": "s", "agents": {"a": {"role": "worker"},
                                                     "b": {"role": "worker"}}})
        self.assertTrue(squad._drop_from_blueprint(f, "a"))
        doc = json.loads(open(f, encoding="utf-8").read())
        self.assertEqual(set(doc["agents"]), {"b"})

    def test_a_count_expanded_replica_is_not_a_literal_key(self):
        # "w1" is what `count:` expands "w" into at plan() time — it never exists as a key
        # in the blueprint itself, so there is nothing here to pop.
        f = _bp(self.root, {"slug": "s", "agents": {"w": {"role": "worker", "count": 3}}})
        self.assertFalse(squad._drop_from_blueprint(f, "w1"))
        doc = json.loads(open(f, encoding="utf-8").read())
        self.assertIn("w", doc["agents"])


class AddRemove(TempFactory):
    def test_add_unknown_name_is_fatal_and_spawns_nothing(self):
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"}}})
        with mock.patch("af.squad.lifecycle.up") as up:
            self.assertEqual(squad.cmd_add(f, "ghost"), 1)
            up.assert_not_called()

    def test_add_spawns_only_the_named_station_leaving_others_alone(self):
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"},
                                                           "b": {"role": "worker"}}})
        with mock.patch("af.squad.preflight", return_value=True), \
             mock.patch("af.squad.hooks.hooks_ok", return_value=True), \
             mock.patch("af.squad.lifecycle.up") as up, \
             mock.patch("af.tmux.has_session", side_effect=[False, True]):
            self.assertEqual(squad.cmd_add(f, "a"), 0)
        up.assert_called_once()
        self.assertEqual(up.call_args[0][0], "a")
        self.assertEqual(roster.load(self.p).blueprint, str((self.root / "bp.json").resolve()))

    def test_remove_unknown_name_errors_without_touching_anything(self):
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"}}})
        with mock.patch("af.squad.lifecycle.down") as down:
            self.assertEqual(squad.cmd_remove(f, "ghost"), 1)
            down.assert_not_called()

    def test_remove_kills_the_session_drops_roster_row_and_blueprint_entry(self):
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"},
                                                           "b": {"role": "worker"}}})
        roster.mark_up("a", self.p, role="worker")
        with mock.patch("af.squad.lifecycle.down") as down:
            self.assertEqual(squad.cmd_remove(f, "a"), 0)
        down.assert_called_once_with("a", self.p)
        self.assertIsNone(roster.get("a", self.p))
        doc = json.loads(open(f, encoding="utf-8").read())
        self.assertEqual(set(doc["agents"]), {"b"})

    def test_remove_deletes_spec_settings_and_sid_so_ledger_and_revive_forget_it(self):
        # `af down` deliberately keeps these — a station stays revivable. `remove` means
        # gone: without the spec, `af ledger` (globs agent-*.json) stops listing it, and
        # `af revive` refuses by default instead of resurrecting it from the manifest.
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"}}})
        self.p.specdir.mkdir(parents=True, exist_ok=True)
        self.p.spec_file("a").write_text("{}", encoding="utf-8")
        self.p.settings_file("a").write_text("{}", encoding="utf-8")
        self.p.sid_file("a").write_text("some-sid", encoding="utf-8")
        with mock.patch("af.squad.lifecycle.down"):
            self.assertEqual(squad.cmd_remove(f, "a"), 0)
        self.assertFalse(self.p.spec_file("a").exists())
        self.assertFalse(self.p.settings_file("a").exists())
        self.assertFalse(self.p.sid_file("a").exists())

    def test_add_on_an_already_running_station_is_not_an_error(self):
        # "skipped" (already up) is the requested end-state already holding, not a
        # failure — cmd_up doesn't count it as one, cmd_add shouldn't either.
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"}}})
        with mock.patch("af.squad.preflight", return_value=True), \
             mock.patch("af.squad.lifecycle.up") as up, \
             mock.patch("af.tmux.has_session", return_value=True):
            self.assertEqual(squad.cmd_add(f, "a"), 0)
        up.assert_not_called()

    def test_add_on_a_malformed_blueprint_is_fatal(self):
        f = self.root / "bad.json"
        f.write_text("{ not json", encoding="utf-8")
        with mock.patch("af.squad.lifecycle.up") as up:
            self.assertEqual(squad.cmd_add(str(f), "a"), 1)
            up.assert_not_called()

    def test_remove_on_a_malformed_blueprint_is_fatal(self):
        f = self.root / "bad.json"
        f.write_text("{ not json", encoding="utf-8")
        with mock.patch("af.squad.lifecycle.down") as down:
            self.assertEqual(squad.cmd_remove(str(f), "a"), 1)
            down.assert_not_called()

    def test_down_stops_the_squads_daemons_not_only_its_stations(self):
        # up starts warden+postmaster WITH the squad; down must stop them, or they outlive
        # the team and loop over a roster of dead stations forever.
        f = _bp(self.root, {"slug": self.slug, "agents": {"a": {"role": "worker"}}})
        with mock.patch("af.squad.lifecycle.down"), \
             mock.patch("af.warden.stop") as wstop, \
             mock.patch("af.postmaster.stop") as pstop:
            self.assertEqual(squad.cmd_down(f), 0)
        wstop.assert_called_once()
        pstop.assert_called_once()


class AutoSlug(TempFactory):
    """A fresh `up` must not sit on top of a DIFFERENT squad's slug — same slug means shared
    mailboxes, specs and state, i.e. another team's unread mail under a brand-new orc. When
    the base slug belongs to someone else, bump to base1/base2/…; when it is free or already
    ours, keep it so re-runs and --resume are unaffected."""

    def _bp_for(self, slug):
        return _bp(self.root, {"slug": slug, "agents": {"a": {"role": "worker"}}})

    def _occupy(self, slug, blueprint_path):
        # Make `slug` look like a live OTHER squad: a squad.json whose recorded blueprint is
        # some other path.
        p = squad._p(slug)
        roster.set_meta(blueprint_path, 1, p)

    def test_free_base_slug_is_used_unchanged(self):
        f = self._bp_for("proj")
        self.assertEqual(squad._resolve_slug("proj", str(Path(f).resolve())),
                         "proj")

    def test_a_slug_owned_by_another_squad_is_bumped(self):
        f = self._bp_for("proj")
        self._occupy("proj", "/some/other/blueprint.json")
        got = squad._resolve_slug("proj", str(Path(f).resolve()))
        self.assertEqual(got, "proj1")

    def test_our_own_slug_is_reused_not_bumped(self):
        f = self._bp_for("proj")
        resolved = str(Path(f).resolve())
        self._occupy("proj", resolved)                 # recorded blueprint == ours
        self.assertEqual(squad._resolve_slug("proj", resolved), "proj")

    def test_walks_past_several_foreign_slugs(self):
        f = self._bp_for("proj")
        resolved = str(Path(f).resolve())
        self._occupy("proj", "/other/a.json")
        self._occupy("proj1", "/other/b.json")
        self.assertEqual(squad._resolve_slug("proj", resolved), "proj2")

    def test_status_down_heal_resolve_to_the_bumped_slug(self):
        # The team lives under proj1; commands driven by the same blueprint must find proj1,
        # not the foreign proj.
        f = self._bp_for("proj")
        resolved = str(Path(f).resolve())
        self._occupy("proj", "/other/a.json")
        self._occupy("proj1", resolved)                # ours, bumped
        stations = squad.plan(f)
        self.assertEqual(squad._effective_paths(f, stations).slug, "proj1")

    def test_ours_wins_over_a_freed_base_slug(self):
        # The orphaning bug: foreign squad forced us to proj1, then the foreign proj is torn
        # down (its squad.json deleted) so proj falls free. down/status/heal must STILL find
        # our team at proj1 — a "first free or ours" walk would grab the now-free proj and
        # leave the live proj1 team unaddressable.
        f = self._bp_for("proj")
        resolved = str(Path(f).resolve())
        self._occupy("proj1", resolved)                # ours, bumped; proj has NO squad.json
        self.assertEqual(squad._resolve_slug("proj", resolved), "proj1")
        stations = squad.plan(f)
        self.assertEqual(squad._effective_paths(f, stations).slug, "proj1")

    def test_a_fresh_up_still_takes_the_free_base_when_nothing_is_ours(self):
        # No team of ours anywhere → the base is free and must be used, not bumped.
        f = self._bp_for("proj")
        self.assertEqual(squad._resolve_slug("proj", str(Path(f).resolve())), "proj")


if __name__ == "__main__":
    unittest.main()
