"""The regexes, fed real pane text.

Every pane-*.txt without "-synth" in its name is a verbatim `tmux capture-pane -p` of a
live agent on this machine (Claude Code 2.1.207, seven agents across two lines), frozen so a
TUI version bump shows up here as a failing test instead of as an agent nobody rings.
The -synth ones are hand-built for states no live agent happened to be in (mid-turn,
paused on a permission prompt, out of quota); their prose is copied from the strings the
bash system records as observed live (ai.sh:522, ai.sh:269).
"""

from __future__ import annotations

import re
import unittest

from support import fixture   # imported first: it puts the af package on sys.path

from af import patterns

REAL_IDLE = [
    "pane-idle-normal.txt",            # empty box, normal mode
    "pane-idle-shellmode.txt",         # empty box, SHELL mode + the "! for shell mode" footer
    "pane-idle-queued-mail.txt",       # box holds an unsent doorbell
    "pane-idle-cyrillic.txt",          # box holds Cyrillic text
    "pane-idle-compact-scrollback.txt",  # many "❯ /compact" lines in the scrollback
]


class TestGenerating(unittest.TestCase):
    def test_idle_panes_are_not_generating(self):
        for name in REAL_IDLE:
            with self.subTest(pane=name):
                self.assertIsNone(patterns.GENERATING.search(fixture(name)))

    def test_finished_turn_summary_is_not_generating(self):
        # The trap: a settled pane says "✻ Worked for 11s" / "Cooked for 16s". Both carry
        # a digit and an 's'. Only the parenthesised live timer counts.
        for line in ("✻ Worked for 11s", "✻ Cooked for 16s", "✻ Baked for 51s"):
            with self.subTest(line=line):
                self.assertIsNone(patterns.GENERATING.search(line))

    def test_live_timer_matches(self):
        self.assertTrue(patterns.GENERATING.search(fixture("pane-generating-synth.txt")))
        for line in (
            "✳ Cogitating… (4s · ↑ 1.2k tokens · esc to interrupt)",
            "✻ Herding… (127s · ↓ 12.3k tokens · esc to interrupt)",
            "· (0s · ",
        ):
            with self.subTest(line=line):
                self.assertTrue(patterns.GENERATING.search(line))

    def test_hours_and_minutes_prefix_is_still_generating(self):
        # A turn past 60s rolls the timer to "(1m 5s · …)", past an hour to "(2h 3m 4s · …)",
        # and a compaction paints "Coalescing… (7m 12s · …)". All are STILL working: matching
        # only "(\d+s" read every long turn as idle and silently disarmed the doorbell dedup
        # for exactly the busy agents it protects. The h/m prefix is optional, so the bare
        # "(4s · …)" must go on matching too.
        for line in (
            "(4s · ↑ 1.2k tokens)",
            "(1m 5s · …)",
            "(10m 49s · …)",
            "Coalescing… (7m 12s · …)",
            "(2h 3m 4s · …)",
        ):
            with self.subTest(line=line):
                self.assertTrue(patterns.GENERATING.search(line), f"missed: {line!r}")

    def test_no_timer_is_not_generating(self):
        # A pane with only a context readout and no parenthesised timer is idle: nothing to ring.
        for line in ("Context: 0%", "❯ some prompt with no statusline"):
            with self.subTest(line=line):
                self.assertIsNone(patterns.GENERATING.search(line))

    def test_agrees_with_the_bash_it_replaces(self):
        # ai.sh:516  _busy(){ ... grep -qE '\([0-9]+s · '; }
        bash = re.compile(r"\([0-9]+s · ")
        for name in REAL_IDLE + ["pane-generating-synth.txt"]:
            with self.subTest(pane=name):
                pane = fixture(name)
                self.assertEqual(bool(bash.search(pane)), bool(patterns.GENERATING.search(pane)))


class TestPermission(unittest.TestCase):
    def test_permission_prompt_matches(self):
        pane = fixture("pane-permission-synth.txt")
        self.assertTrue(patterns.PERMISSION.search(pane))

    def test_both_alternatives_fire_on_their_own(self):
        self.assertTrue(patterns.PERMISSION.search("│  Do you want to proceed?              │"))
        self.assertTrue(patterns.PERMISSION.search("│  ❯ 1. Yes                             │"))

    def test_idle_panes_are_not_permission(self):
        for name in REAL_IDLE:
            with self.subTest(pane=name):
                self.assertIsNone(patterns.PERMISSION.search(fixture(name)))

    def test_feedback_survey_is_not_a_permission_prompt(self):
        # A real idle pane (inna-qa) renders "1: Bad  2: Fine  3: Good  0: Dismiss".
        # Numbered, selectable — and NOT a tool-permission pause.
        self.assertIsNone(patterns.PERMISSION.search("  1: Bad    2: Fine   3: Good   0: Dismiss"))


class TestUsageLimit(unittest.TestCase):
    # ai.sh:523 — the one that gates /compact.
    AI_SH = re.compile(r"hit your (session|usage) limit|usage limit reached")
    # warden.sh:74 — case-insensitive, and two wordings ai.sh never learned.
    WARDEN_SH = re.compile(
        r"hit your (session|usage) limit|usage limit reached|limit reached .*resets|limit will reset",
        re.IGNORECASE,
    )

    VARIANTS = [
        "  ⎿  You've hit your session limit · resets 10am",   # ai.sh:522, observed live
        "You've hit your usage limit",
        "Claude usage limit reached · try again later",
        "5-hour limit reached ∙ resets 2pm",                  # warden-only wording
        "Your limit will reset at 3pm",                       # warden-only wording
        "USAGE LIMIT REACHED",                                # warden matched case-insensitively
    ]

    def test_union_matches_every_variant(self):
        for line in self.VARIANTS:
            with self.subTest(line=line):
                self.assertTrue(patterns.USAGE_LIMIT.search(line), f"missed: {line!r}")

    def test_union_is_a_superset_of_both_bash_regexes(self):
        """Whatever EITHER bash belt caught, the Python one must catch. Run over a corpus
        NOT curated to match — the real panes and the negative lines — so the assertion can
        actually fail: any line a bash regex hits and the Python misses is a silent
        regression (an under-matching scraper keeps acting, which is the worse half)."""
        corpus = (self.VARIANTS
                  + [ln for name in REAL_IDLE for ln in fixture(name).splitlines()]
                  + fixture("pane-limited-synth.txt").splitlines()
                  + ["Context: 36%", "the rate limit config file", "hit your head on the limit"])
        checked = 0
        for line in corpus:
            hit_bash = bool(self.AI_SH.search(line) or self.WARDEN_SH.search(line))
            if hit_bash:
                checked += 1
                self.assertTrue(patterns.USAGE_LIMIT.search(line),
                                f"bash matched but python missed: {line!r}")
        self.assertGreaterEqual(checked, len(self.VARIANTS) - 1,
                                "the corpus is not exercising the bash regexes at all")

    def test_the_two_bash_belts_really_did_disagree(self):
        # The reason the union exists. ai.sh's copy — the one gating /compact — is blind to
        # the wordings warden.sh learned, so a limited agent was invisible to sweep and got
        # /compact re-sent every tick, forever.
        for line in ("5-hour limit reached ∙ resets 2pm", "Your limit will reset at 3pm"):
            with self.subTest(line=line):
                self.assertIsNone(self.AI_SH.search(line))          # ai.sh missed it
                self.assertTrue(self.WARDEN_SH.search(line))        # warden caught it
                self.assertTrue(patterns.USAGE_LIMIT.search(line))  # the union catches it

    def test_limited_pane_matches(self):
        self.assertTrue(patterns.USAGE_LIMIT.search(fixture("pane-limited-synth.txt")))

    def test_idle_panes_are_not_limited(self):
        for name in REAL_IDLE:
            with self.subTest(pane=name):
                self.assertIsNone(patterns.USAGE_LIMIT.search(fixture(name)))

    def test_ordinary_prose_about_limits_is_not_a_limit(self):
        # These must NOT fire — a false "limited" parks a healthy agent until a reset
        # that will never come.
        for line in (
            "Context: 36%",
            "the rate limit config file",
            "hit your head on the limit",
        ):
            with self.subTest(line=line):
                self.assertIsNone(patterns.USAGE_LIMIT.search(line))


class TestResumeChooser(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(patterns.RESUME_CHOOSER.search(fixture("pane-resume-chooser-synth.txt")))

    def test_idle_panes_do_not_match(self):
        for name in REAL_IDLE:
            with self.subTest(pane=name):
                self.assertIsNone(patterns.RESUME_CHOOSER.search(fixture(name)))


def _bash_input_box(pane: str) -> str | None:
    """mail.sh:118 _pending(), transcribed:
        grep -E '^[[:space:]]*[❯!]' | tail -1 | sed 's/^[[:space:]]*[❯!][[:space:]]*//'
    Kept here so the tests can PROVE the fixture contains the trap the bash falls into.
    """
    hits = [ln for ln in pane.splitlines() if re.match(r"^[ \t]*[❯!]", ln)]
    if not hits:
        return None
    return re.sub(r"^[ \t]*[❯!][ \t]*", "", hits[-1])


class TestInputBox(unittest.TestCase):
    def test_empty_box_normal_mode(self):
        # The box renders as "❯\xa0" — an NBSP, not a space.
        self.assertEqual(patterns.input_box(fixture("pane-idle-normal.txt")), "")

    def test_box_holds_the_unsent_doorbell(self):
        self.assertEqual(
            patterns.input_box(fixture("pane-idle-queued-mail.txt")),
            "bash $AF_MAIL read",
        )

    def test_box_holds_cyrillic(self):
        self.assertEqual(patterns.input_box(fixture("pane-idle-cyrillic.txt")), "мержи и деплой")

    def test_shell_mode_box_is_not_the_footer_hint(self):
        """THE bug. In shell mode the pane carries both

            "!\\xa0"                (column 0 — the live box, empty)
            "  ! for shell mode"    (indented — a footer hint)

        mail.sh's parser allows leading whitespace and takes tail -1, so it reads the
        HINT as the box: its "did the Enter get eaten?" check then compares the doorbell
        against "for shell mode", never matches, and reports success unconditionally.
        """
        pane = fixture("pane-idle-shellmode.txt")
        # The trap is really in this pane (this is what bash sees):
        self.assertEqual(_bash_input_box(pane), "for shell mode")
        # Python must see the box.
        self.assertEqual(patterns.input_box(pane), "")
        self.assertNotEqual(patterns.input_box(pane), "for shell mode")

    def test_last_prompt_wins_over_scrollback(self):
        # The rag pane's scrollback is full of submitted "❯ /compact" prompts. Only the
        # live (last, column-0) box counts, and it is empty.
        self.assertEqual(patterns.input_box(fixture("pane-idle-compact-scrollback.txt")), "")

    def test_no_box_at_all(self):
        # The resume chooser has no input box: every prompt-ish line is indented.
        self.assertIsNone(patterns.input_box(fixture("pane-resume-chooser-synth.txt")))
        self.assertIsNone(patterns.input_box("just some text\nand more\n"))

    def test_FINDING_a_modal_pane_reports_a_LONG_SUBMITTED_prompt_as_its_live_box(self):
        """FINDING (characterised, not a failure — the correct answer is a design call).

        A permission prompt REPLACES the input box: the pane has no column-0 "❯ " line, only
        the modal's "│  ❯ 1. Yes". But submitted prompts stay in the scrollback at column 0
        (see pane-idle-compact-scrollback.txt: "❯ /compact" three times over), so
        `input_box()` walks past the modal and hands back the last prompt the human typed —
        possibly hours ago — as if it were sitting unsent in the box.

        This is the very failure the module docstring says it fixed ("searching the whole
        pane matches those forever"); the last-match rule only fixes it while a box exists.
        mail.sh:118 has it too, so it is parity — but a caller that trusts probe().inputbox
        to mean "unsent text" (mail.sh's `_pending`, ai.sh's send-retry) is wrong here.

        Suggested: return None when patterns.PERMISSION matches, or have probe() null
        `inputbox` unless phase is idle/generating.
        """
        pane = fixture("pane-permission-synth.txt")
        self.assertTrue(patterns.PERMISSION.search(pane))
        box = patterns.input_box(pane)
        self.assertIsNotNone(box)                       # <- today's behaviour
        self.assertNotEqual(box, "")
        self.assertTrue(box.startswith("я ничего не понял"))   # a prompt from the scrollback

    def test_generating_pane_still_has_its_box(self):
        self.assertEqual(patterns.input_box(fixture("pane-generating-synth.txt")), "")


class TestFlagRegexes(unittest.TestCase):
    FLAGS = ("--settings /Users/x/.claude/agent-factory/lines/aae1/settings-orc.json "
             "--model opus --append-system-prompt You\\ are\\ orc. --dangerously-skip-permissions")

    def test_model_and_settings_extract(self):
        self.assertEqual(patterns.FLAG_MODEL.search(self.FLAGS).group(1), "opus")
        self.assertEqual(
            patterns.FLAG_SETTINGS.search(self.FLAGS).group(1),
            "/Users/x/.claude/agent-factory/lines/aae1/settings-orc.json",
        )

    def test_session_id(self):
        self.assertTrue(patterns.SESSION_ID.search("b045d974-cf11-4979-955c-fd3f2ee9d37f"))
        self.assertIsNone(patterns.SESSION_ID.search("b045d974-cf11-4979-955c"))

    def test_context_pct_reads_the_last_render(self):
        # Two statusline frames in the scrollback; the freshest (last) is the live one.
        pane = ("link_ai |  git:(x) | [Opus 4.8] Context: 42%\n"
                "…later…\nlink_ai |  git:(x) | [Opus 4.8] Context: 0%\n")
        self.assertEqual(patterns.context_pct(pane), 0)
        self.assertEqual(patterns.context_pct("[Opus 4.8] Context: 12%"), 12)

    def test_context_pct_absent_is_None(self):
        self.assertIsNone(patterns.context_pct("❯ some prompt with no statusline"))

    def test_strip_sid_leaves_no_double_space(self):
        from af import spec
        flags = "--model opus --session-id b045d974-cf11-4979-955c-fd3f2ee9d37f --settings /x.json"
        self.assertEqual(spec.strip_sid(flags), "--model opus --settings /x.json")

    def test_strip_resume_too(self):
        from af import spec
        flags = "--model opus --resume 7404ae94-15d2-427f-acd3-f8625d60a386"
        self.assertEqual(spec.strip_sid(flags), "--model opus")

    def test_strip_sid_survives_undecodable_bytes(self):
        # The flags carry a %q-quoted system prompt that may not decode as UTF-8. A decode
        # error here blanks the flags — i.e. an agent that revives with no role.
        from af import spec
        raw = b"--model opus --append-system-prompt caf\xe9 --resume b045d974-cf11-4979-955c-fd3f2ee9d37f"
        flags = raw.decode("utf-8", "surrogateescape")
        out = spec.strip_sid(flags)
        self.assertIn("--model opus", out)
        self.assertNotIn("--resume", out)
        self.assertEqual(out.encode("utf-8", "surrogateescape"),
                         b"--model opus --append-system-prompt caf\xe9")


if __name__ == "__main__":
    unittest.main()
