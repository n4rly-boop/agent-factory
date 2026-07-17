"""The four hooks — and the check that they will actually fire.

Two halves:

  * `hook_commands` / `hooks_ok` — do the hooks a settings file installs EXIST and EXECUTE?
    In this system safety mechanisms fail SILENTLY. A hook that cannot execute does not
    block the tool: Claude Code prints an error and runs it anyway. So an agent whose
    delegate-wall hook lost its +x bit has a wall-shaped hole where its wall was, and
    nothing in its output says so. That is why this is a LIVE check of the files on disk,
    and why `ledger`'s wall column calls it instead of echoing what the spec claims.

  * the hooks themselves, ported from factory/hooks/*.sh and dispatched from `main`:

        python3 -m af.hooks role-reminder     # UserPromptSubmit
        python3 -m af.hooks delegate-wall     # PreToolUse: Write|Edit|MultiEdit|NotebookEdit|Bash
        python3 -m af.hooks delegate-progress # PreToolUse: Skill — resets delegate-wall's counter
        python3 -m af.hooks spawn-gate        # PreToolUse: Bash — only the orchestrator spawns (`af up`/`af revive`)
        python3 -m af.hooks read-wall         # PreToolUse: Read — deny huge unbounded reads
        python3 -m af.hooks limit-hook        # StopFailure, matcher rate_limit
        python3 -m af.hooks escalation-stop   # Stop

    They read Claude Code's JSON event on stdin and answer on stdout in the shapes Claude
    Code understands, which are NOT interchangeable: a PreToolUse allow-with-advice must be
    hookSpecificOutput.additionalContext (plain stdout is only logged, and "ask" would hang
    an unattended agent), a Stop block must be {"decision":"block","reason":…}, and a
    StopFailure's output is read by nobody at all.

    These run on every prompt and every matching tool call, so the module keeps its
    top-level imports to the cheap ones; paths/mailbox are imported inside the hook that
    needs them. `time python3 -m af.hooks role-reminder </dev/null` is the budget (the hook
    timeout is 5s; we are two orders of magnitude under it).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from .nums import intish
from pathlib import Path


def hook_commands(settings: str | Path) -> list[str]:
    """The executable of every hook the settings file installs (argv[0] of each command)."""
    try:
        s = json.loads(Path(settings).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(s, dict):
        return []
    groups = s.get("hooks")
    if not isinstance(groups, dict):
        # A settings file whose `hooks` is a string/number/list is not a wall, and it is also
        # not a crash: this is called from `ledger` and from `up`, and an AttributeError here
        # took the whole listing down over one malformed file. No hooks → no hooks.
        return []
    out: list[str] = []
    for grp in groups.values():
        if not isinstance(grp, list):
            continue
        for matcher in grp:
            if not isinstance(matcher, dict):
                continue
            hs = matcher.get("hooks")
            if not isinstance(hs, list):
                continue
            for h in hs:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if isinstance(cmd, str) and cmd.strip():
                    out.append(cmd.split()[0])
    return out


def hooks_ok(settings: str | Path | None, quiet: bool = False) -> bool:
    """True only if the file exists, installs at least one hook, and every hook is +x.

    A settings file that installs NO hooks is not a wall either — an empty `hooks` block
    reads as "configured" to every eye and blocks nothing, so it fails the check.
    Missing +x is repaired first (chmod), exactly as the bash does; only a chmod that
    cannot fix it is a failure.
    """
    if not settings:
        return False
    f = Path(settings)
    if not f.is_file():
        return False
    cmds = hook_commands(f)
    if not cmds:
        return False
    for h in cmds:
        if not os.access(h, os.X_OK):
            try:
                os.chmod(h, os.stat(h).st_mode | 0o111)
            except OSError:
                pass
        if not os.access(h, os.X_OK):
            if not quiet:
                print(f"[af] ⚠ hook not executable: {h} — it would FAIL OPEN "
                      f"(tool runs anyway)", file=sys.stderr)
            return False
    return True


# ======================================================================================
# shared
# ======================================================================================
def _stdin_json() -> dict:
    """The event Claude Code piped in. A hook that cannot read its payload knows nothing;
    what it does about that is the caller's decision, so this only reports the fact."""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        raise ValueError("hook payload is not JSON")
    return d if isinstance(d, dict) else {}


def _drain() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass


# The agent's OWN AF_* vars, read from its spec — or None, meaning "nothing better than the
# inherited environment". Bound once per hook run by `main`, from the `<slug> <name>` argv the
# agent's settings file carries.
#
# WHY NOT JUST THE ENVIRONMENT. A hook's env is whatever process ran it, and that process is
# not always the one `squad up` spawned. Claude Code forks a session (compaction, resume) by
# claiming a process from a MACHINE-GLOBAL spare pool, and that pool inherits the env of
# whichever session first started the daemon — which may be a DIFFERENT squad's agent, or a
# squad that died hours ago. Observed live: the `inna` orc forked at 16:14 onto a spare
# descended from a daemon started at 02:23 inside the `aae1` orc's pane, and every hook after
# that fork read AF_SLUG=aae1, AF_PEERS=eval,annotator, AF_WORK=<aae1's dir>. The agent kept
# its correct brief and mailbox (those come from the pane's own shell) while its hooks judged
# it as a station of a dead squad — a delegate-wall measuring writes against the wrong work
# dir, a spawn-gate reading the wrong role.
#
# So identity may not be inherited: it must be looked up. `<slug> <name>` is baked into the
# per-agent settings file at spawn (Claude Code passes it through as argv), and the spec at
# $AF_SPECROOT/<slug>/agent-<name>.json is the file `up` wrote for THIS agent. Neither can be
# swapped by a fork.
_IDENT: dict[str, str] | None = None
_IDENT_SLUG: str = ""


def _bind_identity(slug: str, agent: str) -> None:
    """Resolve this hook run's AF_* from the named agent's spec, not from the inherited env.

    A spec we cannot read leaves `_IDENT` None — the env fallback, i.e. the old behaviour.
    That is deliberate: the args are still trusted for slug/agent (they came from the
    settings file), but inventing a role/wall for an agent whose constitution is missing
    would be worse than the leak this function exists to close. `up` already refuses to
    spawn, loudly, when a spec cannot be written.
    """
    global _IDENT, _IDENT_SLUG
    _IDENT_SLUG = slug
    try:
        from . import spec as specmod
        from .paths import paths as _paths
        sp = specmod.read(agent, _paths(slug))
    except Exception:
        return
    d = {k: v for k, v in (sp.env or {}).items() if v}
    d["AF_AGENT"] = agent
    d["AF_SLUG"] = slug
    _IDENT = d


def _env(k: str, default: str = "") -> str:
    if _IDENT is not None:
        return _IDENT.get(k) or default
    return os.environ.get(k) or default


# ======================================================================================
# role-reminder — UserPromptSubmit
# ======================================================================================
# WHY A HOOK AND NOT AN INSTRUCTION. A brief in a file gets forgotten. A rule stated once in
# the system prompt survives compaction but competes with 200k tokens of recent work for
# attention — thirty turns into a debug session, "delegate instead of writing code yourself"
# is not what the model is thinking about. This re-states identity, chain of command and the
# two rules that decay fastest on EVERY prompt, at the position models weight most (closest
# to the task), for ~25 tokens. It cannot drift. Keep it SHORT: it is paid for every turn.
def role_reminder() -> int:
    _drain()  # the payload tells us nothing the env does not

    role = _env("AF_ROLE")
    if not role:
        return 0  # not a role-managed agent → say nothing

    agent = _env("AF_AGENT", "unknown")
    parent = _env("AF_PARENT", "orchestrator")
    # An orchestrator's parent is the human, not another orchestrator. Without this,
    # AF_PARENT's default makes the top station report to a station that does not exist.
    if role == "orchestrator" and not _env("AF_PARENT"):
        parent = ("the human, directly in chat (do NOT mail an 'orchestrator' — "
                  "no such station)")
    peers = _env("AF_PEERS")
    work = _env("AF_WORK", "work")

    out = [f"ROLE: you are {agent} ({role}). Report to: {parent}."]
    if peers:
        out.append(f" Peers you may mail: {peers}.")
    out.append(' Mail: bash $AF_MAIL send --to <agent> '
               '--kind <question|blocked|result|done|fyi> "…".')

    level = delegate_level(_env("AF_DELEGATE"), strict=False)
    if level == "required":
        out.append(" You are a MINI-ORCHESTRATOR: do not do the work yourself — dispatch it "
                   "via the delegate-to-local-model skill (the only route that can WRITE; a "
                   "Task subagent inherits your wall and cannot), or mail the peer who owns "
                   f"the area. Then verify the result. Your own writes are confined to {work}/.")
    elif level == "advised":
        # Both halves, always. "Delegate" on its own is how you get an agent farming out a
        # two-line fix to an external model — the rule has to carry its own boundary.
        out.append(" You are a MINI-ORCHESTRATOR: delegate BULK/mechanical work (many items, "
                   "boilerplate, spec-code, first drafts, big logs) via delegate-to-local-model, "
                   "or mail the peer who owns it, then verify. Small surgical edits: just make "
                   "them yourself.")
    if _env("AF_CAVEMAN") == "1":
        out.append(" Answer in caveman: drop articles/filler/hedging, keep every technical "
                   "fact exact.")

    # C — context cost made visible EVERY turn, not stated once and forgotten (same reasoning
    # as the rest of this hook's own docstring). Read Claude Code's OWN printed percentage off
    # the pane — ground truth, the same source probe() already trusts — not a re-derived
    # estimate. Silent on any failure: a nudge must never be the reason a prompt breaks.
    try:
        from . import tmux, patterns
        pane = tmux.capture_pane(_state_paths().session(agent))
        pct = patterns.context_pct(pane) if pane is not None else None
        if pct is not None:
            out.append(f" Context: {pct}%.")
    except Exception:
        pass

    sys.stdout.write("".join(out) + "\n")
    return 0


# ======================================================================================
# delegate-wall — PreToolUse on Write|Edit|MultiEdit|NotebookEdit|Bash
# ======================================================================================
# A mini-orchestrator that is TOLD to delegate will still, under pressure, just edit the file
# itself: it is faster, it is right there, and the instruction is 40k tokens back. Telling it
# again does not fix that — an instruction competes for attention, a hook does not.
#
#   AF_DELEGATE=advised   (the default) never blocks. A small write goes through in silence;
#                         a BULK write outside work/ goes through WITH a note in the model's
#                         context. Size, not zone, is what the default judges — a `required`
#                         agent was once observed spinning up an external LLM to write ONE
#                         line, because the wall blocked it and it dutifully re-routed. The
#                         discipline was real and the price was absurd.
#   AF_DELEGATE=required  the hard wall: any write outside work/ is denied, at any size.
#   AF_DELEGATE=no        no wall.
#
# WHAT THIS IS NOT, AT EITHER LEVEL: a sandbox. It routes; it does not contain — the
# sanctioned escape (delegate-to-local-model) writes wherever it is told. If you need
# containment, use permissions.
REQUIRED = ("required", "hard", "block", "wall", "full")
ADVISED = ("advised", "advise", "soft", "nudge", "1", "true", "yes", "on")
OFF = ("no", "0", "false", "off", "none")

DEFAULT_BULK = 40


class DelegateError(ValueError):
    """An AF_DELEGATE value nobody can act on."""


def delegate_level(raw: str, strict: bool = True) -> str:
    """"" | "no" | "advised" | "required" — and NOTHING else gets past here.

    The aliases exist because the value is typed by a human into a blueprint. The FATAL on an
    unknown value exists because of what happened when it wasn't: the bash reads
    `case $AF_DELEGATE in required|advised) ;; *) exit 0` — so `delegate: requird` produced an
    agent with no wall, no advisory and no complaint, failing open past even the default. A
    typo must not be a silently disarmed agent. Empty is not a typo — it is an agent outside
    the scheme, and stays silent.
    """
    v = (raw or "").strip().lower()
    if not v:
        return ""
    if v in REQUIRED:
        return "required"
    if v in ADVISED:
        return "advised"
    if v in OFF:
        return "no"
    if strict:
        raise DelegateError(
            f"AF_DELEGATE={raw!r} is not a level I understand. "
            f"Use one of: required ({'|'.join(REQUIRED[1:])}), "
            f"advised ({'|'.join(ADVISED[1:])}), no ({'|'.join(OFF[1:])})."
        )
    return ""


def _bulk_lines() -> int:
    """Sanitised, because a junk value did not fail safe in bash: `[ abc -lt 40 ]` errors, the
    && never fires, control falls THROUGH to the advisory — so every two-line edit got nagged
    as bulk, and a nudge that fires on everything is a nudge that gets ignored.

    ZERO IS NOT JUNK. `bulk_lines: 0` means "call everything bulk" and the bash honours it
    (`[ 3 -lt 0 ]` is false → advise); line.bulk_lines lets a literal 0 through, so rejecting
    it here would have made the same blueprint behave differently under the two runtimes.
    Only non-numeric and negative values fall back to the default.
    """
    v = _env("AF_BULK_LINES").strip()
    return intish(v, DEFAULT_BULK)


def _norm(p: str) -> str:
    """On macOS /tmp and /var are symlinks into /private, so `/tmp/agent-factory/x` and
    `/private/tmp/agent-factory/x` are THE SAME FILE. Comparing raw strings let an agent step
    around the $AF_ROOT carve-out with one extra prefix and overwrite the settings file that
    installs this very hook. normpath() on top of that is the other half: `work/../../etc/x`
    is not inside work/, whatever a prefix match says (the bash compares strings and would
    have taken it)."""
    if not p:
        return ""
    p = os.path.normpath(p)
    if p.startswith("/private/"):
        p = p[len("/private"):]
    return p


def _abs(tok: str, cwd: str) -> str:
    return tok if tok.startswith("/") else os.path.join(cwd, tok)


def _allowed(path: str, work: str, root: str) -> bool:
    """Is this path inside the zone this agent may write?"""
    p, w, r = _norm(path), _norm(work), _norm(root)
    if p == w or p.startswith(w + "/"):
        return True
    # The factory's own state dir is NOT scratch: the agent's --settings file lives under
    # $AF_ROOT, and $AF_ROOT defaults under /tmp. Writing there would let it disarm the wall.
    # Checked BEFORE the /tmp allowlist, or the allowlist would hand it straight back.
    if p == r or p.startswith(r + "/"):
        return False
    # Scratch is where a delegating agent stages a prompt or inspects output; walling it off
    # would block the very delegation we are demanding.
    if p.startswith("/tmp/") or p.startswith("/var/folders/"):
        return True
    return False


# Redirection, heredoc, tee, sed -i, cp/mv/install, dd… — judge the WRITE TARGET, never "any
# path that appears in the command".
#
# The first bash version scanned the whole command for path-looking tokens, which is wrong in
# both directions and the false positives are the worse half: `grep -rn foo /abs/path
# 2>/dev/null` was blocked (the `2>` reads as a write, the `/abs/path` as its target) — so
# the agent was told to delegate a *grep*, and looped. Meanwhile `echo pwned > ai.sh` sailed
# through, because a bare filename has no slash and produced no token at all.
#
# Tokenised with shlex, not a regex, because quoting is the whole problem: a `>` INSIDE a
# quoted string is not a redirection. `awk '$1 > 2' file` and `grep "a -> b" file` are
# read-only, and a regex scanner blocked both.
_REDIR_CHARS = set("0123456789>|&")


def bash_write_targets(cmd: str) -> list[str]:
    import shlex

    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        return []  # unbalanced quotes — cannot reason, say nothing

    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        # shlex with punctuation_chars groups redirection operators into their own token:
        # ">", ">>", ">|", "&>". The operand is whatever follows.
        if (t.rstrip("0123456789").startswith(">") or t.endswith(">")
                or t in (">", ">>", ">|")):
            if set(t) <= _REDIR_CHARS and i + 1 < len(toks):
                out.append(toks[i + 1])
                i += 2
                continue
        if t.startswith("of="):                       # dd
            out.append(t[3:])
        elif t == "tee":
            for n in toks[i + 1:]:
                if not n.startswith("-"):
                    out.append(n)
                    break
        elif t in ("-o", "-so", "--output"):          # curl and friends
            if i + 1 < len(toks):
                out.append(toks[i + 1])
        elif t.startswith("--output="):
            out.append(t[len("--output="):])
        elif t in ("sed", "perl") and any(
                a.startswith("-") and "i" in a for a in toks[i + 1:i + 4]):
            if toks[-1:]:
                out.append(toks[-1])
        elif t in ("cp", "mv", "install", "patch", "truncate", "rsync"):
            if toks[-1:]:
                out.append(toks[-1])
        i += 1

    return [p for p in out
            if p and not p.startswith("/dev/") and not p.isdigit() and p not in ("-", "&")]


def write_lines(tool_input: dict) -> int:
    """How many lines does this write LAND? The size of the payload, not of the tool call: an
    Edit is judged on its new_string, not on the file it edits.

    MultiEdit keeps its payload in edits[].new_string — neither `content` nor `new_string`
    exists on it, so it once measured ZERO and a 50-edit, 400-line rewrite got no advice at
    all: the easiest way in the toolbox to do exactly the bulk work this hook redirects.
    """
    if not isinstance(tool_input, dict):
        return 0
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        n = 0
        for e in edits:
            if isinstance(e, dict):
                n += len(str(e.get("new_string") or "").splitlines())
        return n
    body = (tool_input.get("content") or tool_input.get("new_string")
            or tool_input.get("new_source") or tool_input.get("command") or "")
    if isinstance(body, list):
        body = "\n".join(map(str, body))
    return len(str(body).splitlines())


def _deny_text(target: str, work: str) -> str:
    agent = _env("AF_AGENT", "this agent")
    return f"""BLOCKED by the factory's delegate-wall: '{agent}' is a mini-orchestrator and must not modify files directly.

  refused: {target}

Do it one of these ways instead:
  1. delegate-to-local-model skill — the way to get a file WRITTEN. It runs an
     external model in its own process, so it is not behind this wall.
  2. mail a peer agent that owns this area (bash $AF_MAIL send --to <agent> --kind task "…").
  3. if this is your own report or working note, write it under {work}/ (always allowed).

NOT a way out: a Task subagent. It inherits this same wall and will be blocked
identically — verified. Use it to READ and analyse, never to write.

Then verify what came back. Do not retry this write."""


def _advice_text(target: str, n: int, bulk: int) -> str:
    agent = _env("AF_AGENT", "this agent")
    return f"""You've self-written {n} lines outside your work dir without delegating (threshold {bulk}):
  {target}

You are '{agent}', a mini-orchestrator. delegate-to-local-model is free, runs in its own
process, and keeps the tokens off your context. Mailing the peer who owns this area is
the other route.

The write was ALLOWED — the counter resets the moment you actually delegate."""


def _bump_self_lines(agent: str, n: int, p) -> int:
    """Read-add-write the cumulative self-lines counter. No lock.

    ponytail: a lost increment (concurrent tool calls racing the same file) only delays the
    next nudge by one more write — the counter keeps growing regardless, and it self-corrects.
    Add a roster.edit()-style flock if this ever needs to become an audited count rather than
    an advisory one."""
    f = p.self_lines(agent)
    try:
        cur = intish(f.read_text(encoding="utf-8").strip(), 0)
    except OSError:
        cur = 0
    total = cur + n
    try:
        p.state.mkdir(parents=True, exist_ok=True)
        f.write_text(str(total), encoding="utf-8")
    except OSError:
        pass   # best-effort: a failed write only delays the next nudge, never blocks the tool
    return total


def _reset_self_lines(agent: str, p) -> None:
    try:
        p.state.mkdir(parents=True, exist_ok=True)
        p.self_lines(agent).write_text("0", encoding="utf-8")
    except OSError:
        pass


def _advise(msg: str) -> int:
    """ALLOW the tool, and put a note in the MODEL's context.

    It has to be this JSON shape. A PreToolUse hook that exits 0 sends its stdout to the debug
    log and NOWHERE ELSE — the model never sees it — and `permissionDecisionReason` is
    likewise only logged. `additionalContext` is the one field that reaches the model while
    still letting the call through. ("ask" would reach the human instead, and would still
    prompt even under --dangerously-skip-permissions: an unattended agent would hang on it
    forever.)
    """
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "delegate-wall: advisory only",
        "additionalContext": msg,
    }}))
    return 0


def _resolve_work(work: str, cwd: str) -> str:
    """Claude Code always reports an ABSOLUTE file_path, but AF_WORK comes from the blueprint
    and is typically written relative ("work: ./work"). Comparing the two as strings blocks
    the agent from writing its own report — and the block message then tells it to write its
    report. Every station loops.

    Unlike the bash (which `cd`s and gives up, i.e. disarms itself, if the dir is not there
    yet) this resolves a work dir that does not exist. A missing work/ is not a reason to stop
    guarding the repo."""
    if not work:
        return ""
    p = work if work.startswith("/") else os.path.join(cwd, work)
    return _norm(p)


def delegate_wall() -> int:
    level_raw = _env("AF_DELEGATE")
    try:
        level = delegate_level(level_raw)          # raises DelegateError on a typo
    except DelegateError as e:
        # FATAL, and fatal means DENY. A level we cannot read is not a level of "no wall" —
        # that is precisely the failure this replaces. Exit 2 is the only exit code Claude
        # Code reads as a block.
        print(f"delegate-wall: FATAL — {e}\n\nThe write is DENIED because the wall could not "
              f"be configured. Fix the blueprint's `delegate:` (or AF_DELEGATE) and respawn "
              f"this agent; do not retry the write.", file=sys.stderr)
        return 2
    if level in ("", "no"):
        return 0                                   # not a delegating agent → say nothing

    cwd = _env("AF_CWD") or os.getcwd()
    work = _resolve_work(_env("AF_WORK"), cwd)
    root = _env("AF_ROOT", "/tmp/agent-factory")
    bulk = _bulk_lines()

    if not work:
        # No safe zone to name. A `required` agent has nowhere it may legally write, so every
        # write is outside it: deny. (The bash exited 0 here — an agent with AF_DELEGATE and
        # no AF_WORK had no wall at all.)
        if level == "required":
            print("delegate-wall: AF_DELEGATE=required but AF_WORK is unset — no writable "
                  "zone is defined, so nothing can be allowed. DENIED.", file=sys.stderr)
            return 2
        return 0

    payload = _stdin_json()                        # ValueError → caught in main() → deny
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}

    advise_target = ""

    def judge(path: str, label: str = "") -> int | None:
        """The one decision, made once, per write target. RETURNS on an allowed target — it
        must never exit: one Bash command can carry several write targets and the caller loops
        over them. In the bash an early exit meant the FIRST allowed target ended the hook and
        every target after it went unjudged — `echo ok > /tmp/x; echo pwned > ai.sh` sailed
        through a `required` wall.

        Only REMEMBERS the first out-of-zone target for `advised` — it does not touch the
        self-lines counter here. `write_lines(tin)` is the same value on every iteration of
        this loop (it reads the whole tool call, not the per-target slice), so folding it into
        the cumulative counter inside this closure would double- or triple-count a single
        multi-target Bash call. The fold happens exactly once, after the loop, below."""
        nonlocal advise_target
        if _allowed(path, work, root):
            return None                            # inside work/ or scratch → not our business
        if level == "required":
            print(_deny_text(label or path, work), file=sys.stderr)
            return 2
        if not advise_target:                      # advise ONCE, after every target is judged
            advise_target = label or path
        return None

    if tool == "Bash":
        cmd = str(tin.get("command") or "")
        if not cmd:
            return 0
        head = cmd.splitlines()[0][:160] if cmd.splitlines() else ""
        if head != cmd:
            head += " …"
        # ADVISORY BLIND SPOT, accepted: for Bash we can only measure the COMMAND, and only a
        # heredoc actually carries the payload there. `cp big.py repo/`, `sed -i`, `tee`,
        # `curl -o` are one line each, so `advised` says nothing about them however much they
        # write. `required` is unaffected — it judges the target, not the size.
        for tok in bash_write_targets(cmd):
            # Only the command's FIRST LINE goes into the label: it ends up inside
            # additionalContext, i.e. back in the model's context, and a 500-line heredoc
            # would re-inject all 500 lines in a message whose whole point is "this is bulk,
            # keep it off your context".
            path = _abs(tok, cwd)
            rc = judge(path, f"{path}  (via Bash: {head})")
            if rc:
                return rc
        # Known gaps, accepted: an interpreter writing from inside its own source
        # (`python3 -c 'open(p,"w")'`), `git checkout`, and anything that computes its target
        # at runtime. This is a routing enforcer, not a sandbox.
    else:
        path = str(tin.get("file_path") or tin.get("notebook_path") or "")
        if not path:
            return 0                               # nothing to judge → don't guess
        rc = judge(path)
        if rc:
            return rc

    if advise_target:
        # Fold this call's size into the CUMULATIVE counter exactly once here — never inside
        # judge()'s per-target loop (see its docstring) — so a multi-target Bash call still
        # only counts once. `write_lines(tin)` is per-call size; `total` is what survives
        # across calls until a real delegation (delegate_progress()) or the threshold fires.
        n = write_lines(tin)
        p = _state_paths()                         # fork-safe — never a bare paths()
        who = _env("AF_AGENT", "orchestrator")
        total = _bump_self_lines(who, n, p)
        if total >= bulk:
            _reset_self_lines(who, p)
            return _advise(_advice_text(advise_target, total, bulk))
    return 0


# ======================================================================================
# delegate-progress — PreToolUse on Skill
# ======================================================================================
# Pure observer, never blocks: resets self-lines the moment the agent actually routes work
# out, so delegate_wall()'s cumulative advisory clears instead of ratcheting forever.
#
# Deliberately NOT matched on Agent/Task too. A Task subagent inherits this same wall under
# the SAME AF_AGENT identity (_deny_text/spawn_gate/the entrypoint brief all say so, three
# times over: "NOT a way out... will be blocked identically"). Resetting on a bare Task/Agent
# tool_use would reward exactly the workaround this wall exists to prevent — fire a trivial
# subagent call to zero the counter, then keep self-writing. Only a real
# Skill(delegate-to-local-model) call earns the reset.
def delegate_progress() -> int:
    try:
        payload = _stdin_json()
    except ValueError:
        return 0
    if str(payload.get("tool_name") or "") != "Skill":
        return 0
    tin = payload.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}
    if str(tin.get("skill") or "") == "delegate-to-local-model":
        _reset_self_lines(_env("AF_AGENT", "orchestrator"), _state_paths())
    return 0


# ======================================================================================
# spawn-gate — PreToolUse on Bash
# ======================================================================================
# The ONE hard topology invariant: only the orchestrator spawns full agents. A worker that
# can `af up` its own sub-team makes the tree a convention nobody enforces — same reasoning
# as delegate-wall (a rule stated in a prompt decays; a rule checked at the tool boundary
# does not), so this is built exactly the same way: PreToolUse on Bash, inspect the command
# about to run, judge, say nothing when there is nothing to say.
#
# A router, not a sandbox — same disclaimer as delegate_wall's own docstring. It catches the
# direct, ordinary ways an agent invokes its own CLI (`af up`, `af revive`, `python3 -m af
# up`, `(af up)`, `FOO=bar af up`); it does not chase an agent that shells out through
# `eval`, `sh -c '...'`, or a base64-encoded command to hide the invocation — those are
# genuinely a wrapper script hiding the call, out of scope for a routing enforcer the same
# way `bash_write_targets` doesn't chase them for writes either.
#
# "(" / ")" are split points too: `(af up)`/`$(af up)` must not dodge the gate just because
# a subshell wraps it — a subshell is not obfuscation, it's how a human writes an ordinary
# command every day.
_SHELL_SEPS = (";", "&&", "||", "|", "&", "(", ")")

# `FOO=bar af up` / `AF_SLUG=child af up` — a leading VAR=value is an ordinary env override
# on a command, not an attempt to hide it; it must not let the real command dodge the gate.
_ASSIGNMENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Both are the same capability: `af revive` calls straight into `lifecycle.up()`, same as
# `af up` does — a station that can't run `af up` must not be able to route around the gate
# through `af revive` instead.
_SPAWN_SUBCOMMANDS = ("up", "revive")


def _spawns_full_agent(cmd: str) -> bool:
    """Does this command spawn a full agent (`af up` / `af revive`, directly, via `python -m
    af`, or via a path to the same shim) AS A COMMAND, not merely CONTAIN those words?
    `echo af up` must not match — same false-positive shape the module's own
    `bash_write_targets` docstring warns about for scanning tokens without regard to
    position. So it only counts as the START of a subcommand, split on the shell's own
    separators, same tokenizer (shlex + punctuation_chars) `bash_write_targets` already uses
    for the same reason."""
    import shlex

    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return False  # unbalanced quotes — cannot reason, say nothing

    segs: list[list[str]] = [[]]
    for t in toks:
        if t in _SHELL_SEPS:
            segs.append([])
        else:
            segs[-1].append(t)

    for seg in segs:
        while seg and _ASSIGNMENT.match(seg[0]):
            seg = seg[1:]
        if not seg:
            continue
        if seg[0] in ("python3", "python") and len(seg) >= 3 and seg[1] == "-m" \
                and seg[2] in ("af", "af.__main__"):
            if len(seg) >= 4 and seg[3] in _SPAWN_SUBCOMMANDS:
                return True
            continue
        base = seg[0].rsplit("/", 1)[-1]
        if base == "af" and len(seg) >= 2 and seg[1] in _SPAWN_SUBCOMMANDS:
            return True
    return False


def spawn_gate() -> int:
    try:
        payload = _stdin_json()
    except ValueError:
        return 0  # cannot read the payload → nothing to judge, say nothing (not our wall to fail-closed)
    tool = str(payload.get("tool_name") or "")
    if tool != "Bash":
        return 0
    tin = payload.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}
    cmd = str(tin.get("command") or "")
    if not cmd or not _spawns_full_agent(cmd):
        return 0

    role = _env("AF_ROLE")
    if role == "orchestrator":
        return 0  # the root — the one station allowed to spawn full agents

    # Every other station, INCLUDING one with no AF_ROLE at all (a bare, unmanaged session
    # is not part of any squad's topology, so `af up`/`af revive` there is a human driving
    # the CLI directly, not a sub-agent spawning a sub-team — nothing to gate).
    if not role:
        return 0

    agent = _env("AF_AGENT", "this agent")
    print(f"BLOCKED by the factory's spawn-gate: '{agent}' is not the orchestrator and must "
          f"not spawn a full agent (af up / af revive).\n\n"
          f"Below the root, use a Task subagent or delegate-to-local-model instead — mail "
          f"the orchestrator if a new full station is genuinely needed.", file=sys.stderr)
    return 2


# ======================================================================================
# read-wall — PreToolUse on Read, with a one-shot escape hatch (`af read-force <path>`)
# ======================================================================================
# Reading a huge file straight into the window is the single biggest context sink, and
# nothing today stops it. This denies an UNBOUNDED Read (no `limit`) of a file over
# AF_READ_WALL_LINES lines — a bounded read (`offset`/`limit`) is normal, targeted work and
# always passes. Read's own schema has no field to carry "skip this deny", so the escape is
# a preceding CLI command instead: `af read-force <path>` drops a ONE-SHOT token that this
# hook consumes (deletes) the moment it lets a read through — it never becomes a standing
# allowlist entry.
#
# Fails OPEN on any error (a stat that raises, a hook payload it cannot parse): a false
# negative here just lets one big read through, same as today; a false positive would block
# ordinary work on every crash. Not the same choice as delegate-wall/spawn-gate, which guard
# invariants worth failing closed for — this is a nudge toward the cheaper path, not a wall.
DEFAULT_READ_WALL_LINES = 500


def _read_force_dir() -> Path:
    return Path(_env("AF_ROOT", "/tmp/agent-factory")) / "read-force"


def _read_force_abs(path: str) -> str:
    """Claude Code's Read tool always supplies an ABSOLUTE `file_path`. `af read-force` runs
    as a Bash call in the same agent's shell, so making a relative argument absolute against
    THIS process's cwd (the same cwd the agent itself is in) is what makes the two sides key
    to the same hash — without this, `af read-force notes.txt` silently never matches Read's
    own absolute path and the escape hatch never fires."""
    return _norm(path if os.path.isabs(path) else os.path.join(os.getcwd(), path))


def _read_force_key(path: str) -> str:
    import hashlib
    return hashlib.sha1(_read_force_abs(path).encode("utf-8")).hexdigest()


def read_force(path: str) -> int:
    """`af read-force <path>` — the escape hatch: the next unbounded Read of this exact path
    is allowed once, then the wall re-arms."""
    if not path:
        print("[af] usage: af read-force <path>", file=sys.stderr)
        return 1
    abs_path = _read_force_abs(path)
    d = _read_force_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / _read_force_key(path)).write_text(abs_path, encoding="utf-8")
    print(f"[af] one-shot read-force set for {abs_path} — the next unbounded Read passes, "
          f"then it re-arms.")
    return 0


def read_wall() -> int:
    try:
        payload = _stdin_json()
    except ValueError:
        return 0
    if str(payload.get("tool_name") or "") != "Read":
        return 0
    tin = payload.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}
    path = str(tin.get("file_path") or "")
    if not path or tin.get("limit"):     # bounded reads (limit set) always pass
        return 0

    tok = _read_force_dir() / _read_force_key(path)
    try:
        tok.unlink()          # unlink() itself is the check: POSIX guarantees only ONE
        return 0              # concurrent caller ever succeeds — no is_file()-then-unlink
    except FileNotFoundError: # race window, no second read ever slips through as "forced"
        pass
    except OSError:
        return 0              # some other race (dir gone mid-call) — not our business either

    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return 0                          # can't stat it → not our business, say nothing

    limit = intish((_env("AF_READ_WALL_LINES") or "").strip(), DEFAULT_READ_WALL_LINES)
    if limit == 0 or n <= limit:
        return 0

    print(f"""BLOCKED by the factory's read-wall: {path} is {n} lines (over {limit}) and this
would read it in whole, unbounded.

Do it one of these ways instead:
  1. bounded pages — pass offset/limit and read it in pieces.
  2. delegate-to-local-model skill (or a Task subagent) — get back a distilled slice
     instead of the raw file.
  3. af read-force {path} — a ONE-SHOT override if you genuinely need the whole file this
     time; it consumes itself on the next read and the wall re-arms.

Then verify what came back. Do not retry this exact read.""", file=sys.stderr)
    return 2


# ======================================================================================
# limit-hook — StopFailure, matcher rate_limit
# ======================================================================================
# Fires at the exact moment a turn is killed by the subscription usage limit. It is
# INFORMATIONAL ONLY: Claude Code ignores its exit code and its output, so it cannot block,
# cannot retry, cannot save the turn. All it can do is leave a note — and that is all we need,
# because the party that acts on the note is not a Claude at all.
#
# The limit is ACCOUNT-WIDE: when it lands it kills every agent on the machine AND the
# orchestrator driving them. Nobody is left with tokens to notice, so the only possible
# rescuer is a plain shell process that spends none. This hook is how the warden learns WHICH
# agents were cut off mid-work (as opposed to idle and fine); the statusline is how it learns
# WHEN the limit lifts.
#
# The marker holds the agent's SID. An agent respawned fresh under the same name is a
# different agent and must not inherit a "you were interrupted, carry on" meant for its
# predecessor.
def limit_hook() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}                               # a marker beats a parse error
    if not isinstance(payload, dict):
        payload = {}

    # The matcher should already have narrowed this to rate_limit, but a hook that assumes its
    # matcher is a promise is a hook that one day writes "limited" because the disk was full.
    et = str(payload.get("error_type") or "")
    if et not in ("rate_limit", ""):               # empty: some versions may not send it
        return 0

    import time

    p = _state_paths()
    p.state.mkdir(parents=True, exist_ok=True)

    who = _env("AF_AGENT", "orchestrator")
    try:
        sid = p.sid_file(who).read_text(encoding="utf-8").strip()
    except OSError:
        sid = ""
    # The RAW payload, newlines flattened, first 400 chars — same TSV third column the bash
    # writes (`tr '\n' ' ' | cut -c1-400`). The warden reads columns, so the shape is fixed.
    blob = raw.replace("\n", " ").replace("\r", " ")[:400]
    p.limited(who).write_text(f"{int(time.time())}\t{sid}\t{blob}\n", encoding="utf-8")
    return 0


# ======================================================================================
# session-start — SessionStart
# ======================================================================================
# Fires on every session start, resume, clear and compact — and crucially, it fires INSIDE
# the session with its real id. That is the only moment the fork id is knowable from inside
# claude: `claude --resume <parent>` forks to a new `--session-id`, and this hook is handed
# that new id in its payload. It writes it to sid-<agent>, so probe/sweep/warden stop reading
# the frozen parent transcript. Without it, sid-<agent> is written once at spawn and rots the
# instant the agent is resumed — see af/live.py for the same repair done from the outside.
#
# Informational, like limit-hook: Claude Code ignores its output, so it cannot fail the
# session. A missing session_id, an unreadable env — it writes nothing and returns 0.
def session_start() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return 0
    sid = str(payload.get("session_id") or "").strip().lower()
    if not sid:
        return 0

    p = _state_paths()
    who = _env("AF_AGENT", "orchestrator")
    try:
        p.state.mkdir(parents=True, exist_ok=True)
        old = ""
        try:
            old = p.sid_file(who).read_text(encoding="utf-8").strip()
        except OSError:
            old = ""
        if sid != old:
            p.sid_file(who).write_text(sid, encoding="utf-8")
            # The cache maps the OLD sid to a transcript that just became the frozen parent.
            p.log_cache(who).unlink(missing_ok=True)
    except OSError:
        return 0
    # Keep the durable roster's authoritative live_sid current from inside the session — the
    # one moment the post-fork id is knowable here. Only for a station already on the roster
    # (never create one: `who` defaults to "orchestrator", who is not a squad member), and
    # never let it fail the hook.
    try:
        from . import roster
        if roster.get(who, p) is not None:
            roster.set_live_sid(who, sid, p)
            if str(payload.get("source") or "") == "compact":
                roster.bump_compacts(who, p)
    except Exception:
        pass
    return 0


def _state_paths():
    """$AF_ROOT/.ai/$AF_SLUG — with the bash's fallback, not paths.py's.

    limit-hook.sh and statusline.sh both default AF_SLUG to the literal "proj"; they never
    derive it from the cwd. Deriving it here would put the marker in a state dir the warden
    does not read.

    The slug from argv wins over the env's, for the reason in _bind_identity: after a fork
    onto a pooled process, $AF_SLUG can name a different squad entirely — and this function
    picks the state dir the sid file, the limit marker and the roster live in. Writing those
    under another squad's slug is not a cosmetic slip: session-start would stamp THIS agent's
    live_sid onto the OTHER squad's roster row of the same name.
    # TODO(merge): belongs next to Paths.from_env as e.g. Paths.from_env(slug_default="proj").
    """
    from .paths import paths
    return paths(_IDENT_SLUG or os.environ.get("AF_SLUG") or "proj")


# ======================================================================================
# escalation-stop — Stop
# ======================================================================================
# A fully-stopped Claude session cannot be woken by an external event. So: this runs when the
# session tries to STOP. If an agent has mailed it, the hook returns
# {"decision":"block","reason":<the mail>} — the session auto-continues and handles it with NO
# human input. If nothing is waiting, it exits immediately.
#
# IT NEVER WAITS. It used to hold the turn open for 45s whenever an `await` flag said async
# work was outstanding, hoping the reply would land inside the window. That was a bad trade:
# the answer arrives as mail, and mail WAKES the orchestrator whenever it lands — so the wait
# bought nothing, while every stale flag (a crashed agent, a task queued to an agent that
# never came up) cost a 45-second stall on EVERY idle turn thereafter.
#
# The mailbox CURSOR gives exactly-once delivery: `read` advances it as it hands the message
# over, so re-firing after a block (stop_hook_active) cannot loop on the same message. And the
# box is THIS PROJECT'S: the old version polled one global inbox shared by every project on
# the machine, so a session in repo A was woken with — and told to answer — repo B's
# escalations.
def escalation_stop() -> int:
    _drain()                                       # we no longer read the Stop payload

    from . import mailbox
    from .paths import paths

    # If launched with `<slug> <agent>` args (identity bound), read that agent's mailbox under
    # the argv slug — fork-proof, like every other hook. With no args this is the human
    # orchestrator's own Stop: keep the env/cwd-derived slug, which is correct for it.
    # (Not installed by settings_json today; this keeps it safe if a Stop hook is ever added.)
    p = _state_paths() if _IDENT is not None else paths()
    who = _env("AF_AGENT", "orchestrator")         # unset in an orchestrator session

    if mailbox.unread(who, p) <= 0:
        return 0                                   # stop for real, immediately

    try:
        msgs = mailbox.read(who, p=p)
    except mailbox.MailboxLocked:
        # The box is locked by the other reader (the doorbell the agent itself just ran). Its
        # cursor did not move, so nothing was consumed: let the session stop and pick the
        # message up next time. Blocking here would re-block on every Stop until the lock
        # cleared, and would hand the model an error string as if it were the escalation.
        return 0
    except OSError:
        return 0
    if not msgs:
        return 0                                   # lost the race to the doorbell reader

    # Byte-for-byte what `mail.sh read` prints — that whole block is what the bash hook
    # embeds, trailing "Reply with:" line included, and it is what the model has been trained
    # by every other delivery path to recognise as mail.
    body = [f"═══ MAIL for '{who}' — {len(msgs)} new ═══"]
    for m in msgs:
        body.append(f"── from: {m.frm}   kind: {m.kind}   id: {m.id}")
        body.append(m.body)
    body.append("═══ end of mail ═══")
    body.append('Reply with: bash $AF_MAIL send --to <agent> '
                '--kind <question|blocked|result|done|fyi> "..."')

    reason = ("⚡ A spawned agent mailed you while you were idle:\n\n"
              + "\n".join(body)
              + "\n\nHandle it now: reply by mail (ai post <agent> --kind result \"…\"), or "
                "drive them with ai say/ask. Then you may stop.")
    # ensure_ascii=False: the block reason is read by a human in a debug log as often as by
    # the model, and ═══ is not a mail header anyone can recognise. Both forms
    # decode to the same string — Claude Code parses this, it does not grep it.
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


# ======================================================================================
# dispatch
# ======================================================================================
HOOKS = {
    "role-reminder": role_reminder,
    "delegate-wall": delegate_wall,
    "delegate-progress": delegate_progress,
    "spawn-gate": spawn_gate,
    "read-wall": read_wall,
    "limit-hook": limit_hook,
    "escalation-stop": escalation_stop,
    "session-start": session_start,
}


def main(argv: list[str] | None = None) -> int:
    global _IDENT, _IDENT_SLUG
    _IDENT, _IDENT_SLUG = None, ""   # each call starts from the env; args (if any) rebind below
    argv = list(sys.argv[1:] if argv is None else argv)
    name = argv[0] if argv else ""
    fn = HOOKS.get(name)
    if fn is None:
        print(f"usage: python3 -m af.hooks {{{'|'.join(HOOKS)}}} [<slug> <agent>]",
              file=sys.stderr)
        return 64

    # `<slug> <agent>`, as written into the agent's own settings file by squad.settings_json.
    # Optional: a settings file generated before this existed passes nothing and gets the old
    # env-derived behaviour. See _bind_identity for why the env alone cannot be trusted.
    if len(argv) >= 3:
        _bind_identity(argv[1], argv[2])

    try:
        return fn()
    except Exception as e:                         # noqa: BLE001 — deliberately total
        # FAIL CLOSED, and only a wall can fail closed: an exception in delegate-wall or
        # spawn-gate must never become an allowed write / an allowed spawn. The other hooks
        # cannot block anything by design, so for them a crash is a crash — say so on stderr
        # and get out of the agent's way.
        print(f"[af.hooks:{name}] {type(e).__name__}: {e}", file=sys.stderr)
        if name == "delegate-wall":
            try:
                if delegate_level(_env("AF_DELEGATE"), strict=False) == "required":
                    print("delegate-wall: DENIED — the wall itself failed, and a wall that "
                          "cannot decide must not allow. Retry; if it persists, this hook is "
                          "broken and the agent is unguarded.", file=sys.stderr)
                    return 2
            except Exception:
                return 2
            # `advised` never blocks, by definition — a broken advisory is a missing nudge,
            # not a missing wall. Allow, having said so on stderr.
        if name == "spawn-gate":
            # The one cheap signal that can decide this without re-doing the work that just
            # crashed: AF_ROLE. The orchestrator (or a bare, unmanaged session outside any
            # squad's topology) must never be blocked by a broken hook — failing closed there
            # would stop the root from doing its one job. Any OTHER role is exactly the case
            # this gate exists to stop, and that much is still known even though the rest of
            # the payload could not be judged — so THAT fails closed.
            role = _env("AF_ROLE")
            if role and role != "orchestrator":
                print("spawn-gate: DENIED — the gate itself failed, and a gate that cannot "
                      "decide must not allow a non-orchestrator to spawn. Retry; if it "
                      "persists, this hook is broken.", file=sys.stderr)
                return 2
        return 0


if __name__ == "__main__":
    sys.exit(main())
