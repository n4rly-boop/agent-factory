"""squad: blueprint loading, count expansion, parent defaulting, threshold + delegate
resolution. The fatal-typo case is the one that, when it was NOT fatal, produced a
silently unwalled agent.

No agent is spawned. The resolver is pure; the only I/O is reading a blueprint written
into a temp dir.
"""

from __future__ import annotations

import json
import unittest

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import squad


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


if __name__ == "__main__":
    unittest.main()
