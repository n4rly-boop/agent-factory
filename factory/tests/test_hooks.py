"""The four hooks — every case here is a hole this system has actually had.

The hooks are the only safety mechanism in the factory that fails SILENTLY: Claude Code
does not stop when a hook misbehaves, it shrugs and runs the tool. So the tests are written
as the failures, not as the features — a `required` station whose wall allowed a write, an
advisory that fired on a two-line edit, a typo that disarmed the wall, an exception that
became an allowed write.

Nothing here spawns an agent or touches the real /tmp/agent-factory: TempFactory owns
AF_ROOT, and the "repo" the wall is guarding is a path that need not exist (/repo), chosen
because it is NOT under /tmp or /var/folders — those are the scratch zone the wall allows,
and a test repo placed there would pass every assertion for the wrong reason.

The last class runs the BASH hooks on the same stdin and asserts the two agree. The bash is
still live; a divergence there is not a style difference, it is one of the two runtimes
having a hole the other does not.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import TempFactory, FACTORY   # imported first: puts the af package on sys.path

from af import hooks

REPO = "/repo"
WORK = "/repo/work"
BULK_BODY = "\n".join(f"line {i}" for i in range(200))
SMALL_BODY = "\n".join(f"line {i}" for i in range(5))


def write_ev(path, content="x\n"):
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def edit_ev(path, new=SMALL_BODY):
    return {"tool_name": "Edit", "tool_input": {"file_path": path, "new_string": new}}


def bash_ev(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


class HookRun(TempFactory):
    """Runs a hook in-process with a fake stdin and a controlled env."""

    def run_hook(self, name, payload=None, pane=None, **env):
        raw = payload if isinstance(payload, str) else json.dumps(payload or {})
        out, err = io.StringIO(), io.StringIO()
        clean = {k: "" for k in ("AF_ROLE", "AF_DELEGATE", "AF_WORK", "AF_PEERS",
                                 "AF_CAVEMAN", "AF_BULK_LINES", "AF_PARENT", "AF_AGENT")}
        clean.update({k: str(v) for k, v in env.items()})
        # role-reminder's Context:% line reads a real tmux pane — pane=None (the default)
        # keeps every OTHER test hermetic (no real tmux call); a test of the context% line
        # itself passes pane="...fixture text..." to fake one in.
        with mock.patch.dict(os.environ, clean), \
                mock.patch.object(sys, "stdin", io.StringIO(raw)), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err), \
                mock.patch("af.tmux.capture_pane", return_value=pane):
            rc = hooks.main([name])
        return rc, out.getvalue(), err.getvalue()

    def wall(self, payload, level="required", work=WORK, **env):
        return self.run_hook("delegate-wall", payload, AF_DELEGATE=level, AF_WORK=work,
                             AF_CWD=REPO, AF_AGENT="coder", **env)

    def assertDenied(self, rc, err):
        self.assertEqual(rc, 2, "exit 2 is the ONLY code Claude Code reads as a block")
        self.assertIn("BLOCKED", err)

    def assertSilent(self, rc, out):
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "an allowed write must say NOTHING — a note on every "
                                 "write is a note the model stops reading")

    def assertAdvised(self, rc, out):
        self.assertEqual(rc, 0, "the advisory level never blocks")
        d = json.loads(out)["hookSpecificOutput"]
        # Not negotiable: plain stdout from a PreToolUse hook goes to the debug log and
        # nowhere else, and permissionDecisionReason is likewise only logged.
        # additionalContext is the one field that reaches the MODEL while allowing the call.
        self.assertEqual(d["hookEventName"], "PreToolUse")
        self.assertEqual(d["permissionDecision"], "allow")
        self.assertIn("delegate-to-local-model", d["additionalContext"])
        return d["additionalContext"]


# ======================================================================================
# delegate-wall: the advisory level
# ======================================================================================
class Advised(HookRun):
    def test_a_small_edit_to_the_repo_passes_in_silence(self):
        # The default judges SIZE, not zone. A `required` agent was once observed spinning up
        # an external LLM to write ONE line, because the wall blocked it and it dutifully
        # re-routed. The discipline was real; the price was absurd.
        rc, out, err = self.wall(edit_ev(f"{REPO}/src.py", SMALL_BODY), level="advised")
        self.assertSilent(rc, out)

    def test_a_bulk_write_to_the_repo_earns_the_note(self):
        rc, out, err = self.wall(write_ev(f"{REPO}/src.py", BULK_BODY), level="advised")
        ctx = self.assertAdvised(rc, out)
        self.assertIn("200 lines, threshold 40", ctx)
        self.assertIn(f"{REPO}/src.py", ctx)

    def test_a_bulk_write_INSIDE_work_is_the_agent_doing_its_job(self):
        rc, out, err = self.wall(write_ev(f"{WORK}/report.md", BULK_BODY), level="advised")
        self.assertSilent(rc, out)

    def test_multiedit_is_measured_on_its_edits_not_its_absent_content(self):
        # MultiEdit keeps its payload in edits[].new_string. It once measured ZERO, so a
        # 50-edit 400-line rewrite got no advice at all — the easiest way in the toolbox to
        # do exactly the bulk work this hook exists to redirect.
        ev = {"tool_name": "MultiEdit", "tool_input": {
            "file_path": f"{REPO}/src.py",
            "edits": [{"new_string": BULK_BODY}, {"new_string": "one\ntwo"}]}}
        ctx = self.assertAdvised(*self.wall(ev, level="advised")[:2])
        self.assertIn("202 lines", ctx)

    def test_an_edit_is_measured_on_what_it_LANDS_not_on_the_file_it_edits(self):
        self.assertEqual(hooks.write_lines({"new_string": "a\nb\nc"}), 3)
        self.assertEqual(hooks.write_lines({"content": BULK_BODY}), 200)

    def test_the_advisory_never_blocks_even_on_a_thousand_line_write(self):
        rc, out, _ = self.wall(write_ev("/etc/passwd", BULK_BODY), level="advised")
        self.assertEqual(rc, 0)

    def test_the_note_fires_once_per_call_not_once_per_target(self):
        cmd = f"echo a > {REPO}/a.py; echo b > {REPO}/b.py"
        rc, out, _ = self.wall(bash_ev(cmd), level="advised", AF_BULK_LINES="1")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn(f"{REPO}/a.py", self.assertAdvised(rc, out))

    def test_the_note_carries_only_the_commands_FIRST_line(self):
        # The label lands in additionalContext, i.e. back in the model's context. A 500-line
        # heredoc would re-inject all 500 lines in a message whose entire point is "this is
        # bulk, keep it off your context".
        heredoc = f"cat > {REPO}/x.py <<'EOF'\n" + BULK_BODY + "\nEOF"
        ctx = self.assertAdvised(*self.wall(bash_ev(heredoc), level="advised")[:2])
        self.assertNotIn("line 150", ctx)
        self.assertIn("…", ctx)


class BulkThreshold(HookRun):
    def test_the_default_is_forty(self):
        self.assertEqual(hooks._bulk_lines(), 40)

    def test_a_junk_threshold_falls_back_rather_than_nagging_on_everything(self):
        # `[ abc -lt 40 ]` errors in bash, the && never fires and control falls THROUGH to
        # the advisory — so every two-line edit got nagged as bulk, and a nudge that fires on
        # everything is a nudge that gets ignored.
        with mock.patch.dict(os.environ, {"AF_BULK_LINES": "abc"}):
            self.assertEqual(hooks._bulk_lines(), 40)
        with mock.patch.dict(os.environ, {"AF_BULK_LINES": "-5"}):
            self.assertEqual(hooks._bulk_lines(), 40)

    def test_zero_is_not_junk_it_means_call_everything_bulk(self):
        # line.bulk_lines lets a literal 0 through and the bash honours it; rejecting it here
        # made the same blueprint behave differently under the two runtimes.
        with mock.patch.dict(os.environ, {"AF_BULK_LINES": "0"}):
            self.assertEqual(hooks._bulk_lines(), 0)
        rc, out, _ = self.wall(edit_ev(f"{REPO}/src.py", "one\ntwo"), level="advised",
                               AF_BULK_LINES="0")
        self.assertAdvised(rc, out)

    def test_the_threshold_is_honoured(self):
        rc, out, _ = self.wall(edit_ev(f"{REPO}/src.py", SMALL_BODY), level="advised",
                               AF_BULK_LINES="3")
        self.assertAdvised(rc, out)


# ======================================================================================
# delegate-wall: the hard wall
# ======================================================================================
class Required(HookRun):
    def test_a_write_to_the_repo_is_denied_at_any_size(self):
        rc, out, err = self.wall(write_ev(f"{REPO}/src.py", "x"))
        self.assertDenied(rc, err)
        self.assertIn(f"refused: {REPO}/src.py", err)
        # The deny text must name the way OUT, or the agent retries the same write.
        self.assertIn("delegate-to-local-model", err)
        self.assertIn(f"write it under {WORK}/", err)
        self.assertIn("NOT a way out: a Task subagent", err)

    def test_a_write_inside_its_own_work_dir_is_allowed(self):
        self.assertSilent(*self.wall(write_ev(f"{WORK}/report.md"))[:2])

    def test_a_relative_work_dir_still_matches_an_absolute_file_path(self):
        # Claude Code always reports an ABSOLUTE file_path; AF_WORK comes from the blueprint
        # and is typically "./work". Compared as strings, the agent is blocked from writing
        # its own report — and the block message then tells it to write its report. Loop.
        self.assertSilent(*self.wall(write_ev(f"{WORK}/report.md"), work="./work")[:2])

    def test_scratch_is_allowed_because_delegation_stages_its_prompts_there(self):
        # Walling /tmp off would block the very delegation the wall demands.
        self.assertSilent(*self.wall(write_ev("/tmp/foo"))[:2])
        self.assertSilent(*self.wall(write_ev("/var/folders/ab/cd/T/prompt.txt"))[:2])

    def test_the_factorys_own_state_dir_is_carved_back_OUT_of_scratch(self):
        # THE SELF-DISARM CASE. The agent's --settings file — the file that installs this very
        # hook — lives under $AF_ROOT, and $AF_ROOT defaults under /tmp. One write there and
        # the wall is gone. The carve-out must be checked BEFORE the /tmp allowlist, or the
        # allowlist hands it straight back.
        root = "/tmp/agent-factory"
        rc, out, err = self.wall(write_ev(f"{root}/.ai/s/settings-w.json"), AF_ROOT=root)
        self.assertDenied(rc, err)

    def test_the_carve_out_survives_the_private_tmp_alias(self):
        # On macOS /tmp is a symlink into /private, so /tmp/x and /private/tmp/x are THE SAME
        # FILE. Comparing raw strings let an agent step around the carve-out with one prefix.
        rc, out, err = self.wall(
            write_ev("/private/tmp/agent-factory/.ai/s/settings-w.json"),
            AF_ROOT="/tmp/agent-factory")
        self.assertDenied(rc, err)

    def test_the_carve_out_survives_a_dotdot_climb_out_of_work(self):
        # work/../ai.sh is not inside work/, whatever a prefix match says. (The bash compares
        # strings and takes it; this is the one place Python is deliberately TIGHTER.)
        rc, out, err = self.wall(write_ev(f"{WORK}/../ai.sh"))
        self.assertDenied(rc, err)

    def test_notebooks_are_judged_on_notebook_path(self):
        ev = {"tool_name": "NotebookEdit",
              "tool_input": {"notebook_path": f"{REPO}/x.ipynb", "new_source": "x"}}
        self.assertDenied(*[self.wall(ev)[i] for i in (0, 2)])

    def test_nothing_to_judge_is_not_a_guess(self):
        self.assertSilent(*self.wall({"tool_name": "Write", "tool_input": {}})[:2])


class RequiredBashForms(HookRun):
    """Every write construct the bash detects. Anything bash catches and Python misses is a
    hole; the wall is judged on the WRITE TARGET, never on "a path that appears somewhere in
    the command"."""

    def deny(self, cmd):
        rc, out, err = self.wall(bash_ev(cmd))
        self.assertDenied(rc, err)
        self.assertIn("via Bash:", err)

    def allow(self, cmd):
        self.assertSilent(*self.wall(bash_ev(cmd))[:2])

    def test_redirect(self):
        # A bare filename has no slash: the first version of this hook produced no token at
        # all for `echo pwned > ai.sh` — the most natural bypass of them all.
        self.deny("echo pwned > ai.sh")

    def test_append_redirect(self):
        self.deny("echo pwned >> ai.sh")

    def test_clobber_redirect(self):
        self.deny("echo pwned >| ai.sh")

    def test_heredoc(self):
        self.deny("cat > src.py <<'EOF'\nprint(1)\nEOF")

    def test_tee(self):
        self.deny("echo x | tee src.py")

    def test_tee_with_a_flag_before_the_file(self):
        self.deny("echo x | tee -a src.py")

    def test_sed_in_place(self):
        self.deny("sed -i '' s/a/b/ src.py")

    def test_perl_in_place(self):
        self.deny("perl -i -pe s/a/b/ src.py")

    def test_cp(self):
        self.deny("cp /tmp/staged.py src.py")

    def test_mv(self):
        self.deny("mv /tmp/staged.py src.py")

    def test_install(self):
        self.deny("install /tmp/staged.py src.py")

    def test_dd(self):
        self.deny("dd if=/tmp/staged of=src.py")

    def test_curl_output(self):
        self.deny("curl -o src.py http://example.invalid/x")

    def test_a_throwaway_write_to_tmp_does_not_disarm_the_rest_of_the_command(self):
        # The judge must RETURN on an allowed target, never exit: one command carries several
        # write targets. An early exit meant the FIRST allowed target ended the hook and every
        # target after it went unjudged — this exact command sailed through a `required` wall.
        self.deny("echo ok > /tmp/x; echo pwned > ai.sh")

    def test_a_write_into_work_is_allowed(self):
        self.allow("echo hi > work/report.md")

    def test_a_read_only_grep_is_not_a_write(self):
        # `2>` reads as a redirection and /abs/path as its target: the agent was told to
        # delegate a *grep*, and looped. /dev/* is not a write target.
        self.allow("grep -rn foo /abs/path 2>/dev/null")

    def test_a_gt_inside_quotes_is_not_a_redirection(self):
        # Quoting is the whole problem; this is why it is shlex and not a regex.
        self.allow("awk '$1 > 2' src.py")
        self.allow('grep "a -> b" src.py')

    def test_unbalanced_quotes_cannot_be_reasoned_about(self):
        self.assertEqual(hooks.bash_write_targets("echo 'unterminated > x.py"), [])

    def test_an_empty_command_is_not_a_write(self):
        self.assertSilent(*self.wall(bash_ev(""))[:2])


# ======================================================================================
# delegate-wall: the ways it must not fail open
# ======================================================================================
class FailsClosed(HookRun):
    def test_a_typo_in_the_level_is_FATAL(self):
        # `case $AF_DELEGATE in required|advised) ;; *) exit 0` — so `delegate: requird`
        # produced an agent with no wall, no advisory and no complaint, failing open past even
        # the default. A typo must not be a silently disarmed agent.
        rc, out, err = self.wall(write_ev(f"{REPO}/src.py"), level="requird")
        self.assertEqual(rc, 2, "a level we cannot read is not a level of 'no wall'")
        self.assertIn("FATAL", err)
        self.assertIn("requird", err)

    def test_the_typo_is_fatal_whatever_the_write_was(self):
        for ev in (write_ev(f"{WORK}/mine.md"), bash_ev("echo hi"), {"tool_name": "Bash"}):
            self.assertEqual(self.wall(ev, level="requird")[0], 2)

    def test_delegate_level_raises_only_on_a_value_nobody_can_act_on(self):
        self.assertEqual(hooks.delegate_level("required"), "required")
        self.assertEqual(hooks.delegate_level("REQUIRED"), "required")
        self.assertEqual(hooks.delegate_level(" advised "), "advised")
        self.assertEqual(hooks.delegate_level("no"), "no")
        self.assertEqual(hooks.delegate_level(""), "", "empty is an agent outside the scheme")
        with self.assertRaises(hooks.DelegateError):
            hooks.delegate_level("requird")
        # Non-strict is for the callers that must not die on it (role-reminder, the crash
        # handler) — it degrades to silence, never to "required".
        self.assertEqual(hooks.delegate_level("requird", strict=False), "")

    def test_an_unparseable_payload_does_not_become_an_allowed_write(self):
        rc, out, err = self.wall("this is not JSON")
        self.assertEqual(rc, 2)
        self.assertIn("DENIED", err)

    def test_an_exception_inside_the_wall_does_not_become_an_allowed_write(self):
        with mock.patch.object(hooks, "bash_write_targets", side_effect=RuntimeError("boom")):
            rc, out, err = self.wall(bash_ev("echo x > src.py"))
        self.assertEqual(rc, 2, "a wall that cannot decide must not allow")
        self.assertIn("RuntimeError", err)

    def test_an_exception_under_advised_is_a_missing_nudge_not_a_missing_wall(self):
        with mock.patch.object(hooks, "bash_write_targets", side_effect=RuntimeError("boom")):
            rc, out, err = self.wall(bash_ev("echo x > src.py"), level="advised")
        self.assertEqual(rc, 0)
        self.assertIn("RuntimeError", err)

    def test_required_with_no_work_dir_has_nowhere_legal_to_write_so_denies(self):
        # The bash exited 0 here: an agent with AF_DELEGATE=required and no AF_WORK had no
        # wall at all.
        rc, out, err = self.wall(write_ev(f"{REPO}/src.py"), work="")
        self.assertEqual(rc, 2)
        self.assertIn("AF_WORK is unset", err)

    def test_advised_with_no_work_dir_stays_quiet(self):
        self.assertSilent(*self.wall(write_ev(f"{REPO}/x"), level="advised", work="")[:2])

    def test_an_agent_outside_the_scheme_is_not_walled(self):
        for level in ("", "no", "off", "0", "none"):
            rc, out, err = self.wall(write_ev(f"{REPO}/src.py"), level=level)
            self.assertEqual((rc, out, err), (0, "", ""), f"AF_DELEGATE={level!r}")


# ======================================================================================
# spawn-gate: the ONE hard topology invariant — only the orchestrator spawns full agents
# ======================================================================================
class SpawnGate(HookRun):
    def gate(self, cmd, role="worker", **env):
        return self.run_hook("spawn-gate", bash_ev(cmd), AF_ROLE=role, AF_AGENT="w1", **env)

    def test_the_orchestrator_may_spawn(self):
        rc, out, err = self.gate("af up newstation", role="orchestrator")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_a_worker_running_af_up_is_denied(self):
        rc, out, err = self.gate("af up newstation", role="worker")
        self.assertEqual(rc, 2, "exit 2 is the ONLY code Claude Code reads as a block")
        self.assertIn("BLOCKED", err)
        self.assertIn("spawn-gate", err)

    def test_python_dash_m_af_up_is_also_gated(self):
        for cmd in ("python3 -m af up x", "python -m af up x", "python3 -m af.__main__ up x"):
            rc, *_ = self.gate(cmd, role="worker")
            self.assertEqual(rc, 2, cmd)

    def test_a_bare_af_path_invocation_is_gated(self):
        rc, *_ = self.gate("/repo/bin/af up x", role="worker")
        self.assertEqual(rc, 2)

    def test_af_revive_is_the_same_capability_as_af_up_and_is_also_gated(self):
        # lifecycle.revive() calls straight into the same up() af up does — a station that
        # can't `af up` must not be able to route around the gate through `af revive`.
        for cmd in ("af revive orc", "python3 -m af revive orc", "python -m af revive orc"):
            rc, *_ = self.gate(cmd, role="worker")
            self.assertEqual(rc, 2, cmd)
        rc, *_ = self.gate("af revive orc", role="orchestrator")
        self.assertEqual(rc, 0)

    def test_a_subshell_or_command_substitution_does_not_dodge_the_gate(self):
        # A subshell is ordinary shell syntax, not obfuscation — it must not be a bypass.
        for cmd in ("(af up x)", "$(af up x)", "( af up x )"):
            rc, *_ = self.gate(cmd, role="worker")
            self.assertEqual(rc, 2, cmd)

    def test_a_leading_env_assignment_does_not_dodge_the_gate(self):
        # `AF_SLUG=child af up` is an everyday env override, not an evasion attempt.
        for cmd in ("FOO=bar af up x", "AF_SLUG=child af up x", "A=1 B=2 af up x"):
            rc, *_ = self.gate(cmd, role="worker")
            self.assertEqual(rc, 2, cmd)

    def test_a_command_that_does_not_invoke_af_up_is_always_allowed(self):
        for cmd in ("af mail send --to x", "af ledger", "echo af up", "python3 -m af mail"):
            rc, out, err = self.gate(cmd, role="worker")
            self.assertEqual((rc, out, err), (0, "", ""), cmd)

    def test_a_non_bash_tool_is_always_allowed(self):
        rc, out, err = self.run_hook("spawn-gate", edit_ev(f"{REPO}/x.py"),
                                     AF_ROLE="worker", AF_AGENT="w1")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_an_agent_with_no_role_is_outside_the_scheme(self):
        # A bare, unmanaged session running `af up` is a human at the CLI, not a sub-agent
        # spawning a sub-team — nothing here to gate.
        rc, out, err = self.gate("af up x", role="")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_an_exception_for_a_non_orchestrator_does_not_become_an_allowed_spawn(self):
        with mock.patch.object(hooks, "_spawns_full_agent", side_effect=RuntimeError("boom")):
            rc, out, err = self.gate("af up x", role="worker")
        self.assertEqual(rc, 2, "a gate that cannot decide must not allow a spawn")
        self.assertIn("RuntimeError", err)

    def test_an_exception_for_the_orchestrator_does_not_block_the_root(self):
        with mock.patch.object(hooks, "_spawns_full_agent", side_effect=RuntimeError("boom")):
            rc, out, err = self.gate("af up x", role="orchestrator")
        self.assertEqual(rc, 0, "a broken gate must never stop the root from doing its job")

    def test_an_exception_with_no_role_does_not_block_a_bare_session(self):
        with mock.patch.object(hooks, "_spawns_full_agent", side_effect=RuntimeError("boom")):
            rc, out, err = self.gate("af up x", role="")
        self.assertEqual(rc, 0)


# ======================================================================================
# read-wall: deny a huge unbounded Read, with a one-shot `af read-force` escape
# ======================================================================================
def _read_ev(path, limit=None, offset=None):
    tin = {"file_path": path}
    if limit is not None:
        tin["limit"] = limit
    if offset is not None:
        tin["offset"] = offset
    return {"tool_name": "Read", "tool_input": tin}


class ReadWall(HookRun):
    def _write(self, name: str, lines: int) -> str:
        p = self.root / name
        p.write_text("\n".join(f"line {i}" for i in range(lines)) + "\n", encoding="utf-8")
        return str(p)

    def test_a_bounded_read_always_passes_however_big_the_file(self):
        big = self._write("big.py", 5000)
        rc, out, err = self.run_hook("read-wall", _read_ev(big, limit=100))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_an_unbounded_read_of_a_small_file_passes(self):
        small = self._write("small.py", 10)
        rc, out, err = self.run_hook("read-wall", _read_ev(small))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_an_unbounded_read_over_the_threshold_is_denied(self):
        big = self._write("big.py", 5000)
        rc, out, err = self.run_hook("read-wall", _read_ev(big), AF_READ_WALL_LINES="500")
        self.assertEqual(rc, 2)
        self.assertIn("BLOCKED", err)
        self.assertIn("read-force", err)

    def test_the_threshold_is_tunable(self):
        med = self._write("med.py", 50)
        rc, *_ = self.run_hook("read-wall", _read_ev(med), AF_READ_WALL_LINES="20")
        self.assertEqual(rc, 2)
        rc, *_ = self.run_hook("read-wall", _read_ev(med), AF_READ_WALL_LINES="200")
        self.assertEqual(rc, 0)

    def test_a_non_read_tool_is_always_allowed(self):
        big = self._write("big.py", 5000)
        rc, out, err = self.run_hook("read-wall", write_ev(big), AF_READ_WALL_LINES="500")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_read_force_is_a_ONE_SHOT_not_a_standing_allowlist(self):
        big = self._write("big.py", 5000)
        with mock.patch.dict(os.environ, {"AF_READ_WALL_LINES": "500"}):
            self.assertEqual(hooks.read_force(big), 0)
        rc1, *_ = self.run_hook("read-wall", _read_ev(big), AF_READ_WALL_LINES="500")
        self.assertEqual(rc1, 0, "the forced read must pass")
        rc2, *_ = self.run_hook("read-wall", _read_ev(big), AF_READ_WALL_LINES="500")
        self.assertEqual(rc2, 2, "a second read must be denied again — one-shot, not standing")

    def test_a_missing_file_stat_does_not_crash(self):
        rc, out, err = self.run_hook("read-wall", _read_ev(str(self.root / "nope.py")))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_read_force_with_a_relative_path_still_matches_the_absolute_file_path(self):
        # Read's own tool_input always carries an ABSOLUTE file_path. `af read-force` is run
        # by the agent in the same cwd, so a relative argument must resolve to the SAME key.
        big = self._write("big.py", 5000)
        rel = os.path.basename(big)
        with mock.patch.dict(os.environ, {"AF_READ_WALL_LINES": "500"}), \
                mock.patch("os.getcwd", return_value=str(self.root)):
            self.assertEqual(hooks.read_force(rel), 0)
        rc, *_ = self.run_hook("read-wall", _read_ev(big), AF_READ_WALL_LINES="500")
        self.assertEqual(rc, 0, "a relative read-force path must still forgive the real read")

    def test_read_force_consumption_is_unlink_not_check_then_unlink(self):
        # A check-then-unlink (is_file() then unlink()) is a TOCTOU race between two
        # concurrent Read hooks; unlink() itself must be the only check.
        big = self._write("big.py", 5000)
        with mock.patch.dict(os.environ, {"AF_READ_WALL_LINES": "500"}):
            self.assertEqual(hooks.read_force(big), 0)
        tok = hooks._read_force_dir() / hooks._read_force_key(big)
        self.assertTrue(tok.is_file())
        with mock.patch.object(Path, "is_file") as spy:
            rc, *_ = self.run_hook("read-wall", _read_ev(big), AF_READ_WALL_LINES="500")
            spy.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertFalse(tok.is_file(), "the token must be consumed")


# ======================================================================================
# role-reminder
# ======================================================================================
class RoleReminder(HookRun):
    def test_an_agent_with_no_role_says_nothing(self):
        self.assertEqual(self.run_hook("role-reminder", {}), (0, "", ""))

    def test_identity_and_chain_of_command(self):
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker", AF_AGENT="coder",
                                   AF_PARENT="lead", AF_PEERS="qa,docs")
        self.assertIn("ROLE: you are coder (worker). Report to: lead.", out)
        self.assertIn("Peers you may mail: qa,docs.", out)
        self.assertIn("$AF_MAIL send", out)
        self.assertTrue(out.endswith("\n"))

    def test_an_orchestrator_reports_to_the_human_not_to_itself(self):
        # Without this, AF_PARENT's default makes the top station mail a station that does
        # not exist.
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="orchestrator", AF_AGENT="orc")
        self.assertIn("the human, directly in chat", out)
        self.assertNotIn("Report to: orchestrator.", out)

    def test_an_explicit_parent_beats_the_human_default(self):
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="orchestrator",
                                   AF_AGENT="orc", AF_PARENT="boss")
        self.assertIn("Report to: boss.", out)

    def test_required_gets_the_hard_rule(self):
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker",
                                   AF_DELEGATE="required", AF_WORK="./work")
        self.assertIn("do not do the work yourself", out)
        self.assertIn("confined to ./work/", out)

    def test_advised_carries_BOTH_halves_of_the_rule(self):
        # "Delegate" on its own is how you get an agent farming a two-line fix out to an
        # external model. The rule has to carry its own boundary.
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker", AF_DELEGATE="advised")
        self.assertIn("delegate BULK/mechanical work", out)
        self.assertIn("Small surgical edits: just make them yourself.", out)

    def test_a_typo_here_is_silence_not_a_crash(self):
        # The wall is where a typo must be fatal. This hook runs on EVERY prompt; dying here
        # would cost the agent its identity line on every turn for no added safety.
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker", AF_DELEGATE="requird")
        self.assertEqual(rc, 0)
        self.assertIn("ROLE: you are", out)
        self.assertNotIn("MINI-ORCHESTRATOR", out)

    def test_caveman(self):
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker", AF_CAVEMAN="1")
        self.assertIn("Answer in caveman", out)

    def test_context_pct_appears_when_the_pane_shows_it(self):
        # Ground truth off the pane, not a re-derived estimate — same source probe() trusts.
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker",
                                   pane="Context: 42% left")
        self.assertIn("Context: 42%.", out)

    def test_no_context_line_when_the_pane_cannot_be_read(self):
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker", pane=None)
        self.assertNotIn("Context:", out)
        self.assertTrue(out.endswith("\n"))

    def test_a_broken_pane_read_is_silence_not_a_crash(self):
        # A nudge must never be the reason a prompt fails.
        with mock.patch("af.tmux.capture_pane", side_effect=RuntimeError("boom")):
            rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker")
        self.assertEqual(rc, 0)
        self.assertIn("ROLE: you are", out)

    def test_an_agent_with_no_role_gets_no_context_line_either(self):
        rc, out, _ = self.run_hook("role-reminder", {}, pane="Context: 10% left")
        self.assertEqual((rc, out), (0, ""))

    def test_it_stays_short_because_it_is_paid_for_on_every_turn(self):
        rc, out, _ = self.run_hook("role-reminder", {}, AF_ROLE="worker", AF_AGENT="coder",
                                   AF_DELEGATE="advised", AF_PEERS="a,b", AF_CAVEMAN="1")
        self.assertLess(len(out), 1200, "this is prepended to the context of EVERY prompt")


# ======================================================================================
# limit-hook — the marker the warden reads
# ======================================================================================
class LimitHook(HookRun):
    PAYLOAD = {"error_type": "rate_limit", "session_id": "s1",
               "message": "5-hour limit reached\nresets 6pm"}

    def marker(self, agent="coder"):
        return self.root / ".ai" / self.slug / f"limited-{agent}"

    def test_the_marker_has_the_TSV_shape_the_warden_parses(self):
        # epoch \t sid \t payload. warden.sh reads COLUMNS out of this — the format is a
        # contract between a Python hook and a shell script, not an implementation detail.
        (self.root / ".ai" / self.slug).mkdir(parents=True, exist_ok=True)
        (self.root / ".ai" / self.slug / "sid-coder").write_text("SID-123\n")
        rc, out, err = self.run_hook("limit-hook", self.PAYLOAD, AF_AGENT="coder",
                                     AF_ROOT=str(self.root), AF_SLUG=self.slug)
        self.assertEqual(rc, 0)
        cols = self.marker().read_text().rstrip("\n").split("\t")
        self.assertEqual(len(cols), 3)
        self.assertTrue(cols[0].isdigit(), "column 1 is the epoch")
        self.assertEqual(cols[1], "SID-123",
                         "the SID: an agent respawned under the same name is a DIFFERENT "
                         "agent and must not inherit 'you were interrupted, carry on'")
        self.assertIn("rate_limit", cols[2])

    def test_the_payload_column_is_flattened_and_capped(self):
        # A newline in column 3 would be a new RECORD to the warden's line reader.
        rc, *_ = self.run_hook("limit-hook", {"error_type": "rate_limit", "m": "a" * 900},
                               AF_AGENT="coder", AF_ROOT=str(self.root), AF_SLUG=self.slug)
        body = self.marker().read_text()
        self.assertEqual(len(body.splitlines()), 1)
        self.assertLessEqual(len(body.rstrip("\n").split("\t")[2]), 400)

    def test_a_missing_sid_leaves_the_column_empty_rather_than_no_marker(self):
        self.run_hook("limit-hook", self.PAYLOAD, AF_AGENT="ghost",
                      AF_ROOT=str(self.root), AF_SLUG=self.slug)
        self.assertEqual(self.marker("ghost").read_text().split("\t")[1], "")

    def test_an_orchestrator_session_has_no_AF_AGENT(self):
        self.run_hook("limit-hook", self.PAYLOAD, AF_ROOT=str(self.root), AF_SLUG=self.slug)
        self.assertTrue(self.marker("orchestrator").exists())

    def test_some_other_failure_is_not_ours(self):
        # A hook that assumes its matcher is a promise is a hook that one day writes "limited"
        # because the disk was full.
        self.run_hook("limit-hook", {"error_type": "oom"}, AF_AGENT="coder",
                      AF_ROOT=str(self.root), AF_SLUG=self.slug)
        self.assertFalse(self.marker().exists())

    def test_a_missing_error_type_is_trusted_to_the_matcher(self):
        self.run_hook("limit-hook", {"note": "no error_type in this version"},
                      AF_AGENT="coder", AF_ROOT=str(self.root), AF_SLUG=self.slug)
        self.assertTrue(self.marker().exists())

    def test_an_unparseable_payload_still_leaves_a_marker(self):
        # A marker beats a parse error: the warden can act on "coder was cut off" alone.
        rc, *_ = self.run_hook("limit-hook", "<<garbage>>", AF_AGENT="coder",
                               AF_ROOT=str(self.root), AF_SLUG=self.slug)
        self.assertEqual(rc, 0)
        self.assertTrue(self.marker().exists())

    def test_the_slug_falls_back_to_proj_exactly_as_the_bash_does(self):
        # limit-hook.sh defaults AF_SLUG to the literal "proj" and never derives it from the
        # cwd. Deriving it here would put the marker in a state dir the warden does not read.
        with mock.patch.dict(os.environ, {"AF_SLUG": ""}):
            self.assertEqual(hooks._state_paths().slug, "proj")


# ======================================================================================
# escalation-stop — it may block, but it must NEVER wait
# ======================================================================================
class EscalationStop(HookRun):
    def send(self, to="orchestrator", frm="coder", kind="blocked", body="I need the key"):
        from af import mailbox
        mailbox.send(to, body, kind=kind, frm=frm, p=self.p)

    def stop(self, agent=None):
        env = {"AF_ROOT": str(self.root), "AF_SLUG": self.slug}
        if agent:
            env["AF_AGENT"] = agent
        return self.run_hook("escalation-stop", {"stop_hook_active": False}, **env)

    def test_an_empty_mailbox_stops_the_session_immediately_and_silently(self):
        # No polling, no hostage-taking. It used to hold the turn open for 45s on a stale
        # flag — every idle turn thereafter cost 45 seconds and bought nothing, because mail
        # WAKES the orchestrator whenever it lands.
        self.assertEqual(self.stop(), (0, "", ""))

    def test_waiting_mail_blocks_the_stop_and_carries_the_message(self):
        self.send(body="the API key is missing")
        rc, out, err = self.stop()
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["decision"], "block",
                         "the ONLY shape Claude Code reads as 'keep going'")
        self.assertIn("the API key is missing", d["reason"])
        self.assertIn("from: coder", d["reason"])
        self.assertIn("kind: blocked", d["reason"])
        # The block reason is the same block `mail read` prints — the model has been trained
        # by every other delivery path to recognise exactly this as mail.
        self.assertIn("═══ MAIL for 'orchestrator' — 1 new ═══", d["reason"])
        self.assertIn("═══ end of mail ═══", d["reason"])
        self.assertIn("Reply with: bash $AF_MAIL send", d["reason"])

    def test_delivery_is_exactly_once_so_a_reblock_cannot_loop(self):
        # The cursor advances as `read` hands the message over. Without that, re-firing after
        # a block (stop_hook_active) loops on the same message forever.
        self.send(body="first")
        self.assertIn("first", json.loads(self.stop()[1])["reason"])
        self.assertEqual(self.stop(), (0, "", ""))

    def test_several_messages_arrive_as_one_block(self):
        self.send(body="one")
        self.send(body="two", frm="qa")
        d = json.loads(self.stop()[1])
        self.assertIn("2 new", d["reason"])
        self.assertIn("one", d["reason"])
        self.assertIn("two", d["reason"])

    def test_a_spawned_agent_watches_its_OWN_box(self):
        self.send(to="coder", frm="orchestrator", kind="task", body="do the thing")
        self.assertEqual(self.stop(), (0, "", ""), "not the orchestrator's mail")
        self.assertIn("do the thing", json.loads(self.stop("coder")[1])["reason"])

    def test_a_locked_mailbox_lets_the_session_stop_rather_than_reblocking(self):
        # The box is locked by the doorbell the agent itself just ran; its cursor did not move,
        # so nothing was consumed. Blocking here would re-block on EVERY Stop until the lock
        # cleared, and would hand the model an error string as if it were the escalation.
        from af import mailbox
        self.send()
        with mock.patch.object(mailbox, "read",
                               side_effect=mailbox.MailboxLocked("locked")):
            self.assertEqual(self.stop(), (0, "", ""))

    def test_losing_the_race_to_the_doorbell_reader_is_not_an_escalation(self):
        from af import mailbox
        self.send()
        with mock.patch.object(mailbox, "read", return_value=[]):
            self.assertEqual(self.stop(), (0, "", ""))

    def test_a_crash_in_the_mailbox_never_holds_the_turn_open(self):
        from af import mailbox
        self.send()
        with mock.patch.object(mailbox, "read", side_effect=RuntimeError("boom")):
            rc, out, err = self.stop()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "a crash must not print a block decision")


# ======================================================================================
# hooks_ok — the live check, because a wall that cannot execute is a hole
# ======================================================================================
class HooksOk(TempFactory):
    def settings(self, obj, name="settings.json"):
        f = self.root / name
        f.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return f

    def installs(self, cmd):
        return {"hooks": {"PreToolUse": [{"matcher": "Write",
                                          "hooks": [{"type": "command", "command": cmd}]}]}}

    def test_a_settings_file_whose_hooks_are_all_executable_passes(self):
        h = self.root / "wall.sh"
        h.write_text("#!/bin/sh\nexit 0\n")
        h.chmod(0o755)
        self.assertTrue(hooks.hooks_ok(self.settings(self.installs(str(h))), quiet=True))

    def test_a_hook_that_lost_its_x_bit_is_repaired_rather_than_failed(self):
        h = self.root / "wall.sh"
        h.write_text("#!/bin/sh\nexit 0\n")
        h.chmod(0o644)
        self.assertTrue(hooks.hooks_ok(self.settings(self.installs(str(h))), quiet=True))
        self.assertTrue(os.access(h, os.X_OK))

    def test_a_hook_that_cannot_be_made_executable_FAILS(self):
        # Claude Code does not stop when a hook cannot run: it prints an error and runs the
        # tool anyway. So a wall-shaped hole is worth more than a missing wall — it looks armed.
        s = self.settings(self.installs(str(self.root / "gone.sh")))
        self.assertFalse(hooks.hooks_ok(s, quiet=True))

    def test_a_settings_file_that_installs_NO_hooks_is_not_a_wall(self):
        # An empty `hooks` block reads as "configured" to every eye and blocks nothing.
        self.assertFalse(hooks.hooks_ok(self.settings({"hooks": {}}), quiet=True))
        self.assertFalse(hooks.hooks_ok(self.settings({}), quiet=True))

    def test_a_missing_settings_file_is_not_a_wall(self):
        self.assertFalse(hooks.hooks_ok(self.root / "nope.json", quiet=True))
        self.assertFalse(hooks.hooks_ok(None, quiet=True))

    def test_a_malformed_settings_file_is_no_hooks_not_a_crash(self):
        # This is called from `ledger` and from `up`; an AttributeError here took the whole
        # listing down over one bad file.
        for bad in ('{"hooks": "nope"}', '{"hooks": 5}', '{"hooks": []}',
                    '{"hooks": {"PreToolUse": "x"}}',
                    '{"hooks": {"PreToolUse": [{"hooks": {"a": 1}}]}}',
                    '{"hooks": {"PreToolUse": [{"hooks": [{"command": 5}]}]}}',
                    'not json at all', '[]'):
            s = self.settings(bad, name="bad.json")
            self.assertEqual(hooks.hook_commands(s), [], bad)
            self.assertFalse(hooks.hooks_ok(s, quiet=True), bad)

    def test_it_reports_argv0_of_each_installed_hook(self):
        s = self.settings({"hooks": {
            "PreToolUse": [{"hooks": [{"command": "/a/wall.sh --flag"}]}],
            "Stop": [{"hooks": [{"command": "/b/stop.sh"}]}]}})
        self.assertEqual(sorted(hooks.hook_commands(s)), ["/a/wall.sh", "/b/stop.sh"])


# ======================================================================================
# dispatch
# ======================================================================================
class Dispatch(HookRun):
    def test_every_hook_the_settings_install_is_dispatchable(self):
        self.assertEqual(sorted(hooks.HOOKS),
                         ["delegate-wall", "escalation-stop", "limit-hook", "read-wall",
                          "role-reminder", "session-start", "spawn-gate"])

    def test_an_unknown_hook_is_a_usage_error_not_a_silent_zero(self):
        rc, out, err = self.run_hook("nonesuch")
        self.assertEqual(rc, 64)
        self.assertIn("usage", err)
        rc, out, err = self.run_hook("")
        self.assertEqual(rc, 64)


# ======================================================================================
# the bash is still live: the two runtimes must not disagree
# ======================================================================================
@unittest.skipUnless((FACTORY / "hooks" / "delegate-wall.sh").exists(), "no bash half")
class BashParity(TempFactory):
    """Same stdin, same env, both implementations. Python is allowed to be TIGHTER (it denies
    a `..` climb the bash takes, it is fatal on a typo, it denies when it cannot decide). It
    is never allowed to be LOOSER: anything the bash blocks and Python does not is a hole."""

    def both(self, hook, payload, **env):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
             "PYTHONPATH": str(FACTORY), "AF_ROOT": str(self.root), "AF_SLUG": self.slug}
        e.update({k: str(v) for k, v in env.items()})
        sh = {"delegate-wall": FACTORY / "hooks" / "delegate-wall.sh",
              "role-reminder": FACTORY / "hooks" / "role-reminder.sh"}[hook]
        b = subprocess.run(["bash", str(sh)], input=raw, capture_output=True, text=True,
                           env=e, cwd=str(self.root))
        p = subprocess.run([sys.executable, "-m", "af.hooks", hook], input=raw,
                           capture_output=True, text=True, env=e, cwd=str(self.root))
        return b, p

    def assertBothDeny(self, payload, **env):
        b, p = self.both("delegate-wall", payload, AF_DELEGATE="required", AF_WORK=WORK,
                         AF_CWD=REPO, AF_AGENT="coder", **env)
        self.assertEqual(b.returncode, 2, f"bash allowed it: {b.stderr}")
        self.assertEqual(p.returncode, 2, f"PYTHON IS LOOSER THAN THE BASH: {p.stdout}")

    def assertBothAllow(self, payload, **env):
        b, p = self.both("delegate-wall", payload, AF_DELEGATE="required", AF_WORK=WORK,
                         AF_CWD=REPO, AF_AGENT="coder", **env)
        self.assertEqual(b.returncode, 0)
        self.assertEqual(p.returncode, 0, f"python denied what bash allows: {p.stderr}")

    def test_every_bash_write_form_is_blocked_by_both(self):
        for cmd in ("echo pwned > ai.sh",
                    "echo pwned >> ai.sh",
                    "echo pwned >| ai.sh",
                    "cat > src.py <<'EOF'\nprint(1)\nEOF",
                    "echo x | tee src.py",
                    "echo x | tee -a src.py",
                    "sed -i '' s/a/b/ src.py",
                    "perl -i -pe s/a/b/ src.py",
                    "cp /tmp/a.py src.py",
                    "mv /tmp/a.py src.py",
                    "install /tmp/a.py src.py",
                    "dd if=/tmp/a of=src.py",
                    "curl -o src.py http://example.invalid/x",
                    "echo ok > /tmp/x; echo pwned > ai.sh"):
            with self.subTest(cmd=cmd):
                self.assertBothDeny(bash_ev(cmd))

    def test_the_write_tools_agree(self):
        self.assertBothDeny(write_ev(f"{REPO}/src.py"))
        self.assertBothDeny(write_ev("/tmp/agent-factory/.ai/s/settings-w.json"),
                            AF_ROOT="/tmp/agent-factory")
        self.assertBothDeny(write_ev("/private/tmp/agent-factory/.ai/s/settings-w.json"),
                            AF_ROOT="/tmp/agent-factory")
        self.assertBothAllow(write_ev(f"{WORK}/report.md"))
        self.assertBothAllow(write_ev("/tmp/foo"))

    def test_the_read_only_commands_agree(self):
        for cmd in ("grep -rn foo /abs/path 2>/dev/null", "awk '$1 > 2' src.py",
                    "echo hi > work/report.md", "ls -la"):
            with self.subTest(cmd=cmd):
                self.assertBothAllow(bash_ev(cmd))

    def test_the_advisory_json_is_byte_for_byte_the_same(self):
        b, p = self.both("delegate-wall", write_ev(f"{REPO}/src.py", BULK_BODY),
                         AF_DELEGATE="advised", AF_WORK=WORK, AF_CWD=REPO, AF_AGENT="coder")
        self.assertEqual(json.loads(b.stdout), json.loads(p.stdout))

    def test_a_small_edit_is_silent_in_both(self):
        b, p = self.both("delegate-wall", edit_ev(f"{REPO}/src.py", SMALL_BODY),
                         AF_DELEGATE="advised", AF_WORK=WORK, AF_CWD=REPO, AF_AGENT="coder")
        self.assertEqual((b.returncode, b.stdout), (0, ""))
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_the_typo_hole_is_closed_now_that_bash_is_a_shim(self):
        # The old bash delegate-wall had a hole: a typo'd AF_DELEGATE ("requird") matched no
        # case, so the wall silently fell open (returncode 0) — the exact fail-open this port
        # exists to close. Python is FATAL on a level it cannot read (2). After T4, bash is a
        # thin shim that EXECS Python, so the two no longer diverge: bash fails closed too,
        # because bash IS Python now. That convergence is the migration working as designed.
        b, p = self.both("delegate-wall", write_ev(f"{REPO}/src.py"),
                         AF_DELEGATE="requird", AF_WORK=WORK, AF_CWD=REPO)
        self.assertEqual(p.returncode, 2, "python must be FATAL on a level it cannot read")
        self.assertEqual(b.returncode, 2, "bash is a shim → Python → same fail-closed verdict")

    def test_the_role_line_is_byte_for_byte_the_same(self):
        for env in ({"AF_ROLE": "worker", "AF_AGENT": "coder", "AF_DELEGATE": "advised"},
                    {"AF_ROLE": "worker", "AF_AGENT": "coder", "AF_DELEGATE": "required",
                     "AF_WORK": "./work", "AF_PEERS": "qa,docs", "AF_CAVEMAN": "1"},
                    {"AF_ROLE": "orchestrator", "AF_AGENT": "orc"},
                    {"AF_ROLE": "worker", "AF_AGENT": "coder", "AF_PARENT": "lead"}):
            with self.subTest(**env):
                b, p = self.both("role-reminder", {}, **env)
                self.assertEqual(b.stdout, p.stdout)


if __name__ == "__main__":
    unittest.main()
