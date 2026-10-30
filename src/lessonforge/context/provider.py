"""UserContextProvider — the port for "tell me more about this user", plus the
dependency-free ``claims`` adapter and the registry config selects from.

``context.provider`` picks the adapter: ``claims`` builds context from the signed
token alone (zero dependencies — the default for tests and partners without a
directory endpoint); ``edboox_http`` (in :mod:`lessonforge.integrations.edboox`)
pulls the richer projection from Edboox.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..config import ContextConfig
from .model import SchoolRef, StudentContext, UserContext

if TYPE_CHECKING:
    from ..auth.principal import Principal
    from ..domain.vocabulary import Vocabulary


class ContextUnavailable(RuntimeError):
    """The upstream directory could not answer (callers keep last-known context)."""


class UserContextProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, principal: Principal) -> UserContext | None:
        """Fresh context for ``principal`` (``None`` = upstream has no record).
        Raises :class:`ContextUnavailable` when the upstream is unreachable."""


class ClaimsContextProvider(UserContextProvider):
    """Context from the token only: the signed school and (students) grade."""

    name = "claims"

    def fetch(self, principal: Principal) -> UserContext | None:
        return claims_context(principal)


def claims_context(principal: Principal) -> UserContext:
    return UserContext(
        school=SchoolRef(id=principal.tenant_id) if principal.tenant_id else None,
        student=(StudentContext(grade=principal.grade)
                 if principal.role == "student" else None),
        source="claims",
    )


ProviderFactory = Callable[[ContextConfig, "Vocabulary"], UserContextProvider]
CONTEXT_PROVIDERS: dict[str, ProviderFactory] = {
    "claims": lambda cfg, vocab: ClaimsContextProvider(),
}


def register_context_provider(name: str) -> Callable[[ProviderFactory], ProviderFactory]:
    def deco(factory: ProviderFactory) -> ProviderFactory:
        if name in CONTEXT_PROVIDERS:
            raise ValueError(f"context provider {name!r} already registered")
        CONTEXT_PROVIDERS[name] = factory
        return factory
    return deco


def build_context_provider(cfg: ContextConfig, vocabulary: Vocabulary) -> UserContextProvider:
    from ..integrations import edboox as _  # noqa: F401  (registers edboox_http)

    try:
        factory = CONTEXT_PROVIDERS[cfg.provider]
    except KeyError:
        raise ValueError(f"Unknown context provider {cfg.provider!r}. "
                         f"Available: {', '.join(sorted(CONTEXT_PROVIDERS))}") from None
    return factory(cfg, vocabulary)
