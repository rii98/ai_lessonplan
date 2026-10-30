"""ProfileStore — where a :class:`TeacherProfile` is persisted, one per principal.

A storage port behind the registry pattern: ``profile.provider`` picks ``memory``
(tests) or ``postgres`` (``teacher_profiles``, on the shared pool). A miss returns
a fresh empty profile rather than raising — a teacher who has never saved
preferences still gets a usable, do-nothing profile. The owner always comes from
the authenticated principal, never from the profile body.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from ..config import ProfileConfig
from ..db.pool import Database, as_uuid, jsonb
from ..domain.profile import TeacherProfile
from .registry import register_profile_store


class ProfileStore(ABC):
    @abstractmethod
    def load(self, owner_id: str) -> TeacherProfile:
        """The stored profile, or a fresh empty one if none is saved."""

    @abstractmethod
    def save(self, owner_id: str, profile: TeacherProfile) -> TeacherProfile:
        """Persist ``profile`` for ``owner_id`` (which overrides the body's owner)."""

    @classmethod
    def from_config(cls, cfg: ProfileConfig, *, db: Database | None = None) -> ProfileStore:  # pragma: no cover
        raise NotImplementedError


@register_profile_store("memory")
class MemoryProfileStore(ProfileStore):
    def __init__(self) -> None:
        self._by_owner: dict[str, TeacherProfile] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: ProfileConfig, *, db: Database | None = None) -> MemoryProfileStore:
        return cls()

    def load(self, owner_id: str) -> TeacherProfile:
        return self._by_owner.get(owner_id) or TeacherProfile(owner_id=owner_id)

    def save(self, owner_id: str, profile: TeacherProfile) -> TeacherProfile:
        stored = profile.model_copy(update={"owner_id": owner_id})
        with self._lock:
            self._by_owner[owner_id] = stored
        return stored


@register_profile_store("postgres")
class PostgresProfileStore(ProfileStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    @classmethod
    def from_config(cls, cfg: ProfileConfig, *, db: Database | None = None) -> PostgresProfileStore:
        if db is None:
            raise ValueError("the postgres profile store needs database.url (DATABASE_URL)")
        return cls(db)

    def load(self, owner_id: str) -> TeacherProfile:
        pid = as_uuid(owner_id)
        if pid is not None:
            with self.db.tx() as cur:
                cur.execute("SELECT data FROM teacher_profiles WHERE principal_id = %s", (pid,))
                row = cur.fetchone()
            if row is not None:
                return TeacherProfile.model_validate({**row["data"], "owner_id": owner_id})
        return TeacherProfile(owner_id=owner_id)

    def save(self, owner_id: str, profile: TeacherProfile) -> TeacherProfile:
        stored = profile.model_copy(update={"owner_id": owner_id})
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO teacher_profiles (principal_id, data) VALUES (%s, %s) "
                "ON CONFLICT (principal_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (as_uuid(owner_id), jsonb(stored.model_dump(mode="json", exclude={"owner_id"}))))
        return stored
