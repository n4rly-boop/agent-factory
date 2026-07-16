"""squad.json — the one mutable source of truth for a team.

Today "who is on the team / is it alive / what session is it on / what is its role" is
scattered across seven places with different truth semantics: the blueprint, per-agent
spec JSON, tmux session existence, mailbox flag files, `sid-<agent>` files, and the
append-only manifest. Liveness lives ONLY in tmux, so every status view re-scans tmux+ps
and throws the answer away; `down` never records that a station left. The blueprint is
immutable after `squad up`.

This module makes one durable file the source of truth and turns tmux/ps into the
*reconciler* that corrects it, not the store that replaces it. It lives under the spec
home — NOT /tmp — so a purge cannot erase the roster, and it is guarded by an `fcntl`
flock (the mkdir mutex was only needed while bash and Python both wrote the mailbox; the
squad file is Python-only from birth, so a normal flock is correct).

Ownership:
  * `up` / `down`          mutate the roster and status. `down` CAPTURES `live_sid` before
                           killing the session, so a killed-then-forked agent re-raises on
                           the transcript that was actually growing, not the frozen parent.
  * the postmaster daemon  reconciles `status`, `live_sid`, `ctx_tokens`, `unread`.
  * the SessionStart hook   writes `live_sid` from inside the session.

`live_sid` is the authoritative session id — spec.sid (frozen at spawn) is no longer part
of the resume fallback chain. The per-agent spec survives only as the immutable revive
constitution (role, flags, model, appended prompt).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from .paths import Paths, paths

# Status is a small closed set of plain strings — not an enum, so a squad.json written by a
# newer version with an unknown status still round-trips instead of crashing an older reader.
PLANNED = "planned"   # in the blueprint, not yet spawned
ALIVE = "alive"       # has a live tmux session
DOWN = "down"         # spawned once, session gone; live_sid is the resume record
LIMITED = "limited"   # alive but cut off by the usage limit
STATUSES = (PLANNED, ALIVE, DOWN, LIMITED)


@dataclass(frozen=True)
class Station:
    name: str
    role: str = ""
    parent: str = ""
    model: str = ""
    delegate: str = ""
    spawn_flags: str = ""
    settings_path: str = ""   # the fork-proof identity key: .../lines/<slug>/settings-<name>.json
    live_sid: str = ""        # AUTHORITATIVE current session id; captured on down
    status: str = PLANNED
    spawned: int = 0          # epoch of first spawn, 0 if never
    ctx_tokens: int = 0       # last measured context size (reconciled)
    unread: int = 0           # last measured unread mail (reconciled)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "role": self.role, "parent": self.parent,
            "model": self.model, "delegate": self.delegate, "spawn_flags": self.spawn_flags,
            "settings_path": self.settings_path, "live_sid": self.live_sid,
            "status": self.status, "spawned": self.spawned,
            "ctx_tokens": self.ctx_tokens, "unread": self.unread,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Station":
        return cls(
            name=str(d.get("name", "")),
            role=str(d.get("role") or ""),
            parent=str(d.get("parent") or ""),
            model=str(d.get("model") or ""),
            delegate=str(d.get("delegate") or ""),
            spawn_flags=str(d.get("spawn_flags") or ""),
            settings_path=str(d.get("settings_path") or ""),
            live_sid=str(d.get("live_sid") or ""),
            status=str(d.get("status") or PLANNED),
            spawned=_intish(d.get("spawned")),
            ctx_tokens=_intish(d.get("ctx_tokens")),
            unread=_intish(d.get("unread")),
        )


@dataclass
class Squad:
    slug: str
    blueprint: str = ""
    created: int = 0
    agents: dict[str, Station] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "blueprint": self.blueprint, "created": self.created,
            "agents": {n: s.to_dict() for n, s in self.agents.items()},
        }

    @classmethod
    def from_dict(cls, d: dict, slug: str) -> "Squad":
        if not isinstance(d, dict):
            return cls(slug=slug)
        agents = {}
        for n, sd in (d.get("agents") or {}).items():
            if isinstance(sd, dict):
                st = Station.from_dict({**sd, "name": sd.get("name") or n})
                agents[st.name] = st
        return cls(
            slug=str(d.get("slug") or slug),
            blueprint=str(d.get("blueprint") or ""),
            created=_intish(d.get("created")),
            agents=agents,
        )


def _intish(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --- persistence ------------------------------------------------------------------
def load(p: Paths | None = None) -> Squad:
    """The roster as last written, for READ-ONLY views. Missing/corrupt file → an empty squad
    for this slug (never an exception): a status view must still render. Do NOT use this to seed
    a mutation — `edit()` uses `_load_for_edit`, which refuses to overwrite a corrupt file."""
    p = p or paths()
    try:
        raw = p.squad_file.read_text(encoding="utf-8")
    except OSError:
        return Squad(slug=p.slug)
    try:
        return Squad.from_dict(json.loads(raw), p.slug)
    except Exception:
        return Squad(slug=p.slug)


def _load_for_edit(p: Paths) -> Squad:
    """Seed a mutation. A MISSING file is a fresh team → empty roster. A file that EXISTS but
    will not parse is dangerous: `load`'s silent-empty would let the next write persist an empty
    roster over real state, destroying every station's live_sid. So quarantine the bad file
    (rename aside, data preserved) and start fresh — never silently clobber it."""
    try:
        raw = p.squad_file.read_text(encoding="utf-8")
    except OSError:
        return Squad(slug=p.slug)
    try:
        return Squad.from_dict(json.loads(raw), p.slug)
    except Exception:
        bad = p.squad_file.with_name(f"squad.json.bad-{int(time.time())}")
        try:
            os.replace(p.squad_file, bad)
        except OSError:
            pass
        print(f"[squad] ⚠ {p.squad_file} was unparseable — quarantined to {bad.name}; "
              f"starting a fresh roster", file=sys.stderr)
        return Squad(slug=p.slug)


def _write(sq: Squad, p: Paths) -> None:
    """Atomic replace so a reader never sees a half-written roster; the caller holds the
    lock (via `edit`)."""
    p.specdir.mkdir(parents=True, exist_ok=True)
    tmp = p.squad_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sq.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p.squad_file)


@contextmanager
def edit(p: Paths | None = None) -> Iterator[Squad]:
    """Read-modify-write the roster under an exclusive flock. Mutate the yielded Squad; it is
    written atomically on a clean exit (a body that raises skips the write, so a failed mutation
    leaves the file untouched). The lock file lives beside squad.json and is only ever held by
    Python, so a plain fcntl flock is enough — no mkdir mutex.

    NOT REENTRANT: each call opens a fresh fd, and flock keys on the open file description, so a
    nested `edit()` (or any `squad.*` mutation called from inside an `edit()` body) deadlocks
    against itself. Do all mutations against the single yielded Squad; never call a mutator from
    within a body."""
    p = p or paths()
    p.specdir.mkdir(parents=True, exist_ok=True)
    lock = p.squad_lock
    with lock.open("w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            sq = _load_for_edit(p)
            yield sq
            _write(sq, p)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --- mutations (all lock internally) ----------------------------------------------
def upsert(p: Paths | None = None, *, name: str, **fields) -> Station:
    """Create or update a station. Only the fields passed are changed; the rest survive, so a
    reconcile that sets `live_sid` does not clobber `role`, and a re-spawn that sets `status`
    does not clobber a measured `ctx_tokens`."""
    p = p or paths()
    with edit(p) as sq:
        cur = sq.agents.get(name) or Station(name=name)
        sq.agents[name] = replace(cur, name=name, **fields)
        return sq.agents[name]


def mark_up(name: str, p: Paths | None = None, **fields) -> Station:
    """Record a station as freshly spawned and ALIVE. Stamps `spawned` on the first transition
    so reconcile can tell DOWN from PLANNED without depending on a caller to set it. Extra
    fields (settings_path, live_sid, role, model, …) are applied in the same write. `status`
    and `spawned` are owned here and ignored if passed."""
    p = p or paths()
    fields.pop("status", None)
    fields.pop("spawned", None)
    with edit(p) as sq:
        cur = sq.agents.get(name) or Station(name=name)
        sq.agents[name] = replace(
            cur, name=name, status=ALIVE, spawned=(cur.spawned or now()), **fields)
        return sq.agents[name]


def set_status(name: str, status: str, p: Paths | None = None) -> None:
    upsert(p, name=name, status=status)


def set_meta(blueprint: str, created: int, p: Paths | None = None) -> None:
    """The team-level facts no per-station Station row can hold — which blueprint this team
    came from, and when it was first brought up. Written once, by `squad up`, right beside the
    per-station rows it writes in the same run — so squad.json is the one file that answers
    both "who is on the team" and "where did the team come from"."""
    p = p or paths()
    with edit(p) as sq:
        sq.blueprint = blueprint
        if not sq.created:
            sq.created = created


def set_live_sid(name: str, sid: str, p: Paths | None = None) -> None:
    """Called by the SessionStart hook and by reconcile. A blank sid is ignored — losing the
    only resume record to a transient read miss is the failure this store exists to prevent."""
    if not sid:
        return
    upsert(p, name=name, live_sid=sid)


def mark_down(name: str, live_sid: str | None = None, p: Paths | None = None) -> None:
    """Record that a station's session is gone. If a live_sid is supplied (captured by `down`
    just before it killed the pane) it is written, so re-raise resumes the transcript that was
    actually growing — the frozen-parent bug's fix. A blank live_sid leaves the last known one
    intact rather than erasing the resume record."""
    p = p or paths()
    with edit(p) as sq:
        cur = sq.agents.get(name) or Station(name=name)
        sq.agents[name] = replace(
            cur, name=name, status=DOWN,
            live_sid=(live_sid or cur.live_sid),
        )


def remove(name: str, p: Paths | None = None) -> None:
    """Drop a station from the roster entirely (a blueprint edit that deletes it). Distinct
    from mark_down, which keeps the record so the agent can be revived."""
    p = p or paths()
    with edit(p) as sq:
        sq.agents.pop(name, None)


def get(name: str, p: Paths | None = None) -> Station | None:
    return load(p).agents.get(name)


def stations(p: Paths | None = None) -> list[Station]:
    return list(load(p).agents.values())


# --- reconcile --------------------------------------------------------------------
def reconcile(p: Paths | None = None) -> Squad:
    """Correct the stored roster against ground truth: tmux for liveness, `ps` argv for the
    real (post-fork) session id, the mailbox for unread. This is what the postmaster runs on
    its tick so a parked, unattended team's state stays honest without anyone driving it.

    Imports are local to keep this module importable with no tmux/mailbox dependency (the hook
    that only writes live_sid must not drag in the world).
    """
    from . import tmux, live as livemod, mailbox

    p = p or paths()
    # Compute ground truth OUTSIDE the lock: live_sid shells out to `ps -A` and walks the whole
    # projects tree, and holding the exclusive flock across that (once per station) would stall
    # every other writer — the SessionStart hook, up, down. Snapshot ps once, resolve everything,
    # then take the lock only for the fast apply.
    snap = livemod._ps()
    computed: dict[str, tuple[bool, str, int]] = {}
    for name, st in load(p).agents.items():
        try:
            alive = tmux.has_session(p.session(name))
        except Exception:
            alive = st.status in (ALIVE, LIMITED)
        try:
            new_sid = livemod.live_sid(name, p, ps_out=snap) if alive else ""
        except Exception:
            new_sid = ""
        try:
            unread = mailbox.unread(name, p)
        except Exception:
            unread = st.unread
        computed[name] = (alive, new_sid or "", unread)

    with edit(p) as sq:
        for name, st in sq.agents.items():
            if name not in computed:
                continue  # added between snapshot and apply — next tick catches it
            alive, new_sid, unread = computed[name]
            # Preserve the resume record when the process is gone; only advance live_sid to a
            # real, currently-running session.
            live_sid_val = new_sid or st.live_sid
            if alive:
                # A limit marker outranks plain "alive": the warden owns clearing it.
                status = LIMITED if p.limited(name).is_file() else ALIVE
            else:
                # Ever spawned (stamped) or holding a resume record → DOWN, not PLANNED. A
                # station that was never spawned and has no sid is still just planned.
                status = DOWN if (st.spawned or st.live_sid) else PLANNED
            sq.agents[name] = replace(
                st, status=status, live_sid=live_sid_val, unread=unread,
            )
    return load(p)


def now() -> int:
    return int(time.time())
