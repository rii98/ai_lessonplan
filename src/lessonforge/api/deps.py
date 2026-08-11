"""FastAPI dependency wiring. The Container is a cached singleton; tests override
it via ``app.dependency_overrides[get_container]``."""

from __future__ import annotations

from functools import lru_cache

from ..container import Container


@lru_cache(maxsize=1)
def get_container() -> Container:
    return Container.from_settings()
