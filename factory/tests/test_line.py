"""line: blueprint parsing, count expansion, parent defaulting, threshold + delegate
resolution. Every rule here is one line.sh preserves on purpose — and the fatal-typo case
is the one that, when it was NOT fatal, produced a silently unwalled agent.

No agent is spawned. The parser and the resolver are pure; the only I/O is reading a
blueprint written into a temp dir.
"""

from __future__ import annotations

import textwrap
import unittest

from support import TempFactory   # imported first: it puts the af package on sys.path

from af import line


def _bp(tmp, text: str):
    f = tmp / "bp.yml"
    f.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(f)


class ParseScalars(unittest.TestCase):
    def test_yaml11_no_is_boolean_false(self):
        # `delegate: no` is the YAML 1.1 boolean False — line.sh saw it that way (safe_load),
        # and dlevel(False) must be "" (no hook), not the string "no".
        self.assertEqual(line.load_from_string("k: no")["k"], False)
        self.assertEqual(line.load_from_string("k: yes")["k"], True)
        self.assertEqual(line.load_from_string("k: 42")["k"], 42)

    def test_hash_is_a_comment_only_at_a_token_boundary(self):
        self.assertEqual(line.load_from_string("k: value   # trailing")["k"], "value")
        self.assertEqual(line.load_from_string("k: foo#bar")["k"], "foo#bar")

    def test_flow_sequences_anchors_tags_are_refused_not_misread(self):
        # Flow MAPPINGS are now accepted (see FlowMappings below); sequences, anchors and
        # tags stay loud FATALs — a half-parsed blueprint is a station with half a constitution.
        for bad in ("k: [a, b]", "k: &anchor v", "k: *ref", "k: !!str 5"):
            with self.assertRaises(line.BlueprintError):
                line.load_from_string(bad)

    def test_sequences_are_refused(self):
        with self.assertRaises(line.BlueprintError):
            line.load_from_string("agents:\n  - a\n  - b")


class FlowMappings(unittest.TestCase):
    """The SKILL.md blueprint example writes agents and `defaults:` as inline `{k: v, …}`
    flow mappings. The parser must accept them exactly where a block mapping is accepted —
    PyYAML's safe_load (what line.sh used) parsed all of these."""

    def test_flow_mapping_as_a_whole_agent_value(self):
        d = line.load_from_string(
            'agents:\n'
            '  orc: { role: orchestrator, model: opus, delegate: no }\n')
        self.assertEqual(
            d["agents"]["orc"],
            {"role": "orchestrator", "model": "opus", "delegate": False})

    def test_flow_mapping_as_defaults(self):
        d = line.load_from_string("defaults: { model: sonnet, caveman: true }")
        self.assertEqual(d["defaults"], {"model": "sonnet", "caveman": True})

    def test_flow_scalars_get_the_same_typing_as_block(self):
        # `no` is YAML 1.1 False, `40` is an int — inside flow exactly as in a block mapping.
        d = line.load_from_string("agents:\n  a: { count: 3, delegate: no, model: haiku }\n")
        self.assertEqual(d["agents"]["a"], {"count": 3, "delegate": False, "model": "haiku"})

    def test_quoted_brief_with_comma_and_colon_survives_intact(self):
        # The naive split(",")/split(":") corruption case: a brief full of both must round-trip.
        brief = "own it: dispatch, then verify, always."
        d = line.load_from_string(
            f'agents:\n  w: {{ role: worker, brief: "{brief}" }}\n')
        self.assertEqual(d["agents"]["w"]["brief"], brief)
        self.assertEqual(d["agents"]["w"]["role"], "worker")

    def test_empty_and_trailing_comma_flow_mappings(self):
        self.assertEqual(line.load_from_string("defaults: {}")["defaults"], {})
        self.assertEqual(
            line.load_from_string("d: { model: opus, }")["d"], {"model": "opus"})

    def test_nested_flow_mapping_is_refused_with_a_clear_message(self):
        with self.assertRaises(line.BlueprintError) as cm:
            line.load_from_string("w: { a: { b: c } }")
        self.assertIn("nested", str(cm.exception).lower())

    def test_flow_sequence_inside_a_flow_mapping_is_refused(self):
        with self.assertRaises(line.BlueprintError):
            line.load_from_string("w: { role: [x, y] }")


class DelegateLevels(unittest.TestCase):
    def test_the_three_levels_and_their_aliases(self):
        self.assertEqual(line.dlevel("required"), "required")
        self.assertEqual(line.dlevel("hard"), "required")
        self.assertEqual(line.dlevel("full"), "required")     # meant required before, still does
        self.assertEqual(line.dlevel("advised"), "advised")
        self.assertEqual(line.dlevel("advise"), "advised")
        self.assertEqual(line.dlevel(True), "advised")        # bare `delegate: true` = advised
        self.assertEqual(line.dlevel("no"), "")
        self.assertEqual(line.dlevel(False), "")
        self.assertEqual(line.dlevel(None, "advised"), "advised")   # default falls through

    def test_a_typo_is_FATAL(self):
        # The whole point. `delegate: requird` used to fall through to '' — no wall, no
        # advisory, no complaint — on a station meant to be walled. It must refuse.
        with self.assertRaises(line.BlueprintError):
            line.dlevel("requird")
        with self.assertRaises(line.BlueprintError):
            line.dlevel("blockk")


class Plan(TempFactory):
    def plan(self, text: str):
        return line.plan(_bp(self.root, text), cwd=str(self.root))

    def test_count_expands_sharing_one_brief(self):
        st = self.plan("""
            slug: s
            agents:
              abl:
                count: 3
                role: ablation
                brief: |
                  one hypothesis
        """)
        self.assertEqual([s.name for s in st], ["abl1", "abl2", "abl3"])
        self.assertTrue(all(s.brief == "one hypothesis" for s in st))

    def test_no_count_keeps_the_bare_name(self):
        st = self.plan("slug: s\nagents:\n  solo:\n    role: worker\n")
        self.assertEqual([s.name for s in st], ["solo"])

    def test_parent_defaults_to_the_role_orchestrator_by_role_not_name(self):
        # Top station named 'boss', role orchestrator. Everyone else must report to 'boss',
        # not to the literal 'orc' that used to be hard-coded.
        st = {s.name: s for s in self.plan("""
            slug: s
            agents:
              boss:
                role: orchestrator
              w:
                role: worker
        """)}
        self.assertEqual(st["boss"].parent, "")       # the orchestrator reports to no station
        self.assertEqual(st["w"].parent, "boss")

    def test_explicit_parent_wins(self):
        st = {s.name: s for s in self.plan("""
            slug: s
            agents:
              boss:
                role: orchestrator
              a:
                parent: boss
              b:
                parent: a
        """)}
        self.assertEqual(st["b"].parent, "a")

    def test_orchestrator_is_a_reserved_station_name(self):
        with self.assertRaises(line.BlueprintError):
            self.plan("slug: s\nagents:\n  orchestrator:\n    role: worker\n")

    def test_delegate_default_is_advised(self):
        st = self.plan("slug: s\nagents:\n  w:\n    role: worker\n")
        self.assertEqual(st[0].delegate, "advised")

    def test_defaults_delegate_flows_to_stations(self):
        st = {s.name: s for s in self.plan("""
            slug: s
            defaults:
              delegate: required
            agents:
              a:
                role: worker
              b:
                role: worker
                delegate: no
        """)}
        self.assertEqual(st["a"].delegate, "required")   # inherited
        self.assertEqual(st["b"].delegate, "")           # per-agent override wins

    def test_a_typo_in_a_station_delegate_aborts_the_whole_plan(self):
        with self.assertRaises(line.BlueprintError):
            self.plan("slug: s\nagents:\n  w:\n    delegate: requird\n")

    def test_work_is_resolved_absolute(self):
        st = self.plan("slug: s\nwork: ./out\nagents:\n  w:\n    role: worker\n")
        self.assertEqual(st[0].work, str(self.root / "out"))

    def test_thresholds_resolve_per_agent_over_defaults(self):
        st = {s.name: s for s in self.plan("""
            slug: s
            defaults:
              compact_soft: 120000
              compact_hard: 400000
            agents:
              a:
                role: worker
              b:
                role: worker
                compact_soft: 80000
        """)}
        self.assertEqual((st["a"].soft, st["a"].hard), ("120000", "400000"))
        self.assertEqual(st["b"].soft, "80000")          # per-agent wins
        self.assertEqual(st["b"].hard, "400000")         # falls back to default

    def test_slug_defaults_to_the_cwd_basename(self):
        st = self.plan("agents:\n  w:\n    role: worker\n")
        self.assertEqual(st[0].slug, self.root.name)

    def test_flow_style_resolves_identically_to_block_style(self):
        # The SKILL.md flow form and the equivalent block form must produce the SAME resolved
        # table — roles, parents, models, delegate, count expansion and all.
        def rows(stations):
            return [(s.name, s.role, s.parent, s.model, s.delegate, s.brief)
                    for s in stations]

        flow = self.plan("""
            slug: s
            defaults: { model: sonnet, delegate: advised }
            agents:
              orc:  { role: orchestrator, model: opus, delegate: no, brief: "Own it." }
              eval: { role: evaluation, parent: orc, brief: "Own metrics." }
              abl:  { count: 3, role: ablation, parent: orc, model: haiku, brief: "One hyp." }
        """)
        block = self.plan("""
            slug: s
            defaults:
              model: sonnet
              delegate: advised
            agents:
              orc:
                role: orchestrator
                model: opus
                delegate: no
                brief: "Own it."
              eval:
                role: evaluation
                parent: orc
                brief: "Own metrics."
              abl:
                count: 3
                role: ablation
                parent: orc
                model: haiku
                brief: "One hyp."
        """)
        self.assertEqual(rows(flow), rows(block))
        self.assertEqual([s.name for s in flow], ["orc", "eval", "abl1", "abl2", "abl3"])


class BulkLines(TempFactory):
    def test_bulk_lines_read_from_defaults_only(self):
        f = _bp(self.root, "defaults:\n  bulk_lines: 25\nagents:\n  w:\n    role: worker\n")
        self.assertEqual(line.bulk_lines(f), 25)

    def test_top_level_bulk_lines_is_ignored_as_in_bash(self):
        # A top-level `bulk_lines:` was silently ignored by the bash (it read
        # defaults.bulk_lines). Kept, not fixed — the hooks read AF_BULK_LINES from the env
        # `up` fills, and both sides must agree on where the number lives.
        f = _bp(self.root, "bulk_lines: 999\nagents:\n  w:\n    role: worker\n")
        self.assertEqual(line.bulk_lines(f), line.DEFAULT_BULK_LINES)


class Rendering(TempFactory):
    def test_required_station_gets_the_hard_wall_brief_and_sysprompt(self):
        st = line.plan(_bp(self.root, """
            slug: s
            agents:
              w:
                role: worker
                delegate: required
                brief: |
                  do the thing
        """), cwd=str(self.root))[0]
        md = line.entrypoint_md(st, bulk=40)
        self.assertIn("MINI-ORCHESTRATOR (hard wall)", md)
        self.assertIn("do the thing", md)
        sp = line.sysprompt(st, self.root / "entrypoint-w.md")
        self.assertIn("you do NOT do work directly", sp)

    def test_advised_station_names_the_bulk_threshold(self):
        st = line.plan(_bp(self.root, "slug: s\nagents:\n  w:\n    role: worker\n"),
                       cwd=str(self.root))[0]
        self.assertIn("(25+ lines)", line.entrypoint_md(st, bulk=25))

    def test_settings_json_is_valid_and_installs_four_hooks(self):
        import json
        d = json.loads(line.settings_json("s", "w"))
        self.assertEqual(set(d["hooks"]),
                         {"SessionStart", "UserPromptSubmit", "PreToolUse", "StopFailure"})
        self.assertIn("statusLine", d)


if __name__ == "__main__":
    unittest.main()
