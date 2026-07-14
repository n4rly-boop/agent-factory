"""Spawn, kill, revive. The commands that create or destroy an agent.

An agent IS a detached tmux session running the real interactive `claude` TUI, and nothing
else. No window is ever opened; a human who wants to watch runs `tmux attach -r`. So
`tmux kill-session` ends an agent completely and there is nothing else to clean up.

Two things are written on every spawn, before the launch:

  the SPEC     — the constitution the agent runs under (role env, model, flags, the
                 --settings file that installs its hooks). It lives in $HOME, not under
                 AF_ROOT (which defaults into /tmp, wiped on reboot), because a revive that
                 finds a manifest but no spec hands back an agent stripped of its role, its
                 delegate-wall and its reminder hook — with nothing in its output saying so.
  the MANIFEST — one TSV line, read by afctl.sh and by `revivable`.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import time
import uuid

from . import drive, hooks, mailbox, manifest, patterns, spec as specmod, tmux
from .paths import LINE_SH, MAIL_SH, POLL_SH, Paths, paths

RESUME_WATCH_TICKS = 24     # 12s of watching for the resume chooser
RESUME_WATCH_SLEEP = 0.5

# Copied from the shell, byte for byte: the same agents must get the same constitution
# whichever implementation spawns them. `$AF_MAIL` is LITERAL — it is expanded later, in
# the agent's own shell, when it types the doorbell.
SYSPROMPT = """You are a spawned peer agent named '{name}', launched by an orchestrator (another Claude) via the agent-factory skill. You run unattended with permissions skipped and no human necessarily watching.

MAIL - how you talk to the rest of the factory. Send: bash $AF_MAIL send --to <agent> --kind <question|blocked|result|done|fyi> "your message". Read: bash $AF_MAIL read (mail is also pushed to you automatically - when you see a MAIL block in your context, act on it and reply to the sender by mail). Your orchestrator is reachable as --to orchestrator.

When you hit a real blocker you cannot resolve on your own - a decision only the orchestrator or a human can make, a missing secret or access you lack, an irreversible or destructive action you should not take alone, or repeated failure on the same step - do NOT stall silently: mail the orchestrator (--kind blocked), then keep doing any work that does not depend on the answer. Escalate only genuine blockers, not routine progress. Mail --kind done with a summary when you finish a long task."""

ROLE_VARS = ("AF_ROLE", "AF_PARENT", "AF_PEERS", "AF_DELEGATE", "AF_BULK_LINES",
             "AF_CAVEMAN", "AF_WORK")
AI_VARS = ("AI_COMPACT_SOFT", "AI_COMPACT_HARD", "AI_NOTIFY_OFF", "AI_SKIP_PERMS")

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _flags(e: dict | None = None) -> str:
    return (e if e is not None else os.environ).get("AI_CLAUDE_FLAGS", "")


def _rm_dead_state(agent: str, p: Paths) -> None:
    for f in p.dead_win_state(agent):
        f.unlink(missing_ok=True)


def answer_resume(agent: str, p: Paths, e: dict | None = None) -> None:
    """`claude --resume` pauses on a chooser for a large/old session:

        ❯ 1. Resume from summary   2. Resume full session as-is   3. Don't ask again

    Auto-answer it so revive lands on a READY agent, not a stuck prompt. Default 2 (full
    session — revive means "bring back the whole memory"); AI_RESUME_MODE=1 for the cheaper
    summary. A no-op, after a short watch, if no chooser appears.
    """
    mode = (e if e is not None else os.environ).get("AI_RESUME_MODE") or "2"
    s = p.session(agent)
    for _ in range(RESUME_WATCH_TICKS):
        pane = tmux.capture_pane(s) or ""
        if patterns.RESUME_CHOOSER.search(pane):
            tmux.send_keys(s, mode, literal=True)
            tmux.send_enter(s)
            print(f"[af] '{agent}' resume chooser → option {mode} (set AI_RESUME_MODE to change)")
            return
        time.sleep(RESUME_WATCH_SLEEP)


def _build_spec(name: str, sid: str, flags: str, p: Paths, e: dict) -> specmod.Spec:
    env = {k: e[k] for k in ROLE_VARS if e.get(k)}
    ai_env = {k: e[k] for k in AI_VARS if e.get(k)}
    work = env.get("AF_WORK", "")
    return specmod.Spec(
        slug=p.slug, name=name, cwd=str(p.cwd), sid=sid, spawned=int(time.time()),
        flags=flags, env=env, ai_env=ai_env, work=work,
        entrypoint=(os.path.join(work, f"entrypoint-{name}.md") if work else ""),
    )


def up(name: str = "claude", p: Paths | None = None, env: dict | None = None) -> int:
    """`env` is the environment the agent is spawned UNDER — the role vars its hooks read,
    the flags, the compaction thresholds recorded in its spec.

    It is passed in rather than taken from os.environ because `revive` restores a spec's env
    and bash could do that by mutating its own (a bash command is a fresh process, so the
    mutation dies with it). In Python the module is importable and long-lived — the warden
    already calls in-process — so two revives in one process would have let agent B inherit
    agent A's AF_ROLE/AF_DELEGATE for every key B's own spec did not set, and `up` would then
    have written that inherited role into B's NEW spec. An agent silently acquiring (or
    losing) a `required` delegate wall is precisely the class of failure this system keeps
    having, so the environment is a value that is passed, not a global that is edited.
    """
    p = p or paths()
    e = dict(os.environ) if env is None else env
    if name == "orchestrator":
        # `orchestrator` is the mailbox of the SESSION that drives the agents. An agent by
        # that name would share it, would be skipped by every sweep (so never compacted),
        # and would pass the sweep guard — it would start compacting its peers.
        print("[af] 'orchestrator' is a reserved name (it is the driving session's mailbox). "
              "Pick another.", file=sys.stderr)
        return 1

    s = p.session(name)
    tmux.kill_session(s)
    _rm_dead_state(name, p)

    flags = _flags(e)
    # The session id gives the agent a known identity: --session-id <uuid> sets its log
    # filename, and that uuid is what the manifest records. If we are resuming, reuse the
    # existing id instead of minting one that would name an empty log.
    if "--resume" in flags:
        m = patterns.SESSION_ID.search(flags)
        sid = m.group(0).lower() if m else ""
        launchflags = flags
    else:
        sid = str(uuid.uuid4()).lower()
        launchflags = f"--session-id {sid} {flags}".strip()

    # Spawned agents run unattended, so by default skip permission prompts — else they
    # stall on the first tool gate with no one to approve. AI_SKIP_PERMS=0 opts out;
    # suppressed if the caller already chose a permission flag.
    if (e.get("AI_SKIP_PERMS", "1") != "0"
            and "--dangerously-skip-permissions" not in launchflags
            and "--permission-mode" not in launchflags):
        launchflags = f"{launchflags} --dangerously-skip-permissions".strip()

    manifest.append(name, sid, str(p.cwd), p=p)

    # Persist the constitution BEFORE launching (it is built from the launch flags). A spec
    # that failed to write is not a small loss: it is a revive that comes back with no role,
    # no system prompt and no hooks, looking exactly like a healthy one. Say so now, while
    # it can still be fixed.
    basefl = specmod.strip_sid(launchflags)
    if launchflags and not basefl:
        print(f"[af] ⚠ launch flags for '{name}' could not be recorded — reviving it later "
              f"would drop its model, hooks and system prompt", file=sys.stderr)
    sp = _build_spec(name, sid, basefl, p, e)
    specfile = specmod.write(sp, p)
    if not specfile.exists() or specfile.stat().st_size == 0:
        print(f"[af] ⚠ could not write the spec for '{name}' ({specfile}) — it will revive "
              f"WITHOUT its role or hooks", file=sys.stderr)
    elif basefl and not specmod.read(name, p).flags:
        print(f"[af] ⚠ spec for '{name}' recorded no launch flags — revive would drop its "
              f"model/settings/system prompt", file=sys.stderr)

    # The agent's IDENTITY and its back-channel. AF_MAIL is what makes it REACHABLE on the
    # reliable channel: with it in the env, a sender rings the doorbell by typing the
    # path-free `!bash $AF_MAIL read` (no slash → no autocomplete popup to swallow the
    # Enter), and the cap- marker below is how senders know this agent understands it.
    # AF_POLL travels with it for one reason: the agent must be able to switch its OWN timer
    # off — it is the only party that knows the wait is over.
    envmap = {
        "AF_AGENT": name,
        "AF_SLUG": p.slug,
        "AF_MAIL": str(MAIL_SH),
        "AF_POLL": str(POLL_SH),
        "AF_MAILROOT": str(p.mailroot),
        "AF_ROOT": str(p.root),
    }
    # Role vars travel into the agent's env so its HOOKS can enforce the role with no
    # per-agent config file: the reminder hook restates the chain of command from them, the
    # delegate-wall decides from them what it may write.
    for v in ROLE_VARS:
        if e.get(v):
            envmap[v] = e[v]
    envpfx = " ".join(f"{k}={shlex.quote(v)}" for k, v in envmap.items())

    p.mailroot.mkdir(parents=True, exist_ok=True)
    p.cap(name).write_text("")

    if e.get("AI_NOTIFY_OFF") != "1":
        prompt = SYSPROMPT.format(name=name)
        full = f"{envpfx} claude {launchflags} --append-system-prompt {shlex.quote(prompt)}"
    else:
        full = f"{envpfx} claude {launchflags}"

    tmux.new_session(s, full, str(p.cwd))
    # The spec was written before the launch, so a launch that FAILED leaves a spec claiming
    # an agent that never existed — and `ledger` would list it as merely "down (revivable)".
    if not tmux.has_session(s):
        specfile.unlink(missing_ok=True)
        print(f"[af] ⚠ '{name}' failed to launch — its spec was removed", file=sys.stderr)
        return 1

    print(f"[af] interactive claude launched (session={s} id={sid} cwd={p.cwd})")
    p.state.mkdir(parents=True, exist_ok=True)
    p.sid_file(name).write_text(sid, encoding="utf-8")
    print(f"[af] detached — watch it live:  tmux attach -r -t {s}   (-r = read-only)")

    if "--resume" in launchflags:
        answer_resume(name, p, e)

    # Mail that arrived while this agent was down would otherwise sit unread forever:
    # nothing re-rings it, and the agent has no reason to go looking. Ring it now.
    pending = mailbox.unread(name, p)
    if pending > 0:
        time.sleep(drive.BOOT_SETTLE)   # or the doorbell types into a TUI that isn't up yet
        if drive.ring(name, p):
            print(f"[af] '{name}' had {pending} unread message(s) — doorbell rung.")
    print(f"[af] drive it:  af say {name} \"hello\"   |   watch screen:  af screen {name}")
    return 0


def down(name: str = "claude", p: Paths | None = None) -> int:
    p = p or paths()
    # A `busy` state that outlives its agent silently disables soft compaction for the NEXT
    # agent to take that name — it inherits the stale flag and never compacts again.
    p.task_flag(name).unlink(missing_ok=True)
    p.tasker(name).unlink(missing_ok=True)
    tmux.kill_session(p.session(name))
    _rm_dead_state(name, p)
    print(f"[af] '{name}' down — session killed.")
    return 0


def list_agents(p: Paths | None = None) -> int:
    p = p or paths()
    live = [s for s in tmux.list_sessions_verbose() if s.startswith("ai-")]
    if live:
        for s in live:
            print(s)
    else:
        print("[af] none")
    drive.inbox_hint(p)
    return 0


def attach(name: str = "claude", p: Paths | None = None) -> int:
    """The only way a human ever sees a spawned agent. Read-only is offered FIRST and on
    purpose: while this session is driving the agent with send-keys, a second writer on the
    same pane interleaves keystrokes and corrupts the input."""
    p = p or paths()
    s = p.session(name)
    print(f"tmux attach -r -t {s}   # watch (read-only — safe while the orchestrator drives)")
    print(f"tmux attach -t {s}      # take over the keyboard (don't, while it's being driven)")
    return 0


def remote(name: str = "claude", sid: str = "", p: Paths | None = None) -> int:
    """(Re)launch with Claude Code's Remote Control, so a human can drive the agent from the
    Claude web app / phone. Reuses the recorded session (memory survives) when its log still
    exists."""
    p = p or paths()
    if not sid:
        try:
            sid = p.sid_file(name).read_text(encoding="utf-8").strip()
        except OSError:
            sid = ""
    if not sid:
        sid = manifest.last_sid(name, p)

    if tmux.has_session(p.session(name)):
        down(name, p)

    e = dict(os.environ)
    flags = _flags(e)
    if sid and manifest.session_log_exists(sid, p):
        print(f"[af] launching '{name}' with Remote Control, resuming session {sid}")
        e["AI_CLAUDE_FLAGS"] = f"--resume {sid} --remote-control {name} {flags}".strip()
    else:
        print(f"[af] launching fresh '{name}' with Remote Control")
        e["AI_CLAUDE_FLAGS"] = f"--remote-control {name} {flags}".strip()
    rc = up(name, p, e)
    print(f"[af] Remote Control on — open the Claude web app / phone to drive '{name}' "
          f"(sign-in required).")
    return rc


# --- revive ----------------------------------------------------------------------
class Refusal(Exception):
    """Revive REFUSES rather than half-restoring. Every degraded path here — no spec, corrupt
    spec, spec without flags, settings whose hooks will not execute — produces an agent that
    LOOKS healthy and has no wall. A refusal is recoverable; a silently unwalled
    mini-orchestrator writing to your repo is not. AI_FORCE=1 when you genuinely want the
    memory back without the role."""


def _restore_spec_env(sp: specmod.Spec, path: str, e: dict) -> None:
    """Put the spec's env back into the environment `up` will spawn the agent under.

    KEYS ARE VALIDATED. bash eval'd these as `export K=V`, where a key like
    `AF_ROLE=w; rm -rf ~; X` lands on the left of an `=` inside an eval and no amount of
    quoting saves you. Python cannot be injected that way — but a spec carrying such a key
    is evidence of a tampered file, and the answer to a tampered constitution is to refuse
    it, not to sanitise it. (Specs live under $HOME, outside the delegate-wall's allowlist —
    and the top orchestrator is deliberately unwalled, so it could plant one for a peer and
    wait for the human to revive it.)
    """
    for group in (sp.env, sp.ai_env):
        for k, v in group.items():
            if not _KEY.match(k):
                raise Refusal(f"spec {path} has a bogus env key {k!r} — refusing")
            if v != "":
                e[k] = v


def revive(name: str = "claude", sid: str = "", p: Paths | None = None) -> int:
    p = p or paths()
    # The spec is restored into a COPY of the environment, never into os.environ: see up().
    e = dict(os.environ)
    force = e.get("AI_FORCE") == "1"
    sf = p.spec_file(name)
    opflags = _flags(e)   # anything the operator passed on THIS command line

    if not sid:
        try:
            sid = p.sid_file(name).read_text(encoding="utf-8").strip()
        except OSError:
            sid = ""
    if not sid:
        try:
            sid = specmod.read(name, p).sid
        except specmod.SpecError:
            sid = ""
    if not sid:
        # The manifest is keyed on NAME ONLY — it knows nothing about slugs. Run
        # `revive orc` from the wrong directory and it would happily resurrect the real
        # orc's memory into session ai-<wrongslug>-orc: no role, no wall, and a fresh
        # mailbox nobody reads. So it may only answer when a spec confirms the identity.
        sid = manifest.last_sid(name, p)
        if sid and not sf.is_file() and not force:
            print(f"[af] refusing: found a session for '{name}' in the manifest, but no spec "
                  f"under {p.specdir}.")
            print(f"[af]   Either you are in the wrong directory (slug='{p.slug}' — set "
                  f"AF_SLUG), or this agent")
            print(f"[af]   predates specs. Reviving it now would restore its memory with NO "
                  f"role and NO hooks.")
            print(f"[af]   Deliberate? AI_FORCE=1 af revive {name}")
            return 1
    if not sid:
        print(f"[af] no recorded session for '{name}' — see: af revivable")
        return 1
    if not manifest.session_log_exists(sid, p):
        print(f"[af] session {sid} log is gone (purged?) — can't revive '{name}'")
        return 1

    # Memory without a constitution is the wrong agent. The SPEC — not the blueprint — is
    # the source of truth: the agent's 100k of context was built under THESE rules, and
    # reviving it under rules that have since changed hands it a system prompt its own
    # history contradicts. To adopt an edited blueprint, respawn (`line up`), don't revive.
    if sf.is_file():
        try:
            sp = specmod.read(name, p)
            if not sp.flags:
                raise Refusal(f"spec {sf} records no launch flags (no model, no --settings, "
                              f"no system prompt)")
            _restore_spec_env(sp, str(sf), e)
        except (specmod.SpecError, Refusal) as err:
            print(f"[af] refusing to revive '{name}': {err}")
            print(f"[af]   A spec that won't load means no role, no hooks, no model, no "
                  f"system prompt.")
            print(f"[af]   Fix or delete {sf}, or respawn the line. Deliberate? "
                  f"AI_FORCE=1 af revive {name}")
            if not force:
                return 1
            sp = None
        if sp is not None:
            # spec first; operator flags win by coming last
            e["AI_CLAUDE_FLAGS"] = f"{sp.flags}{' ' + opflags if opflags else ''}"
            st = sp.settings
            if st:
                # The settings file lives outside the repo and can be deleted from under us.
                # A --settings pointing at nothing is not an error claude refuses — it is an
                # agent with no hooks, i.e. no delegate-wall, and nothing says so.
                if not os.path.isfile(st):
                    print(f"[af] settings file for '{name}' was gone — regenerating (its "
                          f"hooks would have been silently absent)")
                    _regen_settings(name, st, p)
                if not hooks.hooks_ok(st):
                    # Only a `required` station is REFUSED: its whole point is that the wall
                    # is load-bearing, and a fail-open hook silently removes it. An `advised`
                    # station loses a nudge, not a guarantee.
                    if e.get("AF_DELEGATE") == "required":
                        print(f"[af] refusing to revive '{name}': it is delegate:required, and "
                              f"its hooks are missing or not executable, so they would FAIL OPEN")
                        print(f"[af]   (Claude Code runs the tool anyway on a hook error — the "
                              f"wall would be a wall-shaped hole.)")
                        print(f"[af]   settings: {st}")
                        if not force:
                            return 1
                        print(f"[af] AI_FORCE=1 — reviving '{name}' UNWALLED anyway.")
                    else:
                        print(f"[af] ⚠ '{name}' hooks are not executable — its role-reminder "
                              f"and delegate advice will be missing")
            print(f"[af] restored spec: role={sp.role or 'none'} parent={sp.parent or 'none'} "
                  f"delegate={sp.delegate or 'no'} model={sp.model}")
    else:
        print(f"[af] ⚠ no spec for '{name}' — reviving with memory but NO role and NO hooks "
              f"(spawned before specs, or spec deleted)")
        if not force:
            print(f"[af]   Deliberate? AI_FORCE=1 af revive {name}")
            return 1

    print(f"[af] reviving '{name}' from session {sid}")
    e["AI_CLAUDE_FLAGS"] = f"--resume {sid} {_flags(e)}".strip()   # up() detects --resume and reuses the id
    return up(name, p, e)


def _regen_settings(name: str, out: str, p: Paths) -> None:
    """Regenerate the settings file a revive found missing. The generator is line.py's, the
    same one `line up` writes with — byte-identical output, and no bash shell-out."""
    try:
        from . import line
        line.write_settings(p.slug, name, out)
    except Exception:
        pass


def revivable(p: Paths | None = None) -> int:
    """Agents that CAN be revived by name: spawned from THIS cwd, not currently running, and
    whose session log still exists. (`list` shows only live sessions, so this is how you find
    a downed agent's name.)"""
    p = p or paths()
    if not p.manifest.is_file():
        print("[af] no manifest — nothing spawned yet")
        return 0
    out = []
    for name, sid in sorted(manifest.spawned_here(str(p.cwd), p).items()):
        if tmux.has_session(p.session(name)):
            continue
        if not manifest.session_log_exists(sid, p):
            continue
        out.append(f"  {name}\t({sid})")
    if not out:
        print("[af] no revivable agents (none downed with a surviving log)")
    else:
        print("[af] revivable (run: af revive <name>):")
        print("\n".join(out))
    return 0


# --- the orchestrator's own reachability -----------------------------------------
def register_self(p: Paths | None = None) -> int:
    """Register THIS session's tmux pane so spawned agents can WAKE it directly when they
    escalate — a true push into a live-but-idle session, no polling, no Stop-hook busy-wait.
    """
    p = p or paths()
    if not os.environ.get("TMUX"):
        print("[af] not inside tmux — can't register for send-keys wake.")
        print(f"[af]   relaunch the orchestrator in tmux:  tmux new -s {p.slug}-lead 'claude'")
        return 1
    tgt = tmux.display("#{session_name}:#{window_index}.#{pane_id}")
    if not tgt:
        print("[af] couldn't resolve this tmux pane")
        return 1
    p.mailroot.mkdir(parents=True, exist_ok=True)
    p.pane("orchestrator").write_text(tgt, encoding="utf-8")
    if os.environ.get("AF_MAIL"):
        # An orchestrator started WITH AF_MAIL in its env gets the clean path-free doorbell
        # like any spawned agent. Started without it (a plain `claude` in tmux), senders fall
        # back to typing a literal path — which still works, but needs a popup-dismiss.
        p.cap("orchestrator").write_text("")
        print(f"[af] registered orchestrator for slug '{p.slug}' → pane {tgt} (mail-capable)")
    else:
        p.cap("orchestrator").unlink(missing_ok=True)
        print(f"[af] registered orchestrator for slug '{p.slug}' → pane {tgt}")
        print("[af]   note: AF_MAIL not in this session's env — doorbell falls back to typing "
              "a literal path.")
        print("[af]   for the clean channel, launch the orchestrator as:")
        print(f"[af]     tmux new -s {p.slug}-lead 'AF_MAIL={MAIL_SH} "
              f"AF_MAILROOT={p.mailroot} AF_SLUG={p.slug} claude'")
    print("[af] agents can now WAKE this session with mail (no polling needed).")
    return 0


def unregister_self(p: Paths | None = None) -> int:
    p = p or paths()
    p.pane("orchestrator").unlink(missing_ok=True)
    p.cap("orchestrator").unlink(missing_ok=True)
    print(f"[af] unregistered orchestrator for slug '{p.slug}'")
    return 0
