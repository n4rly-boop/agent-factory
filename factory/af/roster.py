"""squad.json + state.json — the durable source of truth for a team.

Today "who is on the team / is it alive / what session is it on / what is its role" is
scattered across seven places with different truth semantics: the blueprint, per-agent
spec JSON, tmux session existence, mailbox flag files, `sid-<agent>` files, and the
append-only manifest. Liveness lives ONLY in tmux, so every status view re-scans tmux+ps
and throws the answer away; `down` never records that a station left. The blueprint is
immutable after `squad up`.

This module makes two durable files the source of truth and turns tmux/ps into the
*reconciler* that corrects one of them, not the store that replaces it. Both live under
the spec home — NOT /tmp — so a purge cannot erase the roster, and both are guarded by the
SAME `fcntl` flock (one team, one lock; the mkdir mutex was only needed while bash and
Python both wrote the mailbox — this store is Python-only from birth, so a plain flock is
correct):

  * `squad.json` (`paths.squad_file`)  — STABLE: blueprint/created, and per-station
    role/parent/model/delegate/spawn_flags/settings_path/spawned. Set once at spawn,
    written only by short-lived processes (`up`/`down`/`squad`), barely ever touched again.
  * `state.json` (`paths.state_file`) — VOLATILE: per-station status/live_sid/ctx_tokens/
    unread/compacts. Touched every ~5s by the postmaster daemon's `reconcile()`, plus
    occasionally by short-lived hooks (`bump_compacts`, `set_live_sid`).

The split exists because a long-running daemon importing this module's `Station` schema
ONCE at process start silently drops any field added to it after that — the daemon's
`to_dict()` never mentions a field it doesn't know about, so a full-object round-trip
erases it on the very next write, however recently a short-lived process set it (this was
observed live: `compacts` vanished from a running squad's roster until postmaster was
restarted). `reconcile()` therefore never round-trips a `Station` at all — it patches known
keys directly onto the raw `state.json` dict via `_patch_volatile()`, so even a stale daemon
that doesn't know about some newer volatile field can never serialize it away, because it
never serializes the whole object in the first place. `squad.json`'s writers are all
short-lived, so its normal `Station.to_dict()`/`from_dict()` round-trip (via `edit()`) stays
safe exactly as it always was — the risk was specific to a DAEMON doing the round-trip, not
to the round-trip itself.

An older `squad.json` (single file, no `"schema"` key, everything embedded together) reads
back exactly as before — `load()` only reads `state.json` once it sees `"schema":
SCHEMA_VERSION` on the stable file. The first write under this version's code (an `edit()`
mutation, or `reconcile()`'s own `_patch_volatile`) migrates it: volatile keys move out of
`squad.json`'s per-station dicts into `state.json`, and the marker is stamped. `state.json`
is always written BEFORE the stamped `squad.json` — a crash between the two must never
leave `squad.json` claiming the new schema while `state.json` (the only place volatile data
then lives) doesn't exist yet.

Ownership:
  * `up` / `down`          mutate the roster and status. `down` CAPTURES `live_sid` before
                           killing the session, so a killed-then-forked agent re-raises on
                           the transcript that was actually growing, not the frozen parent.
  * the postmaster daemon  reconciles `status`, `live_sid`, `unread` via `_patch_volatile`.
  * the SessionStart hook   writes `live_sid` from inside the session, and bumps `compacts`
                            when its payload's `source` is "compact".

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

# The stable/volatile split (see module docstring). "name" rides with STABLE so a station's
# entry in squad.json is self-identifying even read alone; VOLATILE deliberately excludes it —
# state.json's dict is already keyed by name, nothing there needs to repeat it.
STABLE_KEYS = ("name", "role", "parent", "model", "delegate", "spawn_flags",
               "settings_path", "spawned")
VOLATILE_KEYS = ("live_sid", "status", "ctx_tokens", "unread", "compacts")

# Bumped only if the split itself ever needs to change shape again. Its presence (not its
# exact value) is what load()/_load_for_edit()/_patch_volatile() use to tell an old
# single-file squad.json from one that has already migrated onto state.json.
SCHEMA_VERSION = 2


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
    compacts: int = 0         # count of SessionStart(source="compact") events seen

    def to_dict(self) -> dict:
        return {
            "name": self.name, "role": self.role, "parent": self.parent,
            "model": self.model, "delegate": self.delegate, "spawn_flags": self.spawn_flags,
            "settings_path": self.settings_path, "live_sid": self.live_sid,
            "status": self.status, "spawned": self.spawned,
            "ctx_tokens": self.ctx_tokens, "unread": self.unread,
            "compacts": self.compacts,
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
            compacts=_intish(d.get("compacts")),
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
def _pick(d: dict, keys: tuple[str, ...]) -> dict:
    return {k: d[k] for k in keys if k in d}


def _atomic_write(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _parse_json_dict(raw: str) -> dict | None:
    try:
        d = json.loads(raw)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _read_plain(path: Path) -> dict:
    """For read-only views: missing OR corrupt -> {}, no side effect on disk."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_json_dict(raw) or {}


def _read_or_quarantine(path: Path) -> dict:
    """For a mutation about to happen. A MISSING file is a fresh team -> {}. A file that
    EXISTS but will not parse is dangerous: silently proceeding with {} would let the next
    write persist empty data over real state. So quarantine the bad file (rename aside, data
    preserved) and start fresh — never silently clobber it."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    d = _parse_json_dict(raw)
    if d is not None:
        return d
    bad = path.with_name(f"{path.name}.bad-{int(time.time())}")
    try:
        os.replace(path, bad)
    except OSError:
        pass
    print(f"[squad] ⚠ {path} was unparseable — quarantined to {bad.name}; "
          f"starting a fresh roster", file=sys.stderr)
    return {}


def _merge(stable: dict, volatile: dict, slug: str) -> Squad:
    """Combine the STABLE dict (squad.json) with the VOLATILE dict (state.json, keyed by
    station name) into one `Squad`. For an old, unmigrated `squad.json` (no `"schema"` key),
    `volatile` is always `{}` here (the callers below only read state.json once schema is
    current) — the stable dict's per-station entries already carry everything, unchanged
    from today, so this degrades to exactly the old single-file read."""
    agents_raw = {}
    for n, sd in (stable.get("agents") or {}).items():
        if isinstance(sd, dict):
            agents_raw[n] = {**sd, **(volatile.get(n) or {})}
    combined = {**stable, "agents": agents_raw}
    return Squad.from_dict(combined, slug)


def load(p: Paths | None = None) -> Squad:
    """The roster as last written, for READ-ONLY views. Missing/corrupt file → an empty squad
    for this slug (never an exception): a status view must still render. Do NOT use this to seed
    a mutation — `edit()` uses `_load_for_edit`, which refuses to overwrite a corrupt file."""
    p = p or paths()
    stable = _read_plain(p.squad_file)
    if not stable:
        return Squad(slug=p.slug)
    volatile = _read_plain(p.state_file) if stable.get("schema") == SCHEMA_VERSION else {}
    return _merge(stable, volatile, p.slug)


def _load_for_edit(p: Paths) -> Squad:
    """Seed a mutation. A MISSING file is a fresh team → empty roster. A file that EXISTS but
    will not parse is dangerous: `load`'s silent-empty would let the next write persist an empty
    roster over real state, destroying every station's live_sid. So quarantine the bad file
    (rename aside, data preserved) and start fresh — never silently clobber it."""
    stable = _read_or_quarantine(p.squad_file)
    if not stable:
        return Squad(slug=p.slug)
    volatile = _read_or_quarantine(p.state_file) if stable.get("schema") == SCHEMA_VERSION else {}
    return _merge(stable, volatile, p.slug)


def _write(sq: Squad, p: Paths) -> None:
    """Atomic replace so a reader never sees a half-written half of the roster; the caller
    holds the lock (via `edit`). Splits the merged in-memory `Squad` back into the two files —
    every writer that reaches here is short-lived (mark_up/mark_down/upsert/set_meta/
    bump_compacts/set_live_sid), so a `to_dict()` round-trip of the CURRENT schema is safe
    here in a way it is not for the daemon's `reconcile()` (see `_patch_volatile`).

    `state.json` is written FIRST, the (schema-stamped) `squad.json` SECOND: a crash between
    the two must never leave `squad.json` claiming the new schema while `state.json` — the
    only place volatile data then lives — does not exist yet."""
    p.specdir.mkdir(parents=True, exist_ok=True)
    full = sq.to_dict()
    agents_full = full.pop("agents")
    volatile = {n: _pick(a, VOLATILE_KEYS) for n, a in agents_full.items()}
    stable = {**full, "schema": SCHEMA_VERSION,
              "agents": {n: _pick(a, STABLE_KEYS) for n, a in agents_full.items()}}
    _atomic_write(p.state_file, volatile)
    _atomic_write(p.squad_file, stable)


def _patch_volatile(p: Paths, updates: dict[str, dict]) -> set[str]:
    """Patch ONLY the given keys onto each named station's volatile record — never a
    `Station.to_dict()` round-trip. This is what makes `reconcile()` (called every tick by
    the long-running postmaster daemon) immune to the stale-schema bug: it can never
    serialize-and-drop a field it doesn't know about, because it never serializes the whole
    object in the first place, only patches the handful of keys it was actually told to set.

    Also performs the one-time old→new format migration on first touch (pulling EVERY
    station's volatile keys out of its still-combined `squad.json` entry as the baseline,
    not only the ones `updates` mentions — a single-station caller like `set_live_sid` must
    not cause every OTHER station to lose its volatile data just because migration happened
    to be triggered by one station's update) — safe to do here specifically because
    migrating code KNOWS both formats explicitly by version, unlike the schema-drift this
    function exists to prevent.

    Returns the set of names actually found (and patched) in `squad.json`'s stable agents —
    a name not yet registered there is skipped, not created (`set_live_sid` falls back to
    `upsert()` for that rare case; `reconcile()` treats an absent name as "removed between
    the pre-lock snapshot and this lock, next tick catches it").

    Locked under the SAME `squad_lock` as `edit()`, so a `reconcile()` tick and a short-lived
    mutator (bump_compacts, set_live_sid, mark_up/down) never interleave a torn read/write.
    """
    p = p or paths()
    p.specdir.mkdir(parents=True, exist_ok=True)
    lock = p.squad_lock
    with lock.open("w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            stable = _read_or_quarantine(p.squad_file)
            if not stable:
                return set()  # no team here at all yet — nothing to patch
            migrating = stable.get("schema") != SCHEMA_VERSION
            agents = stable.get("agents") or {}
            if migrating:
                # Migration must seed EVERY station's volatile baseline here, not only the
                # ones `updates` happens to mention — `set_live_sid` (a short-lived hook, or
                # the warden's sid-heal) can call this for a SINGLE station. If migration only
                # wrote what `updates` covered, every OTHER station would have its volatile
                # keys stripped from the now-stamped squad.json (schema is per-file, not
                # per-station) while never gaining a state.json entry — silently deleted.
                volatile = {n: _pick(a, VOLATILE_KEYS) for n, a in agents.items()}
            else:
                volatile = _read_or_quarantine(p.state_file)
            patched: set[str] = set()
            for name, fields in updates.items():
                if name not in agents:
                    continue  # removed between the pre-lock snapshot and this lock
                cur = volatile.get(name)
                cur = dict(cur) if isinstance(cur, dict) else {}
                fields = dict(fields)
                # Never regress a live_sid a hook wrote between the pre-lock snapshot and
                # this lock — only overwrite when this update computed a real, non-blank one.
                if "live_sid" in fields and not fields["live_sid"]:
                    fields.pop("live_sid")
                cur.update(fields)
                volatile[name] = cur
                patched.add(name)
            if migrating:
                stable = {**stable, "schema": SCHEMA_VERSION,
                          "agents": {n: _pick(a, STABLE_KEYS) for n, a in agents.items()}}
            _atomic_write(p.state_file, volatile)
            if migrating:
                _atomic_write(p.squad_file, stable)
            return patched
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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
    """Called by the SessionStart hook (short-lived) AND by the warden daemon's sid-heal path
    (`live.heal_sid_file`, long-running) — unlike every other named mutator in this module,
    this one has a long-running caller, so it goes through `_patch_volatile`, never
    `upsert()`/`edit()`'s full `Station.to_dict()` round-trip. A warden that has been running
    since before some newer volatile field existed must not be able to erase it just by
    healing a sid — that is the exact bug class the stable/volatile split exists to kill, and
    routing through the generic dataclass round-trip here would have quietly reopened it.

    A blank sid is ignored — losing the only resume record to a transient read miss is the
    failure this store exists to prevent. A name with no station yet in `squad.json` falls
    back to `upsert()` (short-lived-safe: a first-ever creation, not the routine heal-tick
    that motivated the `_patch_volatile` route above) — preserves the original contract that
    `set_live_sid` may create a station from nothing, which some callers rely on."""
    if not sid:
        return
    p = p or paths()
    if name not in _patch_volatile(p, {name: {"live_sid": sid}}):
        upsert(p, name=name, live_sid=sid)


def bump_compacts(name: str, p: Paths | None = None) -> None:
    """Called by the SessionStart hook when its payload's `source` is "compact" — one more
    real compaction has happened to this agent, whoever drove it (`af sweep`, an operator's
    `af compact`, or the agent typing /compact itself)."""
    p = p or paths()
    with edit(p) as sq:
        cur = sq.agents.get(name) or Station(name=name)
        sq.agents[name] = replace(cur, name=name, compacts=cur.compacts + 1)


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

    Writes via `_patch_volatile`, NOT `edit()` — this is the long-running daemon's own write
    path, and a `Station.to_dict()` round-trip here is exactly the stale-schema hazard the
    stable/volatile split exists to remove (see module docstring). `live_sid` is passed
    through as computed (possibly blank when the agent is down); `_patch_volatile` itself
    is what refuses to let a blank value regress a fresher one written between this
    function's snapshot and the lock — this function no longer needs `st.live_sid` as its
    own fallback.

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
    updates: dict[str, dict] = {}
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
        if alive:
            # A limit marker outranks plain "alive": the warden owns clearing it.
            status = LIMITED if p.limited(name).is_file() else ALIVE
        else:
            # Ever spawned (stamped) or holding a resume record → DOWN, not PLANNED. A
            # station that was never spawned and has no sid is still just planned. Read from
            # the pre-lock snapshot — a coarse PLANNED/DOWN heuristic, not an overwrite, so a
            # one-tick-stale value here carries none of live_sid's regression risk.
            status = DOWN if (st.spawned or st.live_sid) else PLANNED
        updates[name] = {"status": status, "live_sid": new_sid or "", "unread": unread}

    _patch_volatile(p, updates)
    return load(p)


def now() -> int:
    return int(time.time())
