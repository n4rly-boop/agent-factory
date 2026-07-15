"""line — bring up a whole production line of agents from one blueprint.

The problem it solves: a line's design (who exists, who reports to whom, who may only
delegate, who gets the cheap model) is the part that is easiest to get wrong and hardest
to remember. Written as a prompt it decays — you spawn five agents, tell each its role
once, and thirty turns later nobody remembers they were supposed to delegate. Written as
a blueprint it is configuration: applied identically to every agent, every time, and
enforced by hooks rather than hoped for.

    python3 -m af.line plan   <bp.yml>          the resolved line — WITHOUT spawning it
    python3 -m af.line up     [--resume] <bp>   briefs + settings + specs, then spawn
    python3 -m af.line status <bp.yml>          who's alive, context size, unread mail
    python3 -m af.line down   <bp.yml>          stop every station
    python3 -m af.line settings <slug> <name> <out>   regenerate one settings file

WHY THE YAML IS PARSED BY HAND. `af` is stdlib-only on purpose: it runs inside agents'
panes and in a warden loop that must survive a machine with no venv activated, and a
`ModuleNotFoundError: yaml` in that loop is a line that never gets compacted and never
gets woken. line.sh could afford `import yaml` because it shelled out to whatever python3
happened to have it. So blueprint.py below accepts exactly the subset the documented
blueprint uses — scalars, two levels of nested mappings (block OR the inline `{k: v, …}`
flow form the SKILL.md example itself uses), block scalars for the brief — and REFUSES
anything with no meaning here (flow SEQUENCES `[a, b]`, anchors `&x`/`*x`, tags `!!str`)
rather than misreading it. That refusal is the whole safety property: a blueprint that
half-parses is a station with half a constitution.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import hooks, lifecycle, mailbox, tmux
from .paths import FACTORY_DIR, Paths, paths
from .probe import probe as do_probe
from .nums import intish

HOOKS_DIR = FACTORY_DIR / "hooks"
STATUSLINE_SH = FACTORY_DIR / "statusline.sh"
ROLE_REMINDER = HOOKS_DIR / "role-reminder.sh"
DELEGATE_WALL = HOOKS_DIR / "delegate-wall.sh"
LIMIT_HOOK = HOOKS_DIR / "limit-hook.sh"

# Every hook the settings file installs, plus the statusline. A hook that cannot execute
# FAILS OPEN: Claude Code reports "hook error … status code" and runs the tool anyway. So a
# delegate-wall without its +x bit is not a wall — it is a wall-shaped hole, and nothing in
# the agent's output says so. (Observed: an agent sailed straight through a chmod-less wall
# and wrote the file it was supposed to be denied.)
PREFLIGHT = (ROLE_REMINDER, DELEGATE_WALL, LIMIT_HOOK, STATUSLINE_SH)

DEFAULT_BULK_LINES = 40


# ======================================================================================
# the blueprint parser
# ======================================================================================
class BlueprintError(Exception):
    """The blueprint cannot be read, or means something we would have to guess at."""


_KEYLINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)\s*:(?P<rest>.*)$")
_FLOWKEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_BLOCK = re.compile(r"^([|>])([+-]?)\s*(?:#.*)?$")

# YAML 1.1, which is what PyYAML's safe_load implements — and therefore what line.sh saw.
# `delegate: no` is the BOOLEAN False, not the string "no"; the level resolver below is
# written knowing that, exactly as the bash one was.
_TRUE = ("true", "yes", "on")
_FALSE = ("false", "no", "off")
_NULL = ("", "~", "null")


def _strip_comment(s: str) -> str:
    """A `#` starts a comment only at the start of a token — `foo#bar` is the scalar
    "foo#bar", and neither is a `#` inside quotes."""
    out, quote = [], ""
    for i, ch in enumerate(s):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(raw: str, where: int) -> object:
    v = _strip_comment(raw).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\n', "\n").replace('\\t', "\t") \
                         .replace('\\"', '"').replace("\\\\", "\\")
        return inner
    if v[:1] == "{":
        # A flow MAPPING is accepted, but only where a whole mapping is expected — as an
        # agent's value or as `defaults:` — which _parse routes to _flow_mapping before it
        # ever reaches here. Seeing one in a plain-scalar slot (e.g. `model: {…}`) means it
        # was written where a scalar belongs.
        raise BlueprintError(
            f"line {where}: {v!r} — a flow mapping is only meaningful as a whole mapping "
            f"value (an agent, or `defaults:`), not in a scalar position.")
    if v[:1] in ("[", "&", "*", "!"):
        raise BlueprintError(
            f"line {where}: {v!r} — flow sequences, anchors and tags are not part of the "
            f"blueprint language. Use plain scalars, nested keys, or a `|` block.")
    low = v.lower()
    if low in _NULL:
        return None
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if _INT.match(v):
        return int(v)
    if _FLOAT.match(v):
        return float(v)
    return v


def _flow_tokens(body: str) -> list[str]:
    """Split a flow-mapping body on TOP-LEVEL commas — respecting quotes (so a quoted brief
    full of commas survives whole) and nested brackets (so a nested `{…}` we mean to reject
    stays in one piece for a clear error rather than being shredded at its inner comma)."""
    toks: list[str] = []
    buf: list[str] = []
    quote = ""
    depth = 0
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in "{[":
            depth += 1
            buf.append(ch)
            continue
        if ch in "}]":
            depth -= 1
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            toks.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    toks.append("".join(buf))
    return toks


def _flow_kv(tok: str, where: int) -> tuple[str, object]:
    """One `key: value` inside a flow mapping. The split is on the FIRST top-level colon —
    a colon inside the quoted value (`brief: "own it: dispatch"`) is not it."""
    quote = ""
    depth = 0
    cut = -1
    for i, ch in enumerate(tok):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch in "{[":
            depth += 1
            continue
        if ch in "}]":
            depth -= 1
            continue
        if ch == ":" and depth == 0:
            cut = i
            break
    if cut < 0:
        raise BlueprintError(
            f"line {where}: {tok.strip()!r} inside a flow mapping is not `key: value`.")
    key = tok[:cut].strip()
    rawval = tok[cut + 1:].strip()
    if not _FLOWKEY.match(key):
        raise BlueprintError(
            f"line {where}: {key!r} is not a valid key in a flow mapping (keys are bare "
            f"identifiers, as in a block mapping).")
    if rawval[:1] == "{":
        raise BlueprintError(
            f"line {where}: nested flow mapping {rawval!r} — a flow mapping may not contain "
            f"another. No blueprint nests them; use block form for anything two levels deep.")
    if rawval[:1] == "[":
        raise BlueprintError(
            f"line {where}: flow sequence {rawval!r} inside a flow mapping is not part of "
            f"the blueprint language.")
    return key, _scalar(rawval, where)


def _flow_mapping(raw: str, where: int) -> dict:
    """`orc: { role: ..., model: ..., brief: "..." }` — the inline form the SKILL.md
    blueprint uses. Accepted anywhere a block mapping is: as an agent's value, or as
    `defaults:`. Single-line only (no blueprint spans a flow mapping across lines)."""
    s = _strip_comment(raw).strip()
    if not (s.startswith("{") and s.endswith("}")):
        raise BlueprintError(
            f"line {where}: {raw.strip()!r} — an inline flow mapping must open with '{{' and "
            f"close with a matching '}}' on the same line. Multi-line flow is not supported; "
            f"use block form.")
    out: dict = {}
    for tok in _flow_tokens(s[1:-1]):
        if not tok.strip():
            continue  # a trailing comma, or an empty `{}`
        key, val = _flow_kv(tok, where)
        out[key] = val
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _blank(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _block_scalar(lines: list[str], i: int, style: str, chomp: str,
                  parent_indent: int) -> tuple[str, int]:
    """`brief: |` — the block that follows, dedented to its own first line."""
    body: list[str] = []
    n = len(lines)
    base = None
    while i < n:
        line = lines[i]
        if line.strip() and _indent(line) <= parent_indent:
            break
        if line.strip() and base is None:
            base = _indent(line)
        body.append("" if not line.strip() else line[base or 0:] if base else line)
        i += 1
    # Trailing blank lines belong to the next key, not to this scalar.
    while body and not body[-1].strip():
        body.pop()
    if style == ">":
        # Folded: a single newline becomes a space, a blank line stays a newline.
        folded, buf = [], []
        for ln in body:
            if ln.strip():
                buf.append(ln.strip())
            else:
                folded.append(" ".join(buf))
                buf = []
                folded.append("")
        folded.append(" ".join(buf))
        text = "\n".join(x for x in folded if x != "" or True).strip("\n")
    else:
        text = "\n".join(body)
    if chomp != "-":
        text += "\n"
    return text, i


def _parse(lines: list[str], i: int, indent: int) -> tuple[dict, int]:
    out: dict = {}
    n = len(lines)
    while i < n:
        line = lines[i]
        if _blank(line):
            i += 1
            continue
        ind = _indent(line)
        if ind < indent:
            break
        if ind > indent:
            raise BlueprintError(f"line {i + 1}: unexpected indentation — {line.strip()!r}")
        body = line.strip()
        if body.startswith("- "):
            raise BlueprintError(
                f"line {i + 1}: sequences are not part of the blueprint language "
                f"(agents are a MAPPING of name → settings).")
        m = _KEYLINE.match(body)
        if not m:
            raise BlueprintError(f"line {i + 1}: not `key: value` — {body!r}")
        key, rest = m.group("key"), m.group("rest")
        i += 1
        bs = _BLOCK.match(rest.strip()) if rest.strip() else None
        if bs:
            out[key], i = _block_scalar(lines, i, bs.group(1), bs.group(2), ind)
            continue
        rss = _strip_comment(rest).strip()
        if rss == "":
            # A key with no value: either a nested mapping, or an explicit empty.
            j = i
            while j < n and _blank(lines[j]):
                j += 1
            if j < n and _indent(lines[j]) > ind:
                out[key], i = _parse(lines, j, _indent(lines[j]))
            else:
                out[key] = None
            continue
        if rss[0] == "{":
            # Inline flow mapping — a whole nested mapping on one line. Same standing as the
            # block form below it; the SKILL.md example writes agents this way.
            out[key] = _flow_mapping(rest, i)
            continue
        out[key] = _scalar(rest, i)
    return out, i


def load_from_string(text: str) -> dict:
    if re.search(r"^---\s*$", text, re.M) and text.count("---") > 1:
        raise BlueprintError("multi-document YAML is not a blueprint")
    doc, _ = _parse([l.rstrip("\n") for l in text.replace("\t", "    ").splitlines()], 0, 0)
    return doc


def load(path: str | Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise BlueprintError(f"cannot read blueprint {path}: {e}") from e
    return load_from_string(text)


# ======================================================================================
# the resolved line
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
        raise BlueprintError(
            f"delegate: {v!r} is not one of required | advised | no — refusing to spawn a "
            f"line whose enforcement you did not mean.") from None
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


def plan(bp: str | Path, cwd: str | None = None) -> list[Station]:
    """The blueprint, flattened: `count:` expanded, defaults resolved, parents defaulted.

    Resolved HERE and nowhere else, so `line plan` shows exactly what `line up` will do —
    and so a blueprint that dies on station 3 spawns nothing at all. (The bash streamed its
    rows and `up` read them as they came: a validation failure on the third station had
    already spawned the first two.)
    """
    doc = load(bp)
    if not isinstance(doc, dict):
        raise BlueprintError("blueprint is not a mapping")
    cwd = cwd or os.environ.get("AF_CWD") or os.getcwd()
    slug = doc.get("slug") or os.path.basename(cwd)
    # The delegate-wall compares Claude's ALWAYS-ABSOLUTE file_path against this, so a
    # relative "./work" would block every agent from writing its own report — and then tell
    # it to write its report. Resolve once, here.
    work = os.path.abspath(os.path.join(cwd, str(doc.get("work") or "./work")))
    d = doc.get("defaults") or {}
    agents = doc.get("agents") or {}
    if not isinstance(d, dict) or not isinstance(agents, dict):
        raise BlueprintError("`defaults:` and `agents:` must be mappings")

    names: list[tuple[str, dict]] = []
    for name, cfg in agents.items():
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise BlueprintError(f"agent {name!r} must be a mapping of settings")
        n = int(cfg.get("count") or 1)
        for i in range(n):
            nm = f"{name}{i + 1}" if cfg.get("count") else name
            # `orchestrator` is the reserved name of the SESSION that drives the line. A
            # station called that would share its mailbox (orchestrator.jsonl) and would be
            # taken for the orchestrator by the sweep guard — it would start compacting its
            # own peers, and never be compacted itself. Give the role, not the name.
            if nm == "orchestrator":
                raise BlueprintError(
                    "'orchestrator' is a reserved agent name (it is the mailbox of the "
                    "session driving the line). Name the station something else and give it "
                    "`role: orchestrator`.")
            names.append((nm, cfg))

    allnames = [n for n, _ in names]
    # The line's own orchestrator, by ROLE not by name. The default parent used to be the
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
                           hard=hard, peers=peers, brief=brief))
    return out


def bulk_lines(bp: str | Path) -> int:
    """The advisory threshold. Read from `defaults:` ONLY — a top-level `bulk_lines:` is
    ignored, exactly as the bash ignored it (`(yaml…).get("defaults").get("bulk_lines")`).
    Kept rather than fixed: `line up` and the hooks must agree on where the number lives,
    and the hooks read AF_BULK_LINES out of the env this function fills.
    """
    try:
        d = load(bp).get("defaults") or {}
    except BlueprintError:
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
    """
    return f"""{{
  "statusLine": {{ "type": "command", "command": "{STATUSLINE_SH}", "padding": 0 }},
  "hooks": {{
    "UserPromptSubmit": [
      {{ "hooks": [ {{ "type": "command", "command": "{ROLE_REMINDER}", "timeout": 5 }} ] }}
    ],
    "PreToolUse": [
      {{ "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [ {{ "type": "command", "command": "{DELEGATE_WALL}", "timeout": 5 }} ] }}
    ],
    "StopFailure": [
      {{ "matcher": "rate_limit",
        "hooks": [ {{ "type": "command", "command": "{LIMIT_HOOK}", "timeout": 5 }} ] }}
    ]
  }}
}}
"""


def write_settings(slug: str, name: str, out: str | Path) -> Path:
    """Importable, because `revive` needs it: a settings file can be deleted from under a
    spec, and reviving without it means reviving without hooks — i.e. without the wall, with
    nothing saying so. lifecycle._regen_settings shells out to `bash line.sh settings` today;
    this is what kills that shell-out."""
    f = Path(out)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(settings_json(slug, name), encoding="utf-8")
    return f


def entrypoint_md(st: Station, bulk: int) -> str:
    b = []
    b.append(f"# {st.name} — {st.role}\n\n")
    b.append(f"## Who you are\n\nYou are `{st.name}`, the **{st.role}** station on this "
             f"line.\n\n")
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
             f"— it is how the line sees your work.\n\n")
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
    s = f"You are '{st.name}', the {st.role} station on the '{st.slug}' line."
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
            print(f"[line] FATAL: missing hook {h}")
            bad = True
            continue
        if not os.access(h, os.X_OK):
            try:
                os.chmod(h, h.stat().st_mode | 0o111)
            except OSError:
                pass
        if not os.access(h, os.X_OK):
            print(f"[line] FATAL: hook not executable and chmod failed: {h}")
            bad = True
    if bad:
        print("[line] refusing to spawn — enforcement hooks would fail open.")
        return False
    return True


# ======================================================================================
# commands
# ======================================================================================
def _p(slug: str) -> Paths:
    return paths(slug)


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
        print(f"[line] {st.name:<10} ⚠ session {sid} recorded but its log is GONE — spawning "
              f"FRESH (no memory)")
    else:
        print(f"[line] {st.name:<10} ⚠ no recorded session — spawning FRESH (no memory)")
    return "", ""


def cmd_up(bp: str, resume: bool = False) -> int:
    if not preflight():
        return 1
    try:
        stations = plan(bp)
    except BlueprintError as e:
        print(f"[line] FATAL: {e}", file=sys.stderr)
        print("[line] blueprint did not validate — nothing was spawned.")
        return 1
    if not stations:
        print("[line] blueprint has no agents.")
        return 1

    bulk = bulk_lines(bp)
    slug = stations[0].slug
    p = _p(slug)
    n = skipped = 0

    for st in stations:
        dlabel = {"required": "  [wall]", "advised": "  [advise]"}.get(st.delegate, "")
        # `up` kills any existing session for the name before relaunching. Run `line up` twice
        # — a habit, after an edit to one station's brief — and it would tear down the whole
        # live line, every agent's TUI, mid-task. Alive stays alive.
        if tmux.has_session(p.session(st.name)):
            # Left alone means NOTHING was applied: not the brief, not the settings, not the
            # spec. Say that. Reporting "already running" next to a blueprint you just edited
            # reads as "your edit is live", and it is not.
            print(f"[line] {st.name:<10} {st.role:<14} {st.model or 'default':<8} already "
                  f"running — LEFT ALONE (blueprint edits NOT applied)")
            print(f"[line]            to apply them:  af down {st.name} && af line up {bp}")
            if not p.spec_file(st.name).is_file():
                print(f"[line]            ⚠ it has no spec (spawned by an older version) — it "
                      f"would revive with NO role and NO hooks")
            skipped += 1
            continue

        ep = write_entrypoint(st, bulk)
        stf = p.settings_file(st.name)
        write_settings(slug, st.name, stf)
        if not hooks.hooks_ok(stf):
            # preflight already passed, so this is the belt: a settings file that installs no
            # runnable hook is an agent with no wall and nothing saying so.
            print(f"[line] FATAL: settings for {st.name} install hooks that would FAIL OPEN "
                  f"({stf}) — not spawning it.", file=sys.stderr)
            continue

        rflag, sid = _resume_flag(st, p) if resume else ("", "")
        flags = (f"{rflag}--settings {stf} "
                 f"{f'--model {st.model} ' if st.model else ''}"
                 f"--append-system-prompt {shlex.quote(sysprompt(st, ep))} "
                 f"{os.environ.get('AI_CLAUDE_FLAGS', '')}").strip()

        env = dict(os.environ)
        env.update({
            "AF_SLUG": slug, "AF_ROLE": st.role, "AF_PARENT": st.parent, "AF_PEERS": st.peers,
            "AF_DELEGATE": st.delegate, "AF_BULK_LINES": str(bulk), "AF_CAVEMAN": st.caveman,
            "AF_WORK": st.work,
            "AI_COMPACT_SOFT": st.soft or os.environ.get("AI_COMPACT_SOFT", "") or "200000",
            "AI_COMPACT_HARD": st.hard or os.environ.get("AI_COMPACT_HARD", "") or "500000",
            "AI_CLAUDE_FLAGS": flags,
            "AI_NOTIFY_OFF": "1",
        })
        # lifecycle.up narrates a spawn (`ai up` did too, and line.sh threw it away with
        # >/dev/null 2>&1). Its stdout is noise here — but its stderr is NOT: those are the
        # "spec could not be written / it will revive with no wall" warnings, and bash was
        # silently eating them. They go to the operator.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lifecycle.up(st.name, p, env)

        if tmux.has_session(p.session(st.name)):
            tail = f"{'← ' + st.parent if st.parent else ''}{dlabel}" \
                   f"{f'  [resumed {sid}]' if rflag else ''}"
            print(f"[line] {st.name:<10} {st.role:<14} {st.model or 'default':<8} {tail}")
            n += 1
        else:
            print(f"[line] {st.name:<10} FAILED TO LAUNCH — check: python3 -m af up {st.name}")
            sys.stderr.write(buf.getvalue())

    # line.json: the line-level facts no per-agent spec can hold — which blueprint this line
    # came from, and who is on it. Written once, by the single process that brought the line
    # up, so it has no concurrent writer.
    import json
    p.specdir.mkdir(parents=True, exist_ok=True)
    p.line_file.write_text(json.dumps({
        "slug": slug,
        "blueprint": str(Path(bp).resolve()),
        "agents": [s.name for s in stations],
        "created": int(time.time()),
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    skipmsg = f", {skipped} left alone (already running)" if skipped else ""
    print(f"[line] {n} stations up{skipmsg}. attach: tmux attach -t ai-{slug}-<name>")
    print('[line] talk to the line:  af post <agent> "…"   |   read replies:  af mail   |   '
          "see it all:  af ledger")

    # Start the limit watcher WITH the line, not after it. The account-wide usage limit kills
    # every agent and the orchestrator session at the same instant — there is nobody left to
    # start a rescuer once it lands. It has to already be running, and it has to be something
    # that spends no tokens. Idempotent: re-running `line up` does not start a second one.
    from . import warden
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        warden.watch(p=p)
    for ln in out.getvalue().splitlines():
        print(f"[line] {ln}")
    return 0


def cmd_status(bp: str) -> int:
    stations = plan(bp)
    p = _p(stations[0].slug) if stations else paths()
    for st in stations:
        pr = do_probe(st.name, p)
        alive = "up" if pr.alive else "down"
        ctx = pr.ctx if (pr.alive and pr.ctx) else 0
        print(f"  {st.name:<10} {alive:<5} ctx={str(ctx):<9} unread={mailbox.unread(st.name, p)}")
    return 0


def cmd_down(bp: str) -> int:
    stations = plan(bp)
    p = _p(stations[0].slug) if stations else paths()
    for st in stations:
        with contextlib.redirect_stdout(io.StringIO()):
            lifecycle.down(st.name, p)
        print(f"[line] {st.name} down")
    return 0


def cmd_settings(slug: str, name: str, out: str) -> int:
    # Preflights first: `up` refuses to spawn into a fail-open state, and this path had no
    # reason to be the one that quietly hands out a settings file pointing at a hook that
    # cannot execute.
    if not preflight():
        return 1
    f = write_settings(slug, name, out)
    print(f"[line] wrote {f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="af.line", description="bring up a line of agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("plan", help="the resolved line, without spawning it")
    q.add_argument("blueprint")
    q = sub.add_parser("up", help="generate briefs + settings and spawn every station")
    q.add_argument("--resume", "--adopt", dest="resume", action="store_true",
                   help="bring each station back ON ITS OLD SESSION (memory kept)")
    q.add_argument("blueprint")
    q = sub.add_parser("status", help="who's alive, context size, unread mail")
    q.add_argument("blueprint")
    q = sub.add_parser("down", help="stop every station on the line")
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
        if a.cmd == "settings":
            return cmd_settings(a.slug, a.name, a.out)
    except BlueprintError as e:
        # A blueprint that does not validate must STOP the command, not decorate it. (`line
        # plan` printed its header, printed the FATAL to stderr — and still exited 0, so
        # `line plan && line up` sailed on into `up`.)
        print(f"[line] FATAL: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
