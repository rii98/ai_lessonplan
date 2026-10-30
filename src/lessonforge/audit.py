"""Audit log — security-relevant privileged actions only (grants, ingestion).

Kept apart from activity events (product analytics Edboox reads) and from
operational logs (debugging): mixing them either leaks detail to a partner or
buries audit rows among millions of chat events (§15).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .db.pool import Database, as_uuid, jsonb


@dataclass(frozen=True, slots=True)
class AuditEntry:
    actor: str                          # "edboox:teacher:1" or "cli:<os user>"
    action: str                         # 'corpus.ingest', 'grant.add', …
    target: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    principal_id: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditLog(ABC):
    @abstractmethod
    def record(self, entry: AuditEntry) -> None: ...

    @abstractmethod
    def recent(self, limit: int = 50) -> list[AuditEntry]: ...


class MemoryAuditLog(AuditLog):
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> None:
        with self._lock:
            self.entries.append(entry)

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        return list(reversed(self.entries[-limit:]))


class PostgresAuditLog(AuditLog):
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, entry: AuditEntry) -> None:
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO audit_log (principal_id, actor, action, target, detail, at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (as_uuid(entry.principal_id), entry.actor, entry.action, entry.target,
                 jsonb(entry.detail), entry.at),
            )

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        with self.db.tx() as cur:
            cur.execute("SELECT principal_id, actor, action, target, detail, at FROM audit_log "
                        "ORDER BY id DESC LIMIT %s", (limit,))
            return [AuditEntry(actor=r["actor"], action=r["action"], target=r["target"],
                               detail=r["detail"], at=r["at"],
                               principal_id=str(r["principal_id"]) if r["principal_id"] else None)
                    for r in cur.fetchall()]
