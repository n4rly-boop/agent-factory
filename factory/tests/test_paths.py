"""paths: the bash and the Python write the SAME files during the migration. A path that
drifts by one character is an agent whose mail nobody reads."""

from __future__ import annotations

import os
import subprocess
import unittest

from support import MAIL_SH, TempFactory   # imported first: it puts the af package on sys.path

from af import paths as af_paths


class TestSlugify(unittest.TestCase):
    def test_matches_the_bash(self):
        # ai.sh: basename $CWD | tr A-Z a-z | sed 's/[^a-z0-9]//g' | cut -c1-12
        cases = {
            "agent-factory": "agentfactory",
            "link_ai": "linkai",
            "Croissan": "croissan",
            "A_very-Long.Project.Name": "averylongpro",   # cut -c1-12
            "___": "proj",
            "": "proj",
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                got = af_paths.slugify(name)
                self.assertEqual(got, want)
                self.assertLessEqual(len(got), 12)

    def test_matches_the_bash_pipeline_for_real(self):
        for name in ("agent-factory", "link_ai", "Croissan", "A_very-Long.Project.Name"):
            with self.subTest(name=name):
                out = subprocess.run(
                    ["bash", "-c",
                     f"printf '%s' {name!r} | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9]//g' | cut -c1-12"],
                    capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(af_paths.slugify(name), out)


class TestPaths(TempFactory):
    def test_env_drives_everything(self):
        self.assertEqual(self.p.slug, "aftest")
        self.assertEqual(self.p.state, self.root / ".ai" / "aftest")
        self.assertEqual(self.p.mailroot, self.root / ".ai" / "aftest" / "mail")
        self.assertEqual(self.p.session("qa"), "ai-aftest-qa")
        self.assertEqual(self.p.box("qa").name, "qa.jsonl")
        self.assertEqual(self.p.cursor("qa").name, "qa.cursor")
        self.assertEqual(self.p.task_flag("qa").name, "state-qa")
        self.assertEqual(self.p.tasker("qa").name, "tasker-qa")
        self.assertEqual(self.p.cap("qa").name, "cap-qa")

    def test_af_mailroot_override_is_honoured(self):
        # mail.sh honours it; a Python reader that ignored it would read a different mailbox
        # than the bash writer in the same session.
        os.environ["AF_MAILROOT"] = str(self.root / "elsewhere")
        p = af_paths.paths()
        self.assertEqual(p.mailroot, self.root / "elsewhere")

    def test_boxes_lists_only_mailboxes(self):
        self.p.box("qa").write_text("")
        self.p.box("orc").write_text("")
        self.p.cursor("qa").write_text("0")
        self.p.task_flag("qa").write_text("busy")
        self.assertEqual(self.p.boxes(), ["orc", "qa"])

    def test_mail_sh_derives_the_same_mailbox_path_from_the_same_env(self):
        """The path contract, exercised through the real mail.sh rather than a transcription
        of it: bash is given only AF_ROOT and AF_SLUG, and the box has to land exactly where
        Python says it is. A path that drifts by one character is an agent whose mail nobody
        reads."""
        env = self.bash_env()
        env.pop("AF_MAILROOT")            # let bash derive it, as an agent's env does
        env["AF_AGENT"] = "orc"
        r = subprocess.run(["bash", str(MAIL_SH), "send", "--to", "qa", "hello"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.p.box("qa").is_file(),
                        f"mail.sh did not write {self.p.box('qa')}; it wrote: "
                        f"{sorted(str(x) for x in self.root.rglob('*.jsonl'))}")


if __name__ == "__main__":
    unittest.main()
