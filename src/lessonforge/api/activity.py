"""Emitting activity events from routes: one call, never fails the request."""

from __future__ import annotations

import time
from typing import Any

from .. import request_context
from ..activity import events as act
from ..container import Container


def emit(c: Container, type_: str, props: act.Props, *, resource_type: Any = None,
         resource_id: str | None = None) -> None:
    try:
        c.platform.activity.record(act.build_event(type_, props, resource_type=resource_type,
                                                   resource_id=resource_id))
    except Exception:   # analytics must never break a request
        return


def cost(started: float) -> dict[str, int]:
    """Latency and the request's summed token usage so far, for *.generated events."""
    ctx = request_context.current()
    usage = ctx.usage.snapshot() if ctx else {}
    return {"latency_ms": int((time.perf_counter() - started) * 1000),
            "tokens_in": usage.get("tokens_in", 0), "tokens_out": usage.get("tokens_out", 0)}
