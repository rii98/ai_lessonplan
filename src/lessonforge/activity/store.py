"""Activity persistence: a write port (:class:`ActivityRecorder`) and a read port
(:class:`ActivityReader`: the cursor feed, summary reports, retention).

Writing never breaks a request. :class:`BufferedRecorder` puts events on a bounded
in-process queue and a daemon thread writes them in batches (one multi-row insert
per flush); a full queue drops the event and logs — losing one analytics row is
acceptable, failing a student's answer is not.

Reports are plain ``GROUP BY`` queries over the indexed table. The memory reader
implements the same semantics in Python; one contract test suite runs against
both, so they cannot drift.
"""

from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..db.pool import Database, as_uuid, jsonb
from .events import ActivityEvent

log = logging.getLogger(__name__)

GroupBy = Literal["day", "school", "role"]
_GROUP_SQL: dict[str, str] = {
    "day": "date_trunc('day', occurred_at)::date",
    "school": "tenant_id",
    "role": "role",
}
_GROUP_OUT = {"day": "day", "school": "school", "role": "role"}


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: int
    event: ActivityEvent

    def view(self) -> dict[str, Any]:
        e = self.event
        return {"id": str(self.id), "occurred_at": e.occurred_at, "type": e.type,
                "schema_ver": e.schema_ver, "subject": e.subject, "role": e.role,
                "tenant_id": e.tenant_id, "grade": e.grade, "resource_type": e.resource_type,
                "resource_id": e.resource_id, "props": e.props}


@dataclass(frozen=True, slots=True)
class ReportFilter:
    start: datetime
    end: datetime
    tenant_id: str | None = None          # None = every school (service/superadmin only)
    grade: int | None = None
    subject: str | None = None            # canonical subject (props.subject)


@dataclass(slots=True)
class FeedPage:
    items: list[StoredEvent] = field(default_factory=list)
    next_cursor: str | None = None


# ── ports ────────────────────────────────────────────────────────────────────
class ActivityRecorder(ABC):
    @abstractmethod
    def record_many(self, events: list[ActivityEvent]) -> None: ...

    def record(self, event: ActivityEvent | None) -> None:
        """Best effort: never raises into the caller."""
        if event is None:
            return
        try:
            self.record_many([event])
        except Exception:
            log.exception("activity event dropped: %s", event.type)

    def close(self) -> None:  # flush buffered writers
        return None


class ActivityReader(ABC):
    @abstractmethod
    def feed(self, *, after: int, limit: int, types: list[str] | None,
             tenant_id: str | None) -> FeedPage: ...

    @abstractmethod
    def usage(self, f: ReportFilter, group_by: list[GroupBy]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def chat_topics(self, f: ReportFilter, *, top: int = 20) -> dict[str, Any]: ...

    @abstractmethod
    def quizzes(self, f: ReportFilter, *, top: int = 10) -> dict[str, Any]: ...

    @abstractmethod
    def user_summary(self, subject: str, f: ReportFilter) -> dict[str, Any]: ...

    @abstractmethod
    def purge_older_than(self, days: int) -> int: ...


class NoopRecorder(ActivityRecorder):
    def record_many(self, events: list[ActivityEvent]) -> None:
        return None


class BufferedRecorder(ActivityRecorder):
    """Decorator: asynchronous, batched writes to an inner recorder."""

    def __init__(self, inner: ActivityRecorder, *, max_queue: int = 10_000, batch: int = 200,
                 flush_interval_s: float = 1.0) -> None:
        self.inner = inner
        self.batch = batch
        self.flush_interval_s = flush_interval_s
        self._q: queue.Queue[ActivityEvent | None] = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, name="activity-writer", daemon=True)
        self._thread.start()

    def record_many(self, events: list[ActivityEvent]) -> None:
        for e in events:
            try:
                self._q.put_nowait(e)
            except queue.Full:
                log.warning("activity queue full; dropping %s", e.type)

    def _run(self) -> None:
        stop = False
        while not stop:
            buf: list[ActivityEvent] = []
            try:
                first = self._q.get(timeout=self.flush_interval_s)
            except queue.Empty:
                continue
            if first is None:
                stop = True
            else:
                buf.append(first)
            while len(buf) < self.batch:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                buf.append(nxt)
            if buf:
                try:
                    self.inner.record_many(buf)
                except Exception:
                    log.exception("activity batch of %d dropped", len(buf))

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=5)


# ── memory ───────────────────────────────────────────────────────────────────
class MemoryActivityStore(ActivityRecorder, ActivityReader):
    def __init__(self) -> None:
        self.rows: list[StoredEvent] = []
        self._lock = threading.Lock()

    def record_many(self, events: list[ActivityEvent]) -> None:
        with self._lock:
            for e in events:
                self.rows.append(StoredEvent(len(self.rows) + 1, e))

    def _in(self, f: ReportFilter) -> list[ActivityEvent]:
        out = []
        for r in self.rows:
            e = r.event
            if not (f.start <= e.occurred_at < f.end):
                continue
            if f.tenant_id is not None and e.tenant_id != f.tenant_id:
                continue
            if f.grade is not None and e.grade != f.grade:
                continue
            if f.subject is not None and e.props.get("subject") != f.subject:
                continue
            out.append(e)
        return out

    def feed(self, *, after, limit, types, tenant_id) -> FeedPage:
        items = [r for r in self.rows if r.id > after
                 and (not types or r.event.type in types)
                 and (tenant_id is None or r.event.tenant_id == tenant_id)][:limit]
        return FeedPage(items, str(items[-1].id) if len(items) == limit else None)

    def usage(self, f, group_by):
        if not group_by:   # one totals row, like SQL's ungrouped aggregate
            return [_usage_counts(self._in(f))]
        groups: dict[tuple, list[ActivityEvent]] = defaultdict(list)
        for e in self._in(f):
            key = tuple(_group_value(e, g) for g in group_by)
            groups[key].append(e)
        rows = []
        for key in sorted(groups, key=lambda k: tuple("" if v is None else str(v) for v in k)):
            evs = groups[key]
            row = {_GROUP_OUT[g]: v for g, v in zip(group_by, key, strict=True)}
            row.update(_usage_counts(evs))
            rows.append(row)
        return rows

    def chat_topics(self, f, *, top=20):
        msgs = [e for e in self._in(f) if e.type == "chat.message"]
        subjects = Counter(e.props.get("subject") for e in msgs if e.props.get("subject"))
        chapters = Counter((c, e.props.get("subject")) for e in msgs
                           for c in e.props.get("chapters", []))
        grounded = sum(1 for e in msgs if e.props.get("grounded"))
        return {
            "turns": len(msgs),
            "grounded_share": round(grounded / len(msgs), 4) if msgs else None,
            "subjects": [{"subject": s, "turns": n}
                         for s, n in sorted(subjects.items(), key=lambda x: (-x[1], x[0]))[:top]],
            "chapters": [{"chapter": c, "subject": s, "turns": n}
                         for (c, s), n in sorted(chapters.items(),
                                                 key=lambda x: (-x[1], x[0][0]))[:top]],
        }

    def quizzes(self, f, *, top=10):
        evs = self._in(f)
        created = [e for e in evs if e.type == "quiz.created"]
        started = [e for e in evs if e.type == "quiz.attempt.started"]
        finished = [e for e in evs if e.type == "quiz.attempt.finished"]
        chapter_of = {e.resource_id: e.props.get("chapter") for e in self.rows_of("quiz.created")}
        by_chapter: dict[str, list[float]] = defaultdict(list)
        for e in finished:
            ch = chapter_of.get(e.resource_id)
            if ch:
                by_chapter[ch].append(float(e.props["percent"]))
        weakest = sorted(((c, sum(v) / len(v), len(v)) for c, v in by_chapter.items()),
                         key=lambda x: (x[1], x[0]))[:top]
        return {
            "created": len(created), "attempts_started": len(started),
            "attempts_finished": len(finished),
            "completion_rate": round(len(finished) / len(started), 4) if started else None,
            "avg_percent": (round(sum(e.props["percent"] for e in finished) / len(finished), 2)
                            if finished else None),
            "weakest_chapters": [{"chapter": c, "avg_percent": round(a, 2), "attempts": n}
                                 for c, a, n in weakest],
        }

    def rows_of(self, type_: str) -> Iterable[ActivityEvent]:
        return (r.event for r in self.rows if r.event.type == type_)

    def user_summary(self, subject, f):
        evs = [e for e in self._in(f) if e.subject == subject]
        finished = [e for e in evs if e.type == "quiz.attempt.finished"]
        return {
            "subject": subject,
            "events": dict(sorted(Counter(e.type for e in evs).items())),
            "last_active": max((e.occurred_at for e in evs), default=None),
            "avg_quiz_percent": (round(sum(e.props["percent"] for e in finished) / len(finished), 2)
                                 if finished else None),
            "tokens_in": sum(int(e.props.get("tokens_in", 0)) for e in evs),
            "tokens_out": sum(int(e.props.get("tokens_out", 0)) for e in evs),
        }

    def purge_older_than(self, days):
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with self._lock:
            before = len(self.rows)
            self.rows = [r for r in self.rows if r.event.occurred_at >= cutoff]
            return before - len(self.rows)


def _group_value(e: ActivityEvent, g: str) -> Any:
    if g == "day":
        return e.occurred_at.astimezone(UTC).date()
    return e.tenant_id if g == "school" else e.role


def _usage_counts(evs: list[ActivityEvent]) -> dict[str, Any]:
    t = Counter(e.type for e in evs)
    return {
        "active_users": len({e.subject for e in evs if e.subject}),
        "chat_messages": t["chat.message"], "conversations": t["chat.conversation.created"],
        "quizzes_created": t["quiz.created"], "quiz_attempts": t["quiz.attempt.finished"],
        "lessons": t["lesson.generated"], "units": t["unit.generated"],
        "tokens_in": sum(int(e.props.get("tokens_in", 0)) for e in evs),
        "tokens_out": sum(int(e.props.get("tokens_out", 0)) for e in evs),
    }


# ── postgres ─────────────────────────────────────────────────────────────────
_USAGE_AGGS = """
  count(DISTINCT subject)                                         AS active_users,
  count(*) FILTER (WHERE type = 'chat.message')                   AS chat_messages,
  count(*) FILTER (WHERE type = 'chat.conversation.created')      AS conversations,
  count(*) FILTER (WHERE type = 'quiz.created')                   AS quizzes_created,
  count(*) FILTER (WHERE type = 'quiz.attempt.finished')          AS quiz_attempts,
  count(*) FILTER (WHERE type = 'lesson.generated')               AS lessons,
  count(*) FILTER (WHERE type = 'unit.generated')                 AS units,
  COALESCE(sum((props->>'tokens_in')::bigint), 0)::bigint         AS tokens_in,
  COALESCE(sum((props->>'tokens_out')::bigint), 0)::bigint        AS tokens_out"""


class PostgresActivityStore(ActivityRecorder, ActivityReader):
    def __init__(self, db: Database) -> None:
        self.db = db

    def record_many(self, events: list[ActivityEvent]) -> None:
        if not events:
            return
        with self.db.tx() as cur:
            cur.executemany(
                "INSERT INTO activity_events (occurred_at, type, schema_ver, principal_id, "
                "subject, role, tenant_id, grade, resource_type, resource_id, props, request_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(e.occurred_at, e.type, e.schema_ver, as_uuid(e.principal_id), e.subject,
                  e.role, e.tenant_id, e.grade, e.resource_type, as_uuid(e.resource_id),
                  jsonb(e.props), e.request_id) for e in events],
            )

    @staticmethod
    def _where(f: ReportFilter, extra: str = "", *, alias: str = "") -> tuple[str, list[Any]]:
        a = f"{alias}." if alias else ""
        sql, params = [f"{a}occurred_at >= %s", f"{a}occurred_at < %s"], [f.start, f.end]
        if f.tenant_id is not None:
            sql.append(f"{a}tenant_id = %s")
            params.append(f.tenant_id)
        if f.grade is not None:
            sql.append(f"{a}grade = %s")
            params.append(f.grade)
        if f.subject is not None:
            sql.append(f"{a}props->>'subject' = %s")
            params.append(f.subject)
        if extra:
            sql.append(extra)
        return " AND ".join(sql), params

    def feed(self, *, after, limit, types, tenant_id) -> FeedPage:
        sql = ["id > %s"]
        params: list[Any] = [after]
        if types:
            sql.append("type = ANY(%s)")
            params.append(list(types))
        if tenant_id is not None:
            sql.append("tenant_id = %s")
            params.append(tenant_id)
        params.append(limit)
        with self.db.tx() as cur:
            cur.execute(
                "SELECT id, occurred_at, type, schema_ver, principal_id, subject, role, tenant_id, "
                "grade, resource_type, resource_id, props, request_id FROM activity_events "
                f"WHERE {' AND '.join(sql)} ORDER BY id LIMIT %s", params)
            rows = cur.fetchall()
        items = [StoredEvent(r["id"], ActivityEvent(
            type=r["type"], props=r["props"], role=r["role"], schema_ver=r["schema_ver"],
            principal_id=str(r["principal_id"]) if r["principal_id"] else None,
            subject=r["subject"], tenant_id=r["tenant_id"], grade=r["grade"],
            resource_type=r["resource_type"],
            resource_id=str(r["resource_id"]) if r["resource_id"] else None,
            request_id=r["request_id"], occurred_at=r["occurred_at"])) for r in rows]
        return FeedPage(items, str(items[-1].id) if len(items) == limit else None)

    def usage(self, f, group_by):
        cols = [f"{_GROUP_SQL[g]} AS {_GROUP_OUT[g]}" for g in group_by]
        where, params = self._where(f)
        group = f" GROUP BY {', '.join(_GROUP_OUT[g] for g in group_by)}" if group_by else ""
        order = f" ORDER BY {', '.join(_GROUP_OUT[g] for g in group_by)}" if group_by else ""
        select = ", ".join([*cols, _USAGE_AGGS])
        with self.db.tx() as cur:
            cur.execute(f"SELECT {select} FROM activity_events WHERE {where}{group}{order}", params)
            return [dict(r) for r in cur.fetchall()]

    def chat_topics(self, f, *, top=20):
        where, params = self._where(f, "type = 'chat.message'")
        with self.db.tx() as cur:
            cur.execute(f"SELECT count(*) AS turns, count(*) FILTER "
                        f"(WHERE (props->>'grounded')::boolean) AS grounded "
                        f"FROM activity_events WHERE {where}", params)
            head = cur.fetchone()
            cur.execute(f"SELECT props->>'subject' AS subject, count(*) AS turns "
                        f"FROM activity_events WHERE {where} AND props ? 'subject' "
                        f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT %s", [*params, top])
            subjects = [dict(r) for r in cur.fetchall()]
            cur.execute(f"SELECT ch AS chapter, props->>'subject' AS subject, count(*) AS turns "
                        f"FROM activity_events, jsonb_array_elements_text(props->'chapters') ch "
                        f"WHERE {where} GROUP BY 1, 2 ORDER BY 3 DESC, 1 LIMIT %s", [*params, top])
            chapters = [dict(r) for r in cur.fetchall()]
        turns = head["turns"]
        return {"turns": turns,
                "grounded_share": round(head["grounded"] / turns, 4) if turns else None,
                "subjects": subjects, "chapters": chapters}

    def quizzes(self, f, *, top=10):
        where, params = self._where(f)
        with self.db.tx() as cur:
            cur.execute(
                f"""SELECT count(*) FILTER (WHERE type = 'quiz.created')          AS created,
                           count(*) FILTER (WHERE type = 'quiz.attempt.started')  AS started,
                           count(*) FILTER (WHERE type = 'quiz.attempt.finished') AS finished,
                           avg((props->>'percent')::float)
                             FILTER (WHERE type = 'quiz.attempt.finished')        AS avg_percent
                    FROM activity_events WHERE {where}""", params)
            head = cur.fetchone()
            fwhere, fparams = self._where(f, "fin.type = 'quiz.attempt.finished'", alias="fin")
            cur.execute(
                f"""SELECT q.props->>'chapter' AS chapter,
                           avg((fin.props->>'percent')::float) AS avg_percent,
                           count(*) AS attempts
                      FROM activity_events fin
                      JOIN activity_events q
                        ON q.type = 'quiz.created' AND q.resource_id = fin.resource_id
                     WHERE {fwhere} AND q.props ? 'chapter'
                     GROUP BY 1 ORDER BY 2, 1 LIMIT %s""", [*fparams, top])
            weakest = cur.fetchall()
        started, finished = head["started"], head["finished"]
        return {
            "created": head["created"], "attempts_started": started,
            "attempts_finished": finished,
            "completion_rate": round(finished / started, 4) if started else None,
            "avg_percent": round(head["avg_percent"], 2) if head["avg_percent"] is not None else None,
            "weakest_chapters": [{"chapter": r["chapter"], "avg_percent": round(r["avg_percent"], 2),
                                  "attempts": r["attempts"]} for r in weakest],
        }

    def user_summary(self, subject, f):
        where, params = self._where(f, "subject = %s")
        params.append(subject)
        with self.db.tx() as cur:
            cur.execute(f"SELECT type, count(*) AS n, max(occurred_at) AS last, "
                        f"avg((props->>'percent')::float) AS pct, "
                        f"COALESCE(sum((props->>'tokens_in')::bigint), 0)::bigint AS tin, "
                        f"COALESCE(sum((props->>'tokens_out')::bigint), 0)::bigint AS tout "
                        f"FROM activity_events WHERE {where} GROUP BY type ORDER BY type", params)
            rows = cur.fetchall()
        fin = next((r for r in rows if r["type"] == "quiz.attempt.finished"), None)
        return {
            "subject": subject,
            "events": {r["type"]: r["n"] for r in rows},
            "last_active": max((r["last"] for r in rows), default=None),
            "avg_quiz_percent": round(fin["pct"], 2) if fin and fin["pct"] is not None else None,
            "tokens_in": sum(r["tin"] for r in rows),
            "tokens_out": sum(r["tout"] for r in rows),
        }

    def purge_older_than(self, days):
        with self.db.tx() as cur:
            cur.execute("DELETE FROM activity_events WHERE occurred_at < now() - make_interval(days => %s)",
                        (days,))
            return cur.rowcount


def with_time(event: ActivityEvent, at: datetime) -> ActivityEvent:
    """Test/backfill helper: the same event at another time."""
    return replace(event, occurred_at=at)
