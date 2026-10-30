"""Cursor pagination shared by every list endpoint (API contract rule 6).

Lists are ordered by ``(timestamp DESC, id DESC)`` and paged by *keyset*: the
cursor encodes the last row's ``(timestamp, id)`` and the next page is
``WHERE (ts, id) < (cursor.ts, cursor.id)``. Unlike ``OFFSET``, this is O(page)
on the index whatever the depth, and never skips or repeats a row when new rows
arrive between pages. The cursor is opaque to clients (base64url JSON).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Keyset:
    ts: datetime
    id: str

    def encode(self) -> str:
        raw = json.dumps([self.ts.isoformat(), self.id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str | None) -> Keyset | None:
        if not cursor:
            return None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            ts, id_ = json.loads(base64.urlsafe_b64decode(padded.encode()))
            return cls(datetime.fromisoformat(ts), str(id_))
        except (ValueError, TypeError, binascii.Error) as exc:
            raise InvalidCursor("malformed cursor") from exc

    def before(self, ts: datetime, id_: str) -> bool:
        """Whether a row sorts strictly after this cursor in (ts, id) DESC order."""
        return (ts, id_) < (self.ts, self.id)


@dataclass(slots=True)
class Page(Generic[T]):
    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None


class PageOut(BaseModel, Generic[T]):
    """The wire shape of every list endpoint: ``{items, next_cursor}``."""

    items: list[T]
    next_cursor: str | None = None


def page_of(rows: list[T], limit: int, key: callable) -> Page[T]:  # type: ignore[valid-type]
    """Trim an over-fetched ``limit + 1`` result into a page with its cursor."""
    has_more = len(rows) > limit
    rows = rows[:limit]
    return Page(rows, key(rows[-1]).encode() if has_more and rows else None)
