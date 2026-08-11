"""ProfileStore — where a :class:`TeacherProfile` is persisted.

A storage port behind the same registry pattern as everything else: swap
``profile.provider`` in config and the store changes with no code edit.

- ``memory`` — in-process dict. The default: zero infra, perfect for tests and a
  single-process dev run. Profiles reset on restart.
- ``file`` — one JSON file per owner under a directory, so a teacher's
  preferences survive restarts without standing up a database. The real
  multi-tenant DB store lands in M5; this port is the seam that keeps that a
  config swap.

Both look profiles up by ``owner_id`` (``"default"`` in the single-teacher v1),
and a miss returns a fresh empty profile rather than raising — a teacher who has
never saved preferences still gets a usable, do-nothing profile.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import ProfileConfig
from ..domain.profile import TeacherProfile
from .registry import register_profile_store

# owner_id becomes a filename in the file store — keep it to a safe charset so it
# can never escape the profiles directory (path traversal) or collide with the OS.
_SAFE_OWNER = re.compile(r"[^A-Za-z0-9._-]")


def _safe_owner(owner_id: str) -> str:
    cleaned = _SAFE_OWNER.sub("_", owner_id).strip("._") or "default"
    return cleaned


class ProfileStore(ABC):
    """Loads and saves teacher preferences, keyed by owner."""

    @abstractmethod
    def load(self, owner_id: str = "default") -> TeacherProfile:
        """Return the stored profile, or a fresh empty one if none is saved."""

    @abstractmethod
    def save(self, profile: TeacherProfile) -> TeacherProfile:
        """Persist ``profile`` and return it."""

    @classmethod
    def from_config(cls, cfg: ProfileConfig) -> ProfileStore:  # pragma: no cover
        raise NotImplementedError


@register_profile_store("memory")
class MemoryProfileStore(ProfileStore):
    """In-process store. Fast, dependency-free, non-persistent."""

    def __init__(self) -> None:
        self._by_owner: dict[str, TeacherProfile] = {}

    @classmethod
    def from_config(cls, cfg: ProfileConfig) -> MemoryProfileStore:
        return cls()

    def load(self, owner_id: str = "default") -> TeacherProfile:
        return self._by_owner.get(owner_id) or TeacherProfile(owner_id=owner_id)

    def save(self, profile: TeacherProfile) -> TeacherProfile:
        self._by_owner[profile.owner_id] = profile
        return profile


@register_profile_store("file")
class FileProfileStore(ProfileStore):
    """One ``<owner_id>.json`` file per teacher under ``path``."""

    def __init__(self, path: str | Path) -> None:
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: ProfileConfig) -> FileProfileStore:
        return cls(cfg.path or ".lessonforge/profiles")

    def _file(self, owner_id: str) -> Path:
        return self.dir / f"{_safe_owner(owner_id)}.json"

    def load(self, owner_id: str = "default") -> TeacherProfile:
        f = self._file(owner_id)
        if f.exists():
            return TeacherProfile.model_validate_json(f.read_text(encoding="utf-8"))
        return TeacherProfile(owner_id=owner_id)

    def save(self, profile: TeacherProfile) -> TeacherProfile:
        self._file(profile.owner_id).write_text(
            profile.model_dump_json(indent=2), encoding="utf-8"
        )
        return profile
