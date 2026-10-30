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
    id: int | None = None                # assigned by the store


class AuditLog(ABC):
    @abstractmethod
    def record(self, entry: AuditEntry) -> None: ...

    @abstractmethod
    def recent(self, limit: int = 50, *, before: int | None = None,
               action: str | None = None) -> list[AuditEntry]:
        """Newest first; ``before`` (an entry id) pages backwards; ``action`` filters
        by prefix (``grant.``, ``corpus.``, ``principal.``…)."""


class MemoryAuditLog(AuditLog):
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> None:
        from dataclasses import replace

        with self._lock:
            self.entries.append(replace(entry, id=len(self.entries) + 1))

    def recent(self, limit=50, *, before=None, action=None):
        rows = [e for e in reversed(self.entries)
                if (before is None or (e.id or 0) < before)
                and (action is None or e.action.startswith(action))]
        return rows[:limit]


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

    def recent(self, limit=50, *, before=None, action=None):
        sql, params = [], []
        if before is not None:
            sql.append("id < %s")
            params.append(before)
        if action:
            sql.append("action LIKE %s")
            params.append(action.replace("%", r"\%").replace("_", r"\_") + "%")
        where = f"WHERE {' AND '.join(sql)}" if sql else ""
        with self.db.tx() as cur:
            cur.execute("SELECT id, principal_id, actor, action, target, detail, at FROM audit_log "
                        f"{where} ORDER BY id DESC LIMIT %s", [*params, limit])
            return [AuditEntry(id=r["id"], actor=r["actor"], action=r["action"],
                               target=r["target"], detail=r["detail"], at=r["at"],
                               principal_id=str(r["principal_id"]) if r["principal_id"] else None)
                    for r in cur.fetchall()]
