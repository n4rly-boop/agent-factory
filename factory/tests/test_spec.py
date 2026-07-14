"""The spec: an agent's durable constitution. A spec that will not round-trip is an agent
that revives as a nameless twin of itself.

The fixtures are verbatim copies of real specs written by the bash (line.sh/ai.sh) and
living in ~/.claude/agent-factory/lines/. The suite ALSO round-trips every real spec on
this machine when there is one, copied into a temp dir — the originals are never opened
for writing.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from support import FIXTURES, TempFactory   # imported first: it puts the af package on sys.path

from af import spec as af_spec

REAL_LINES = Path.home() / ".claude" / "agent-factory" / "lines"


class TestRoundTrip(TempFactory):
    def assertRoundTrips(self, path: Path):
        original = json.loads(path.read_text(encoding="utf-8"))
        s = af_spec.Spec.from_dict(original)
        self.assertEqual(s.to_dict(), original)

    def test_fixture_specs(self):
        specs = sorted(FIXTURES.glob("spec-*.json"))
        self.assertTrue(specs, "the spec fixtures have gone missing")
        for f in specs:
            with self.subTest(spec=f.name):
                self.assertRoundTrips(f)

    def test_every_real_spec_on_this_machine(self):
        if not REAL_LINES.is_dir():
            self.skipTest("no real lines on this machine")
        specs = sorted(REAL_LINES.glob("*/agent-*.json"))
        if not specs:
            self.skipTest("no real specs on this machine")
        # Copy them out first. The originals belong to live agents.
        sandbox = self.root / "real"
        sandbox.mkdir()
        for src in specs:
            dst = sandbox / f"{src.parent.name}-{src.name}"
            shutil.copy2(src, dst)
            with self.subTest(spec=str(src)):
                self.assertRoundTrips(dst)

    def test_write_then_read_is_lossless(self):
        raw = json.loads((FIXTURES / "spec-real-orc.json").read_text(encoding="utf-8"))
        s = af_spec.Spec.from_dict(raw)
        f = af_spec.write(s, self.p)
        self.assertEqual(f, self.p.spec_file("orc"))
        back = af_spec.read("orc", self.p)
        self.assertEqual(back.to_dict(), raw)
        self.assertEqual(back, s)


class TestSpecFields(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((FIXTURES / "spec-real-orc.json").read_text(encoding="utf-8"))
        self.s = af_spec.Spec.from_dict(self.raw)

    def test_model_and_settings_are_derived_from_the_flags(self):
        self.assertEqual(self.s.model, "opus")
        self.assertEqual(self.s.model, self.raw["model"])
        self.assertEqual(self.s.settings, self.raw["settings"])

    def test_derived_beats_stored_when_they_disagree(self):
        d = dict(self.raw, model="haiku")          # a stale/wrong stored value
        s = af_spec.Spec.from_dict(d)
        self.assertEqual(s.model, "opus")          # the flags are the truth
        self.assertEqual(s.to_dict()["model"], "opus")

    def test_settings_is_derived_too_not_just_echoed_back(self):
        # Without a disagreeing stored value this assertion is unfalsifiable: the fallback
        # would return the stored string and look identical.
        d = dict(self.raw, settings="/stale/wrong.json")
        s = af_spec.Spec.from_dict(d)
        self.assertEqual(s.settings, self.raw["settings"])   # the flags win
        self.assertEqual(s.to_dict()["settings"], self.raw["settings"])

    def test_stored_values_are_the_fallback_for_a_pre_flags_spec(self):
        s = af_spec.Spec.from_dict({"name": "old", "flags": "", "model": "sonnet",
                                    "settings": "/s.json"})
        self.assertEqual(s.model, "sonnet")
        self.assertEqual(s.settings, "/s.json")

    def test_chain_of_command(self):
        self.assertEqual(self.s.role, "orchestrator")
        self.assertEqual(self.s.parent, "")        # the orc has none
        qa = af_spec.Spec.from_dict(
            json.loads((FIXTURES / "spec-real-qa.json").read_text(encoding="utf-8")))
        self.assertEqual(qa.parent, "orc")
        self.assertEqual(qa.role, "qa")
        self.assertEqual(qa.delegate, "advised")

    def test_thresholds(self):
        self.assertEqual(self.s.thresholds(), (200000, 500000))

    def test_thresholds_of_a_spec_that_has_none(self):
        s = af_spec.Spec.from_dict({"name": "x"})
        self.assertEqual(s.thresholds(), (None, None))

    def test_thresholds_ignore_junk(self):
        s = af_spec.Spec.from_dict({"name": "x", "ai_env": {"AI_COMPACT_SOFT": "lots"}})
        self.assertEqual(s.thresholds(), (None, None))

    def test_a_missing_field_never_explodes(self):
        s = af_spec.Spec.from_dict({})
        self.assertEqual(s.name, "")
        self.assertEqual(s.spawned, 0)
        self.assertEqual(s.env, {})


class TestSpecErrors(TempFactory):
    def test_a_missing_spec_raises(self):
        with self.assertRaises(af_spec.SpecError):
            af_spec.read("ghost", self.p)

    def test_a_corrupt_spec_raises_rather_than_reviving_a_nameless_agent(self):
        f = self.p.spec_file("broken")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{ not json")
        with self.assertRaises(af_spec.SpecError):
            af_spec.read("broken", self.p)

    def test_a_spec_that_is_not_an_object_raises(self):
        f = self.p.spec_file("weird")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[1, 2, 3]")
        with self.assertRaises(af_spec.SpecError):
            af_spec.read("weird", self.p)

    def test_all_specs_lists_agent_names(self):
        self.assertEqual(af_spec.all_specs(self.p), [])
        for name in ("orc", "qa", "lead"):
            af_spec.write(af_spec.Spec(slug="aftest", name=name, cwd="/x", sid="",
                                       spawned=0, flags=""), self.p)
        (self.p.specdir / "line.json").write_text("{}")          # must not be mistaken for an agent
        self.assertEqual(af_spec.all_specs(self.p), ["lead", "orc", "qa"])

    def test_spec_file_is_never_the_bare_name(self):
        # An agent literally named "line" must not collide with line.json.
        self.assertEqual(self.p.spec_file("line").name, "agent-line.json")


if __name__ == "__main__":
    unittest.main()
