"""The per-request context: who is calling, which knowledge they may read, and
what the request has cost so far.

Bound ONCE at the edge (the API's auth dependency, or the admin CLI) and read
only by infrastructure that sits below the services — the retriever (to pass the
:class:`AccessScope` to the vector store), the metered LLM (to sum token usage),
and the activity recorder (to fill the actor). Services stay authorization-free.

Why a ``ContextVar`` rather than a parameter threaded through every service: the
retrieval graph (retriever → grounding → generator / refiner / unit planner /
artifact generators / chat skills) is composed once at boot and shared; threading
a scope argument through ~a dozen service signatures would couple every service to
authorization. A ContextVar is *task*-local (each request runs in its own task,
and Starlette copies the context into the threadpool that runs sync endpoints and
streaming generators), so nothing leaks between concurrent requests. The design
stays fail-safe: with no context bound, reads get :meth:`AccessScope.public` —
shared curriculum only, never a school's private shelf.

Work handed to an executor must carry the context explicitly
(:func:`submit_in_context`); a bare ``pool.submit`` would run with the
least-privileged default.
"""

from __future__ import annotations

import contextvars
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from .knowledge.scope import AccessScope

if TYPE_CHECKING:
    from .auth.principal import Principal
    from .context.model import UserContext

T = TypeVar("T")


@dataclass(slots=True)
class TokenUsage:
    """LLM usage summed across every call a request makes (thread-safe: the chat
    router classifies on a worker thread while the answer streams)."""

    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    models: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, *, tokens_in: int, tokens_out: int, model: str | None) -> None:
        with self._lock:
            self.tokens_in += max(0, int(tokens_in or 0))
            self.tokens_out += max(0, int(tokens_out or 0))
            self.calls += 1
            if model:
                self.models.add(model)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                    "llm_calls": self.calls, "model": ",".join(sorted(self.models)) or None}


@dataclass(slots=True)
class RequestContext:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    principal: Principal | None = None
    access_scope: AccessScope = field(default_factory=AccessScope.public)
    user_context: UserContext | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


_current: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "lessonforge_request_context", default=None
)


def current() -> RequestContext | None:
    return _current.get()


def current_scope() -> AccessScope:
    """The access scope for vector-store reads (least privilege when unbound)."""
    ctx = _current.get()
    return ctx.access_scope if ctx is not None else AccessScope.public()


def bind(ctx: RequestContext) -> contextvars.Token:
    return _current.set(ctx)


@contextmanager
def bound(ctx: RequestContext) -> Iterator[RequestContext]:
    """Bind ``ctx`` for the duration of a block (CLI jobs, tests)."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def submit_in_context(pool: Executor, fn: Callable[..., T], *args: Any) -> Future[T]:
    """``pool.submit`` that carries the caller's request context to the worker."""
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args)
