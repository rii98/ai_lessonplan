"""Stage registry — maps a config ``provider`` string to a pipeline-stage class.

The same loose-coupling pattern as ``providers.registry``, applied to the
pipeline stages that vary by strategy: the **intake** parser and the **critic**.
A new strategy is a new class plus a one-line ``@register_*`` decorator; an
unknown provider fails loudly with the list of what IS available.

Kept separate from the provider registry because these stages are composed from
providers (they take an ``LLMClient``), not swappable backends themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from ..config import ChatStoreConfig, CritiqueConfig, IntakeConfig, ProfileConfig

if TYPE_CHECKING:
    from ..providers.base import LLMClient
    from .chat.store import ChatStore
    from .critique import Critic
    from .intake import Intake
    from .profile import ProfileStore

T = TypeVar("T")

INTAKE_REGISTRY: dict[str, type[Intake]] = {}
CRITIC_REGISTRY: dict[str, type[Critic]] = {}
PROFILE_STORE_REGISTRY: dict[str, type[ProfileStore]] = {}
CHAT_STORE_REGISTRY: dict[str, type[ChatStore]] = {}


def _register(registry: dict[str, type[T]], name: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        if name in registry:
            raise ValueError(f"stage {name!r} already registered as {registry[name]!r}")
        registry[name] = cls
        return cls

    return deco


def register_intake(name: str):
    return _register(INTAKE_REGISTRY, name)


def register_critic(name: str):
    return _register(CRITIC_REGISTRY, name)


def register_profile_store(name: str):
    return _register(PROFILE_STORE_REGISTRY, name)


def register_chat_store(name: str):
    return _register(CHAT_STORE_REGISTRY, name)


def _lookup(registry: dict[str, type[T]], provider: str, kind: str) -> type[T]:
    try:
        return registry[provider]
    except KeyError:
        available = ", ".join(sorted(registry)) or "<none registered>"
        raise ValueError(
            f"Unknown {kind} provider {provider!r}. Available: {available}. "
            f"Check config/config.yaml or register the stage."
        ) from None


def build_intake(cfg: IntakeConfig, *, llm: LLMClient) -> Intake:
    from . import intake as _  # noqa: F401  (import triggers stage registration)

    return _lookup(INTAKE_REGISTRY, cfg.provider, "intake").from_config(cfg, llm=llm)  # type: ignore[attr-defined]


def build_critic(cfg: CritiqueConfig, *, llm: LLMClient) -> Critic:
    from . import critique as _  # noqa: F401  (import triggers stage registration)

    return _lookup(CRITIC_REGISTRY, cfg.provider, "critic").from_config(cfg, llm=llm)  # type: ignore[attr-defined]


def build_profile_store(cfg: ProfileConfig) -> ProfileStore:
    from . import profile as _  # noqa: F401  (import triggers store registration)

    return _lookup(PROFILE_STORE_REGISTRY, cfg.provider, "profile store").from_config(cfg)  # type: ignore[attr-defined]


def build_chat_store(cfg: ChatStoreConfig) -> ChatStore:
    from .chat import store as _  # noqa: F401  (import triggers store registration)

    return _lookup(CHAT_STORE_REGISTRY, cfg.provider, "chat store").from_config(cfg)  # type: ignore[attr-defined]
