"""squad — bring up a whole team of agents from one blueprint.

The problem it solves: a team's design (who exists, who reports to whom, who may only
delegate, who gets the cheap model) is the part that is easiest to get wrong and hardest
to remember. Written as a prompt it decays — you spawn five agents, tell each its role
once, and thirty turns later nobody remembers they were supposed to delegate. Written as
a blueprint it is configuration: applied identically to every agent, every time, and
enforced by hooks rather than hoped for.

    python3 -m af.squad plan   <bp.json>          the resolved team — WITHOUT spawning it
    python3 -m af.squad up     [--resume] <bp>    briefs + settings + specs, then spawn
    python3 -m af.squad status <bp.json>          who's alive, context size, unread mail
    python3 -m af.squad down   <bp.json>          stop every station
    python3 -m af.squad add    <bp.json> <name>   spawn ONE new station, rest untouched
    python3 -m af.squad remove <bp.json> <name>   kill it, drop it from roster + blueprint
    python3 -m af.squad settings <slug> <name> <out>   regenerate one settings file

The blueprint is plain JSON — `af` is stdlib-only on purpose (it runs inside agents' panes
and in a warden loop that must survive a machine with no venv activated, so a third-party
parser is not an option), and `json` is the one structured format the standard library
already reads without guessing.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import hooks, lifecycle, mailbox, roster, tmux
from .paths import FACTORY_DIR, Paths, paths
from .probe import probe as do_probe
from .nums import intish

HOOKS_DIR = FACTORY_DIR / "hooks"
STATUSLINE_SH = FACTORY_DIR / "statusline.sh"
ROLE_REMINDER = HOOKS_DIR / "role-reminder.sh"
DELEGATE_WALL = HOOKS_DIR / "delegate-wall.sh"
DELEGATE_PROGRESS = HOOKS_DIR / "delegate-progress.sh"
SPAWN_GATE = HOOKS_DIR / "spawn-gate.sh"
READ_WALL = HOOKS_DIR / "read-wall.sh"
LIMIT_HOOK = HOOKS_DIR / "limit-hook.sh"
SESSION_START = HOOKS_DIR / "session-start.sh"

# Every hook the settings file installs, plus the statusline. A hook that cannot execute
# FAILS OPEN: Claude Code reports "hook error … status code" and runs the tool anyway. So a
# delegate-wall without its +x bit is not a wall — it is a wall-shaped hole, and nothing in
# the agent's output says so. (Observed: an agent sailed straight through a chmod-less wall
# and wrote the file it was supposed to be denied.)
PREFLIGHT = (ROLE_REMINDER, DELEGATE_WALL, DELEGATE_PROGRESS, SPAWN_GATE, READ_WALL,
             LIMIT_HOOK, SESSION_START, STATUSLINE_SH)

DEFAULT_BULK_LINES = 40


# ======================================================================================
# the blueprint loader
# ======================================================================================
class SquadSpecError(Exception):
    """The blueprint cannot be read, or means something we would have to guess at."""


def load_from_string(text: str) -> dict:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise SquadSpecError(f"blueprint is not valid JSON: {e}") from e
    return doc


def load(path: str | Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SquadSpecError(f"cannot read blueprint {path}: {e}") from e
    return load_from_string(text)


# ======================================================================================
# the resolved squad
# ======================================================================================
def flag(v: object, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "required", "full", "on")


def dlevel(v: object, default: str = "") -> str:
    """delegate is three-valued, not boolean:

      required  hard wall — every write outside work/ is denied, whatever its size
      advised   never blocks; a BULK write outside work/ gets a note in the model's context
                suggesting delegate-to-local-model. The default.
      ''        no hook at all

    An UNKNOWN value is a hard error, not a shrug. It used to fall through to '' — no hook at
    all — so `delegate: requird` (typo) on a station meant to be walled spawned with no wall,
    no advisory and no delegate clause in its prompts, and nothing said a word. A typo
    maximised the downgrade: it failed open past even the default.

    The alias table itself is hooks.delegate_level — the same one the wall enforces at
    runtime. Two tables would be two answers to "is this agent walled".
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return "advised" if v else ""
    try:
        lvl = hooks.delegate_level(str(v), strict=True)
    except hooks.DelegateError:
        raise SquadSpecError(
            f"delegate: {v!r} is not one of required | advised | no — refusing to spawn a "
            f"squad whose enforcement you did not mean.") from None
    # hooks says "no" where the blueprint has always said "" (= install no hook at all).
    return "" if lvl == "no" else lvl


@dataclass(frozen=True)
class Station:
    slug: str
    work: str
    name: str
    role: str
    parent: str
    model: str
    delegate: str
    caveman: str
    soft: str
    hard: str
    peers: str
    brief: str
    cwd: str = ""   # the blueprint's declared project dir; "" falls back to AF_CWD/getcwd()


def plan(bp: str | Path, cwd: str | None = None) -> list[Station]:
    """The blueprint, flattened: `count:` expanded, defaults resolved, parents defaulted.

    Resolved HERE and nowhere else, so `squad plan` shows exactly what `squad up` will do —
    and so a blueprint that dies on station 3 spawns nothing at all. (The bash streamed its
    rows and `up` read them as they came: a validation failure on the third station had
    already spawned the first two.)
    """
    doc = load(bp)
    if not isinstance(doc, dict):
        raise SquadSpecError("blueprint is not a mapping")
    # The blueprint may declare its own project directory — so agents spawn where the squad
    # actually works, not wherever `af squad up` happened to be invoked from. Optional: a
    # blueprint without "cwd" behaves exactly as before (env/getcwd fallback).
    if not cwd:
        declared = doc.get("cwd")
        if declared:
            # Resolved relative to the BLUEPRINT FILE's own directory, NOT the invoking
            # shell's — a relative "../proj" must name the same project dir every time this
            # blueprint is used, regardless of which directory `squad up`/`add`/`status`/
            # `heal` happens to run from today. (Plain os.path.abspath() would resolve it
            # against the invoking shell instead, silently reintroducing the exact
            # invocation-dependent behavior this field exists to remove.)
            cwd = os.path.join(os.path.dirname(os.path.abspath(str(bp))), str(declared))
        else:
            cwd = os.environ.get("AF_CWD") or os.getcwd()
    cwd = os.path.abspath(cwd)
    slug = doc.get("slug") or os.path.basename(cwd)
    # The delegate-wall compares Claude's ALWAYS-ABSOLUTE file_path against this, so a
    # relative "./work" would block every agent from writing its own report — and then tell
    # it to write its report. Resolve once, here.
    work = os.path.abspath(os.path.join(cwd, str(doc.get("work") or "./work")))
    d = doc.get("defaults") or {}
    agents = doc.get("agents") or {}
    if not isinstance(d, dict) or not isinstance(agents, dict):
        raise SquadSpecError("`defaults:` and `agents:` must be mappings")

    names: list[tuple[str, dict]] = []
    for name, cfg in agents.items():
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise SquadSpecError(f"agent {name!r} must be a mapping of settings")
        n = int(cfg.get("count") or 1)
        for i in range(n):
            nm = f"{name}{i + 1}" if cfg.get("count") else name
            # `orchestrator` is the reserved name of the SESSION that drives the squad. A
            # station called that would share its mailbox (orchestrator.jsonl) and would be
            # taken for the orchestrator by the sweep guard — it would start compacting its
            # own peers, and never be compacted itself. Give the role, not the name.
            if nm == "orchestrator":
                raise SquadSpecError(
                    "'orchestrator' is a reserved agent name (it is the mailbox of the "
                    "session driving the squad). Name the station something else and give it "
                    "`role: orchestrator`.")
            names.append((nm, cfg))

    allnames = [n for n, _ in names]
    # The squad's own orchestrator, by ROLE not by name. The default parent used to be the
    # literal 'orc' — my own example name, leaked into the code. Name your top station 'boss'
    # and every other station reported to a nonexistent 'orc': mail into a mailbox nobody
    # reads, and not one error anywhere.
    orch = next((n for n, c in names if (c.get("role") or "worker") == "orchestrator"), "")

    out: list[Station] = []
    for nm, cfg in names:
        role = str(cfg.get("role") or "worker")
        parent = str(cfg.get("parent") or ("" if role == "orchestrator" else orch))
        model = str(cfg.get("model") or d.get("model") or "")
        delegate = dlevel(cfg.get("delegate"), dlevel(d.get("delegate"), "advised"))
        caveman = "1" if flag(cfg.get("caveman"), flag(d.get("caveman"))) else ""
        soft = str(cfg.get("compact_soft") or d.get("compact_soft") or "")
        hard = str(cfg.get("compact_hard") or d.get("compact_hard") or "")
        brief = str(cfg.get("brief") or "").strip()
        peers = ",".join(p for p in allnames if p != nm)
        out.append(Station(slug=str(slug), work=work, name=nm, role=role, parent=parent,
                           model=model, delegate=delegate, caveman=caveman, soft=soft,
                           hard=hard, peers=peers, brief=brief, cwd=cwd))
    return out


def bulk_lines(bp: str | Path) -> int:
    """The advisory threshold. Read from `defaults:` ONLY — a top-level `bulk_lines:` is
    ignored, exactly as the bash ignored it (`(yaml…).get("defaults").get("bulk_lines")`).
    Kept rather than fixed: `squad up` and the hooks must agree on where the number lives,
    and the hooks read AF_BULK_LINES out of the env this function fills.
    """
    try:
        d = load(bp).get("defaults") or {}
    except SquadSpecError:
        return _env_bulk()
    v = str((d.get("bulk_lines") if isinstance(d, dict) else "") or "")
    n = intish(v, None)
    return n if n is not None else _env_bulk()


def _env_bulk() -> int:
    v = (os.environ.get("AF_BULK_LINES") or "").strip()
    return intish(v, DEFAULT_BULK_LINES)


# ======================================================================================
# what each station is handed
# ======================================================================================
def settings_json(slug: str, name: str) -> str:
    """A settings file per agent: same hooks for everyone, but they read the agent's ENV, so
    one file shape covers every role. Written per-agent anyway because the agents share a cwd
    — a project-level .claude/settings.json could not give them different rules.

    statusLine is not decoration: it is the ONLY channel that carries
    rate_limits.five_hour.resets_at out of a live session. No CLI reports it. Without it the
    warden knows an agent was cut off by the usage limit but not when the limit lifts — and a
    rescuer that has to guess the time wakes the agent into the same wall.

    StopFailure/rate_limit fires at the instant a turn is killed by that limit. It cannot
    block or retry (Claude Code ignores its output) — it just leaves the marker that tells the
    warden WHICH agents were cut off mid-work, as opposed to idle and fine.

    SessionStart keeps sid-<agent> honest. Claude Code forks the session on --resume, so the
    id written once at spawn names a frozen transcript the moment the agent is resumed; this
    hook fires with the LIVE id on every start/resume and rewrites the sid file, so the warden
    stops reading a dead context number. af/live.py does the same repair from the outside for
    agents whose settings predate this hook.

    EVERY hook command carries `<slug> <name>`. That argument pair is this file's whole
    reason for being per-agent: it is the ONLY channel that survives a fork. Claude Code
    forks a session onto a process claimed from a machine-global spare pool, and that pool
    carries the env of whichever session started the daemon — possibly another squad's, or a
    dead one's. Hooks that read identity from $AF_* alone therefore judge the agent as
    whoever the pool used to be (observed: `inna`'s orc forking onto an `aae1` spare and
    every later hook seeing AF_SLUG=aae1). The settings file is written per agent at spawn
    and passed through verbatim, so args cannot be swapped underneath it; the hook resolves
    the rest from the spec those args name. See af.hooks._bind_identity.
    """
    # Shell-quote (the command runs in a shell) THEN JSON-escape (it is interpolated into a
    # hand-rolled JSON string). Slug is slugified and names are tame in practice, but a `"` in
    # either would otherwise inject a raw quote and produce invalid settings JSON.
    ident = json.dumps(f"{shlex.quote(slug)} {shlex.quote(name)}")[1:-1]
    return f"""{{
  "statusLine": {{ "type": "command", "command": "{STATUSLINE_SH} {ident}", "padding": 0 }},
  "hooks": {{
    "SessionStart": [
      {{ "hooks": [ {{ "type": "command", "command": "{SESSION_START} {ident}", "timeout": 5 }} ] }}
    ],
    "UserPromptSubmit": [
      {{ "hooks": [ {{ "type": "command", "command": "{ROLE_REMINDER} {ident}", "timeout": 5 }} ] }}
    ],
    "PreToolUse": [
      {{ "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          {{ "type": "command", "command": "{DELEGATE_WALL} {ident}", "timeout": 5 }},
          {{ "type": "command", "command": "{SPAWN_GATE} {ident}", "timeout": 5 }}
        ] }},
      {{ "matcher": "Read",
        "hooks": [ {{ "type": "command", "command": "{READ_WALL} {ident}", "timeout": 5 }} ] }},
      {{ "matcher": "Skill",
        "hooks": [ {{ "type": "command", "command": "{DELEGATE_PROGRESS} {ident}", "timeout": 5 }} ] }}
    ],
    "StopFailure": [
      {{ "matcher": "rate_limit",
        "hooks": [ {{ "type": "command", "command": "{LIMIT_HOOK} {ident}", "timeout": 5 }} ] }}
    ]
  }}
}}
"""


def write_settings(slug: str, name: str, out: str | Path) -> Path:
    """Importable, because `revive` needs it: a settings file can be deleted from under a
    spec, and reviving without it means reviving without hooks — i.e. without the wall, with
    nothing saying so. lifecycle._regen_settings shells out to `bash squad.sh settings` today;
    this is what kills that shell-out."""
    f = Path(out)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(settings_json(slug, name), encoding="utf-8")
    return f


def entrypoint_md(st: Station, bulk: int) -> str:
    b = []
    b.append(f"# {st.name} — {st.role}\n\n")
    b.append(f"## Who you are\n\nYou are `{st.name}`, the **{st.role}** station on this "
             f"squad.\n\n")
    if st.parent:
        b.append(f"You report to `{st.parent}`. Send it your results; escalate blockers to "
                 f"it.\n\n")
    b.append(f"## Who you can reach\n\nPeers: {st.peers or 'none'}\n\n```bash\n"
             f"bash $AF_MAIL send --to <agent> --kind <question|blocked|result|done|fyi> "
             f'"..."\nbash $AF_MAIL read      # your inbox (mail is also pushed to you '
             f"automatically)\n```\n\n")
    if st.delegate == "required":
        b.append("## How you work — you are a MINI-ORCHESTRATOR (hard wall)\n\n")
        b.append("You do **not** do the work yourself. You dispatch it and verify what comes "
                 "back:\n\n")
        b.append("1. `delegate-to-local-model` skill — **the** way to get a file written. "
                 "Free, runs in\n   its own process, keeps the work off your context.\n")
        b.append("2. Mail a peer agent that owns the area: `bash $AF_MAIL send --to <agent> "
                 '--kind task "..."`.\n')
        b.append("3. A Task subagent to READ and analyse — never to write (see below).\n\n")
        b.append("When the delegated task can **check itself** — code that must pass tests, an "
                 "edit that\nmust not break a build — reach for the skill's `agent.py "
                 "--write --allow-cmd '<cmd>'`:\nthe worker writes, runs the command, reads "
                 "the failure and fixes, and hands you an\noutcome instead of a blind first "
                 "draft. Then verify cheaply — run the same command\nyourself and read `git "
                 "diff --stat`, not the files.\n\n")
        b.append(f"This is enforced, not advised: a hook blocks your Write/Edit/Bash-writes "
                 f"outside `{st.work}/`,\n")
        b.append("at any size. A Task subagent **inherits the same wall** and is blocked "
                 "identically —\nverified. Do not try to route a write through one; you will "
                 "just loop.\n\n")
        b.append("Verify everything that comes back. Never trust bulk output unread.\n\n")
    elif st.delegate == "advised":
        b.append("## How you work — you are a MINI-ORCHESTRATOR\n\n")
        b.append("Your job is to **dispatch and verify**, not to type out volume yourself.\n\n")
        b.append("Delegate the work that is bulk or mechanical — many items to convert or "
                 "classify,\nboilerplate, spec-code, first drafts, big logs to read — and "
                 "cheaply checkable:\n\n")
        b.append("1. `delegate-to-local-model` skill — free, runs in its own process, keeps "
                 "the tokens\n   off your context. This is the main one.\n")
        b.append("2. Mail the peer agent that owns the area: `bash $AF_MAIL send --to <agent> "
                 '--kind task "..."`.\n\n')
        b.append("**Small, surgical edits you just make yourself.** A three-line fix does not "
                 "need an\nexternal model — delegating it costs more than doing it. A hook "
                 f"will note it if a\nwrite looks like bulk ({bulk}+ lines) outside "
                 f"`{st.work}/`; it does not block you, it is telling\nyou the cheaper route "
                 "exists.\n\n")
        b.append("Always verify what comes back. Never trust bulk output unread.\n\n")
    b.append(f"## Your report\n\nWrite it to `{st.work}/{st.name}.md`. One file, kept current "
             f"— it is how the squad sees your work.\n\n")
    b.append("## Your brief\n\n")
    b.append(st.brief)
    b.append("\n")
    return "".join(b)


def write_entrypoint(st: Station, bulk: int) -> Path:
    d = Path(st.work)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"entrypoint-{st.name}.md"
    f.write_text(entrypoint_md(st, bulk), encoding="utf-8")
    return f


def sysprompt(st: Station, ep: Path) -> str:
    """The invariant goes in the SYSTEM PROMPT (it survives compaction); the full brief goes
    in the entrypoint file (too long to repeat, and re-readable)."""
    s = f"You are '{st.name}', the {st.role} station on the '{st.slug}' squad."
    if st.parent:
        s += f" You report to '{st.parent}'."
    s += (f" Read {ep} NOW - it is your brief, your chain of command and your working rules "
          f"- then follow it.")
    # NOT "…or a Task subagent": a Task subagent runs in the same process, inherits the same
    # --settings, and is blocked by the same wall (verified). Advertising it as a route sends
    # the agent into a loop it cannot exit.
    if st.delegate == "required":
        s += (" You are a mini-orchestrator: you do NOT do work directly. To get a file "
              "WRITTEN, use the delegate-to-local-model skill (it runs in its own process) or "
              "mail the peer who owns the area; a Task subagent inherits your wall and cannot "
              "write. Then verify the result. A hook enforces this.")
    # advised: say what to delegate AND what not to. Tell an agent only "delegate" and it
    # delegates one-line fixes to an external LLM — which is what the old default did.
    if st.delegate == "advised":
        s += (" You are a mini-orchestrator: delegate work that is bulk or mechanical (many "
              "items, boilerplate, spec-code, first drafts, big logs) via the "
              "delegate-to-local-model skill, or mail the peer who owns the area - then "
              "verify what comes back. Small surgical edits you make yourself; delegating a "
              "three-line fix costs more than doing it.")
    if st.caveman == "1":
        s += (" Answer tersely - drop articles, filler and hedging; keep every technical fact "
              "exact.")
    return s


# ======================================================================================
# preflight
# ======================================================================================
def preflight() -> bool:
    bad = False
    for h in PREFLIGHT:
        if not h.is_file():
            print(f"[squad] FATAL: missing hook {h}")
            bad = True
            continue
        if not os.access(h, os.X_OK):
            try:
                os.chmod(h, h.stat().st_mode | 0o111)
            except OSError:
                pass
        if not os.access(h, os.X_OK):
            print(f"[squad] FATAL: hook not executable and chmod failed: {h}")
            bad = True
    if bad:
        print("[squad] refusing to spawn — enforcement hooks would fail open.")
        return False
    return True


# ======================================================================================
# commands
# ======================================================================================
def _p(slug: str) -> Paths:
    return paths(slug)


def _slug_owner(slug: str) -> str:
    """The blueprint path recorded in this slug's squad.json, or "" if the slug has no squad
    (no file, unreadable, or an old squad.json from before the blueprint field). "" means
    BOTH "free" and "cannot prove it is ours" — the caller separates those."""
    try:
        return roster.load(_p(slug)).blueprint or ""
    except Exception:
        return ""


def _slug_is_free(slug: str) -> bool:
    """No squad has ever been materialised under this slug — no squad.json at all. A fresh
    `up` may take it without clobbering another team's mailboxes/specs/state."""
    return not _p(slug).squad_file.is_file()


def _slug_candidates(base: str):
    yield base
    for i in range(1, 1000):
        yield f"{base}{i}"


def _resolve_slug(base: str, bp_resolved: str) -> str:
    """Where THIS blueprint's team lives, as one slug — the same answer for `up` and for
    every command after it, so they never address different squads.

    TWO passes, and the order is the fix for a real orphaning bug:

      1. OURS WINS. Scan base, base1, base2, … and return the first slug whose recorded
         blueprint IS this one. A team bumped to `base1` on `up` recorded us there; it must
         keep being found at `base1` for the rest of its life — including after the FOREIGN
         squad that forced the bump is torn down and `base` falls free again. A one-pass
         "first free or ours" walk returned that newly-free `base` instead, so `squad down`
         targeted an empty slug and left the real team at `base1` running. Preferring ours
         over free closes that.
      2. NO EXISTING TEAM → pick a home for a fresh `up`: the first slug that is free, or
         (base taken by another) the first bump past it that is free. `down`/`status` on a
         team that was never brought up land here too and get `base`; operating on an empty
         slug is a harmless no-op.
    """
    for slug in _slug_candidates(base):
        if _slug_owner(slug) == bp_resolved:
            return slug                      # pass 1: our team, wherever it landed
    for slug in _slug_candidates(base):
        owner = _slug_owner(slug)
        if not owner and _slug_is_free(slug):
            return slug                      # pass 2: a genuinely empty slug
    raise SquadSpecError(f"every slug from {base!r} to {base}999 is taken by another squad")


def _effective_paths(bp: str, stations: list[Station]) -> Paths:
    """The Paths the LIVE team uses — base slug resolved to its bumped variant if `up` moved
    it. Every command past `up` goes through here so they all address the same squad — which
    is also why the blueprint's declared cwd (if any) is applied here rather than only in
    `cmd_up`: `cmd_add`/`heal` can spawn a station too, and it must land in the same project
    directory as the rest of the team, not wherever this particular command was invoked from."""
    if not stations:
        return paths()
    p = _p(_resolve_slug(stations[0].slug, str(Path(bp).resolve())))
    if stations[0].cwd:
        p = replace(p, cwd=Path(stations[0].cwd))
    return p


def cmd_plan(bp: str) -> int:
    stations = plan(bp)
    print(f"{'NAME':<10} {'ROLE':<14} {'MODEL':<8} {'PARENT':<8} {'DELEGATE':<9} PEERS")
    for s in stations:
        print(f"{s.name:<10} {s.role:<14} {s.model or 'default':<8} {s.parent or '-':<8} "
              f"{s.delegate or '-':<9} {s.peers}")
    return 0


def _resume_flag(st: Station, p: Paths) -> tuple[str, str]:
    """--resume, but only on a session we can PROVE is still there.

    This is the only way to give a constitution to an agent that already has a memory. A
    station with no recorded sid, or whose log has been purged, is spawned FRESH and SAID SO
    — a silent fresh spawn under a flag that promised continuity is how you lose a day's
    context and only notice tomorrow.
    """
    from . import manifest
    try:
        sid = p.sid_file(st.name).read_text(encoding="utf-8").strip()
    except OSError:
        sid = ""
    if sid and manifest.session_log_exists(sid, p):
        return f"--resume {sid} ", sid
    if sid:
        print(f"[squad] {st.name:<10} ⚠ session {sid} recorded but its log is GONE — spawning "
              f"FRESH (no memory)")
    else:
        print(f"[squad] {st.name:<10} ⚠ no recorded session — spawning FRESH (no memory)")
    return "", ""


def _spawn_station(st: Station, p: Paths, bp: str, bulk: int, resume: bool) -> str:
    """Bring up ONE station. Shared by `up` (every station in the blueprint) and `add` (one
    named station added to an already-running squad) so the two never drift apart on what
    "spawn" means. Returns "spawned" | "skipped" (already running) | "failed"."""
    dlabel = {"required": "  [wall]", "advised": "  [advise]"}.get(st.delegate, "")
    # `up` kills any existing session for the name before relaunching. Run `squad up` twice
    # — a habit, after an edit to one station's brief — and it would tear down the whole
    # live squad, every agent's TUI, mid-task. Alive stays alive.
    if tmux.has_session(p.session(st.name)):
        # Left alone means NOTHING was applied: not the brief, not the settings, not the
        # spec. Say that. Reporting "already running" next to a blueprint you just edited
        # reads as "your edit is live", and it is not.
        print(f"[squad] {st.name:<10} {st.role:<14} {st.model or 'default':<8} already "
              f"running — LEFT ALONE (blueprint edits NOT applied)")
        print(f"[squad]            to apply them:  af down {st.name} && af squad up {bp}")
        if not p.spec_file(st.name).is_file():
            print(f"[squad]            ⚠ it has no spec (spawned by an older version) — it "
                  f"would revive with NO role and NO hooks")
        return "skipped"

    ep = write_entrypoint(st, bulk)
    stf = p.settings_file(st.name)
    write_settings(st.slug, st.name, stf)
    if not hooks.hooks_ok(stf):
        # preflight already passed, so this is the belt: a settings file that installs no
        # runnable hook is an agent with no wall and nothing saying so.
        print(f"[squad] FATAL: settings for {st.name} install hooks that would FAIL OPEN "
              f"({stf}) — not spawning it.", file=sys.stderr)
        return "failed"

    rflag, sid = _resume_flag(st, p) if resume else ("", "")
    flags = (f"{rflag}--settings {stf} "
             f"{f'--model {st.model} ' if st.model else ''}"
             f"--append-system-prompt {shlex.quote(sysprompt(st, ep))} "
             f"{os.environ.get('AI_CLAUDE_FLAGS', '')}").strip()

    env = dict(os.environ)
    env.update({
        "AF_SLUG": st.slug, "AF_ROLE": st.role, "AF_PARENT": st.parent, "AF_PEERS": st.peers,
        "AF_DELEGATE": st.delegate, "AF_BULK_LINES": str(bulk), "AF_CAVEMAN": st.caveman,
        "AF_WORK": st.work,
        "AI_COMPACT_SOFT": st.soft or os.environ.get("AI_COMPACT_SOFT", "") or "200000",
        "AI_COMPACT_HARD": st.hard or os.environ.get("AI_COMPACT_HARD", "") or "500000",
        "AI_CLAUDE_FLAGS": flags,
        "AI_NOTIFY_OFF": "1",
    })
    # lifecycle.up narrates a spawn (`ai up` did too, and squad.sh threw it away with
    # >/dev/null 2>&1). Its stdout is noise here — but its stderr is NOT: those are the
    # "spec could not be written / it will revive with no wall" warnings, and bash was
    # silently eating them. They go to the operator.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lifecycle.up(st.name, p, env)

    if tmux.has_session(p.session(st.name)):
        tail = f"{'← ' + st.parent if st.parent else ''}{dlabel}" \
               f"{f'  [resumed {sid}]' if rflag else ''}"
        print(f"[squad] {st.name:<10} {st.role:<14} {st.model or 'default':<8} {tail}")
        return "spawned"
    print(f"[squad] {st.name:<10} FAILED TO LAUNCH — check: python3 -m af up {st.name}")
    sys.stderr.write(buf.getvalue())
    return "failed"


def cmd_up(bp: str, resume: bool = False) -> int:
    if not preflight():
        return 1
    try:
        stations = plan(bp)
    except SquadSpecError as e:
        print(f"[squad] FATAL: {e}", file=sys.stderr)
        print("[squad] blueprint did not validate — nothing was spawned.")
        return 1
    if not stations:
        print("[squad] blueprint has no agents.")
        return 1

    bulk = bulk_lines(bp)
    bp_resolved = str(Path(bp).resolve())
    slug = _resolve_slug(stations[0].slug, bp_resolved)
    if slug != stations[0].slug:
        print(f"[squad] slug '{stations[0].slug}' is another squad's — using '{slug}' so this "
              f"team gets its own mailboxes, specs and state.")
        stations = [replace(st, slug=slug) for st in stations]
    p = _p(slug)
    if stations[0].cwd:
        p = replace(p, cwd=Path(stations[0].cwd))
    n = skipped = 0

    for st in stations:
        outcome = _spawn_station(st, p, bp, bulk, resume)
        if outcome == "spawned":
            n += 1
        elif outcome == "skipped":
            skipped += 1

    # squad.json is the one file that also answers "where did this team come from, and
    # when": no separate line.json, no second writer to keep in sync.
    roster.set_meta(str(Path(bp).resolve()), int(time.time()), p)

    skipmsg = f", {skipped} left alone (already running)" if skipped else ""
    print(f"[squad] {n} stations up{skipmsg}. attach: tmux attach -t ai-{slug}-<name>")
    print('[squad] talk to the squad:  af post <agent> "…"   |   read replies:  af mail   |   '
          "see it all:  af ledger")

    # Start the limit watcher WITH the squad, not after it. The account-wide usage limit kills
    # every agent and the orchestrator session at the same instant — there is nobody left to
    # start a rescuer once it lands. It has to already be running, and it has to be something
    # that spends no tokens. Idempotent: re-running `squad up` does not start a second one.
    from . import warden
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        warden.watch(p=p)
    for ln in out.getvalue().splitlines():
        print(f"[squad] {ln}")

    # The postmaster is the squad's OTHER daemon — squad.json reconciliation and the mail
    # ring-catch, on a much shorter clock than the warden's five-minute one. Started
    # alongside it for the same reason: a team working unattended never calls a
    # command, so nothing else will ever start it. Idempotent, like warden.watch.
    from . import postmaster
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        postmaster.watch(p=p)
    for ln in out2.getvalue().splitlines():
        print(f"[squad] {ln}")
    return 0


def cmd_add(bp: str, name: str, resume: bool = False) -> int:
    """Bring up ONE new station that already has an entry in the blueprint (someone edited
    it in — a wizard, or by hand) but has never been spawned. Everyone else on the squad is
    untouched: this is `squad up` narrowed to a single name, for when you don't want the
    "already running — LEFT ALONE" line printed for the whole team just to add one agent."""
    if not preflight():
        return 1
    try:
        stations = plan(bp)
    except SquadSpecError as e:
        print(f"[squad] FATAL: {e}", file=sys.stderr)
        return 1
    st = next((s for s in stations if s.name == name), None)
    if st is None:
        print(f"[squad] FATAL: {name!r} is not a station in {bp} — add it under `agents:` "
              f"first, then run `squad add` again.", file=sys.stderr)
        return 1

    # Address the LIVE team, which may sit under a bumped slug (see _resolve_slug). Re-stamp
    # the station so its own settings/env carry that slug, not the blueprint's base.
    p = _effective_paths(bp, stations)
    st = replace(st, slug=p.slug)
    bulk = bulk_lines(bp)
    outcome = _spawn_station(st, p, bp, bulk, resume)
    if outcome == "failed":
        return 1
    if outcome == "spawned":
        roster.set_meta(str(Path(bp).resolve()), int(time.time()), p)
        print(f"[squad] talk to it:  af post {name} \"…\"")
    return 0


def _drop_from_blueprint(bp: str | Path, name: str) -> bool:
    """Remove `name` from `agents:` so the blueprint stays the truth after `squad remove`.
    Returns False (and leaves the file untouched) if `name` is not a literal key — which
    happens when it is one replica of a `count:`-expanded entry, a group the schema has no
    way to remove one member of.

    Written via temp-file + os.replace, same as roster._write — a `squad remove` that dies
    mid-write must not leave a half-written blueprint behind."""
    path = Path(bp)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SquadSpecError(f"cannot read blueprint {bp} to remove {name!r}: {e}") from e
    agents = doc.get("agents") or {}
    if name not in agents:
        return False
    agents.pop(name)
    doc["agents"] = agents
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def cmd_remove(bp: str, name: str) -> int:
    """Take one station off the squad for good: kill its session, delete its spec/settings/sid
    (not just mark it down — `af down` keeps those so the station stays revivable; `remove`
    means gone, so `af ledger` — which lists agents by spec file — stops showing it, and `af
    revive` refuses by default instead of resurrecting it from the manifest), drop its roster
    row, and delete it from the blueprint so a later `squad up` never treats it as "left alone"
    or `heal` as "crashed"."""
    try:
        stations = plan(bp)
    except SquadSpecError as e:
        print(f"[squad] FATAL: {e}", file=sys.stderr)
        return 1
    if not any(s.name == name for s in stations):
        print(f"[squad] FATAL: {name!r} is not a station in {bp} — nothing to remove.",
              file=sys.stderr)
        return 1

    slug = stations[0].slug
    p = _p(slug)
    with contextlib.redirect_stdout(io.StringIO()):
        lifecycle.down(name, p)
    p.spec_file(name).unlink(missing_ok=True)
    p.settings_file(name).unlink(missing_ok=True)
    p.sid_file(name).unlink(missing_ok=True)
    roster.remove(name, p)
    try:
        dropped = _drop_from_blueprint(bp, name)
    except SquadSpecError as e:
        print(f"[squad] {name} removed — session killed, spec gone, dropped from roster. "
              f"FATAL: could not update {bp}: {e}", file=sys.stderr)
        return 1
    if dropped:
        print(f"[squad] {name} removed — session killed, dropped from roster and {bp}.")
    else:
        print(f"[squad] {name} removed — session killed, dropped from roster. NOT found as "
              f"a literal key in {bp} (likely one replica of a `count:` group) — edit the "
              f"blueprint by hand if you meant to drop it from there too.")
    return 0


def cmd_status(bp: str) -> int:
    stations = plan(bp)
    p = _effective_paths(bp, stations)
    for st in stations:
        pr = do_probe(st.name, p)
        alive = "up" if pr.alive else "down"
        ctx = pr.ctx if (pr.alive and pr.ctx) else 0
        print(f"  {st.name:<10} {alive:<5} ctx={str(ctx):<9} unread={mailbox.unread(st.name, p)}")
    return 0


def cmd_down(bp: str) -> int:
    stations = plan(bp)
    p = _effective_paths(bp, stations)
    for st in stations:
        with contextlib.redirect_stdout(io.StringIO()):
            lifecycle.down(st.name, p)
        print(f"[squad] {st.name} down")

    # The daemons come up WITH the squad (see cmd_up) so they must go down with it. They did
    # not: `up` started them, `down` stopped nothing, and each pair outlived the team it was
    # hired to watch — looping, re-reconciling a roster of dead stations and ring-catching
    # mailboxes with no panes behind them, until something killed them by hand. Found three
    # wardens stacked up this way on one machine, one per slug ever brought up, none ever
    # stopped. Both stops are idempotent and say nothing when there is no daemon to stop.
    from . import postmaster, warden
    for mod in (warden, postmaster):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                mod.stop(p=p)
        except Exception as e:      # a daemon that will not die must not fail the teardown
            print(f"[squad] could not stop {mod.__name__.rsplit('.', 1)[-1]}: {e}",
                  file=sys.stderr)
            continue
        for ln in out.getvalue().splitlines():
            print(f"[squad] {ln}")
    return 0


def cmd_heal(bp: str, dry_run: bool = False, restart_idle: bool = False) -> int:
    from . import heal as healmod
    stations = plan(bp)
    if not stations:
        print("[heal] blueprint has no agents.")
        return 0
    p = _effective_paths(bp, stations)
    return healmod.heal(stations, p, healmod.Options(dry_run=dry_run, restart_idle=restart_idle))


def cmd_settings(slug: str, name: str, out: str) -> int:
    # Preflights first: `up` refuses to spawn into a fail-open state, and this path had no
    # reason to be the one that quietly hands out a settings file pointing at a hook that
    # cannot execute.
    if not preflight():
        return 1
    f = write_settings(slug, name, out)
    print(f"[squad] wrote {f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af.squad", description="bring up a squad of agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("plan", help="the resolved squad, without spawning it")
    q.add_argument("blueprint")
    q = sub.add_parser("up", help="generate briefs + settings and spawn every station")
    q.add_argument("--resume", "--adopt", dest="resume", action="store_true",
                   help="bring each station back ON ITS OLD SESSION (memory kept)")
    q.add_argument("blueprint")
    q = sub.add_parser("status", help="who's alive, context size, unread mail")
    q.add_argument("blueprint")
    q = sub.add_parser("down", help="stop every station on the squad")
    q.add_argument("blueprint")
    q = sub.add_parser("add", help="spawn ONE new station already in the blueprint, "
                                   "leaving the rest of the squad untouched")
    q.add_argument("--resume", "--adopt", dest="resume", action="store_true",
                   help="bring it back ON ITS OLD SESSION (memory kept)")
    q.add_argument("blueprint")
    q.add_argument("name")
    q = sub.add_parser("remove", help="kill ONE station, drop it from the roster AND the "
                                      "blueprint")
    q.add_argument("blueprint")
    q.add_argument("name")
    q = sub.add_parser("heal", help="diagnose every station and repair breakage without "
                                    "losing context (sid drift, stale settings, crashed agents)")
    q.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="report what is broken and what would change — touch nothing")
    q.add_argument("--restart-idle", dest="restart_idle", action="store_true",
                   help="also restart LIVE-but-idle agents to load regenerated settings "
                        "(never a busy one; memory kept via --resume)")
    q.add_argument("blueprint")
    q = sub.add_parser("settings", help="(internal) regenerate one agent's settings file")
    q.add_argument("slug")
    q.add_argument("name")
    q.add_argument("out")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "plan":
            return cmd_plan(a.blueprint)
        if a.cmd == "up":
            return cmd_up(a.blueprint, a.resume)
        if a.cmd == "status":
            return cmd_status(a.blueprint)
        if a.cmd == "down":
            return cmd_down(a.blueprint)
        if a.cmd == "add":
            return cmd_add(a.blueprint, a.name, a.resume)
        if a.cmd == "remove":
            return cmd_remove(a.blueprint, a.name)
        if a.cmd == "heal":
            return cmd_heal(a.blueprint, a.dry_run, a.restart_idle)
        if a.cmd == "settings":
            return cmd_settings(a.slug, a.name, a.out)
    except SquadSpecError as e:
        # A blueprint that does not validate must STOP the command, not decorate it. (`squad
        # plan` printed its header, printed the FATAL to stderr — and still exited 0, so
        # `squad plan && squad up` sailed on into `up`.)
        print(f"[squad] FATAL: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
