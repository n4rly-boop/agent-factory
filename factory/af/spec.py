"""The spec: an agent's durable constitution, on disk as agent-<name>.json.

Memory without a constitution is the wrong agent. The spec holds what the live world
cannot say — role, chain of command, model, the --settings file that installs the hooks,
the appended system prompt — so `revive` brings back the agent rather than a nameless
twin of it. Every field here already exists on disk and is read by bash; the shape is
fixed until bash is gone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import patterns
from .paths import Paths, paths


class SpecError(Exception):
    """A spec that cannot be used. Never swallow this: a spec that will not load means an
    agent revived with no role, no wall and no model, looking exactly like a healthy one."""


@dataclass(frozen=True)
class Spec:
    slug: str
    name: str
    cwd: str
    sid: str
    spawned: int
    flags: str
    env: dict[str, str] = field(default_factory=dict)
    ai_env: dict[str, str] = field(default_factory=dict)
    entrypoint: str = ""
    work: str = ""
    # Only ever read back from a spec whose flags are empty (a pre-flags spec). The live
    # values are DERIVED — see model/settings below.
    stored_model: str = ""
    stored_settings: str = ""

    # `model` and `settings` are regex-extracted back out of the flags string and then
    # stored redundantly in the JSON, because that is what bash does and both sides write
    # the same file. Here they are derived, so the two can never disagree; write() emits
    # them anyway. The redundancy dies with the bash.
    @property
    def model(self) -> str:
        m = patterns.FLAG_MODEL.search(self.flags or "")
        return m.group(1) if m else self.stored_model

    @property
    def settings(self) -> str:
        m = patterns.FLAG_SETTINGS.search(self.flags or "")
        return m.group(1) if m else self.stored_settings

    @property
    def role(self) -> str:
        return self.env.get("AF_ROLE", "")

    @property
    def parent(self) -> str:
        return self.env.get("AF_PARENT", "")

    @property
    def delegate(self) -> str:
        return self.env.get("AF_DELEGATE", "")

    def thresholds(self) -> tuple[int | None, int | None]:
        """The agent's OWN compaction thresholds, as recorded at spawn. A station on a
        200k-window model is configured far below this session's defaults; judging it by
        the sweeper's env means it is never compacted until it dies."""
        def num(key: str) -> int | None:
            v = self.ai_env.get(key, "")
            return int(v) if v.isdigit() else None
        return num("AI_COMPACT_SOFT"), num("AI_COMPACT_HARD")

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "cwd": self.cwd,
            "sid": self.sid,
            "spawned": self.spawned,
            "model": self.model,
            "flags": self.flags,
            "env": dict(self.env),
            "ai_env": dict(self.ai_env),
            "settings": self.settings,
            "entrypoint": self.entrypoint,
            "work": self.work,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Spec":
        if not isinstance(d, dict):
            raise SpecError("spec is not an object")
        return cls(
            slug=str(d.get("slug", "")),
            name=str(d.get("name", "")),
            cwd=str(d.get("cwd", "")),
            sid=str(d.get("sid", "")),
            spawned=int(d.get("spawned") or 0),
            flags=str(d.get("flags") or ""),
            env={str(k): str(v) for k, v in (d.get("env") or {}).items()},
            ai_env={str(k): str(v) for k, v in (d.get("ai_env") or {}).items()},
            entrypoint=str(d.get("entrypoint") or ""),
            work=str(d.get("work") or ""),
            stored_model=str(d.get("model") or ""),
            stored_settings=str(d.get("settings") or ""),
        )


def read(agent: str, p: Paths | None = None) -> Spec:
    p = p or paths()
    f = p.spec_file(agent)
    try:
        raw = f.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as e:
        raise SpecError(f"spec {f} is unreadable ({e})") from e
    try:
        return Spec.from_dict(json.loads(raw))
    except SpecError:
        raise
    except Exception as e:
        raise SpecError(f"spec {f} is unreadable ({e})") from e


def write(spec: Spec, p: Paths | None = None) -> Path:
    p = p or paths()
    f = p.spec_file(spec.name)
    f.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii: the flags may carry bytes that bash's printf %q mangled on the way in.
    # Escaped, they at least keep the file valid JSON the parser can read; written raw, one
    # bad byte makes the whole spec unloadable — and an unloadable spec is an agent that
    # revives with no role and no wall.
    with f.open("w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(json.dumps(spec.to_dict(), indent=2, ensure_ascii=True) + "\n")
    return f


def strip_sid(flags: str) -> str:
    """The session id is per-LAUNCH, the spec is per-AGENT: keeping --session-id/--resume
    in the saved flags would make every revive resume the revive-before-last."""
    b = patterns.STRIP_SID.sub(b"", flags.encode("utf-8", "surrogateescape"))
    return b.strip().decode("utf-8", "surrogateescape")


def all_specs(p: Paths | None = None) -> list[str]:
    p = p or paths()
    if not p.specdir.is_dir():
        return []
    return sorted(f.name[len("agent-"):-len(".json")] for f in p.specdir.glob("agent-*.json"))
