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


def _env(k: str, default: str = "") -> str:
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
    if role == "orchestrator" and not os.environ.get("AF_PARENT"):
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
    v = (os.environ.get("AF_BULK_LINES") or "").strip()
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
    return f"""This write is bulk ({n} lines, threshold {bulk}) and lands outside your work dir:
  {target}

You are '{agent}', a mini-orchestrator. Bulk writing is exactly what the
delegate-to-local-model skill is for: it is free, it runs in its own process, and it keeps
the tokens off your context. Mailing the peer who owns this area is the other route.

The write was ALLOWED — if you have already thought about it, carry on. If you were just
doing it because it was quicker than delegating, delegate it and verify what comes back."""


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

    advise_target, advise_n = "", 0

    def judge(path: str, label: str = "") -> int | None:
        """The one decision, made once, per write target. RETURNS on an allowed target — it
        must never exit: one Bash command can carry several write targets and the caller loops
        over them. In the bash an early exit meant the FIRST allowed target ended the hook and
        every target after it went unjudged — `echo ok > /tmp/x; echo pwned > ai.sh` sailed
        through a `required` wall."""
        nonlocal advise_target, advise_n
        if _allowed(path, work, root):
            return None                            # inside work/ or scratch → not our business
        if level == "required":
            print(_deny_text(label or path, work), file=sys.stderr)
            return 2
        n = write_lines(tin)                       # advised: only bulk is worth a word
        if n < bulk:
            return None
        if not advise_target:                      # advise ONCE, after every target is judged
            advise_target, advise_n = (label or path), n
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
        return _advise(_advice_text(advise_target, advise_n, bulk))
    return 0


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


def _state_paths():
    """$AF_ROOT/.ai/$AF_SLUG — with the bash's fallback, not paths.py's.

    limit-hook.sh and statusline.sh both default AF_SLUG to the literal "proj"; they never
    derive it from the cwd. Deriving it here would put the marker in a state dir the warden
    does not read.
    # TODO(merge): belongs next to Paths.from_env as e.g. Paths.from_env(slug_default="proj").
    """
    from .paths import paths
    return paths(os.environ.get("AF_SLUG") or "proj")


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

    p = paths()                                    # AF_SLUG, else slugify(basename(AF_CWD))
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
    "limit-hook": limit_hook,
    "escalation-stop": escalation_stop,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    name = argv[0] if argv else ""
    fn = HOOKS.get(name)
    if fn is None:
        print(f"usage: python3 -m af.hooks {{{'|'.join(HOOKS)}}}", file=sys.stderr)
        return 64

    try:
        return fn()
    except Exception as e:                         # noqa: BLE001 — deliberately total
        # FAIL CLOSED, and only the wall can fail closed: an exception in delegate-wall must
        # never become an allowed write. The other three cannot block anything by design, so
        # for them a crash is a crash — say so on stderr and get out of the agent's way.
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
        return 0


if __name__ == "__main__":
    sys.exit(main())
