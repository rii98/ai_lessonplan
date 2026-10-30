"""RBAC policy: roles → permissions, from config, validated at boot (D6).

Fail closed at every step: an unknown permission name or a ``role_map`` that
would let an issuer mint a superadmin aborts startup; an unmapped role at runtime
gets no permissions (→ 403), never a fallback role.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..config import AuthConfig, RbacConfig
from .principal import SUPERADMIN, Perm

_WILDCARD = "*"


class PolicyError(ValueError):
    """The access configuration is unsafe or inconsistent."""


class RbacPolicy:
    def __init__(self, roles: dict[str, frozenset[Perm]]) -> None:
        self._roles = roles

    @classmethod
    def from_config(cls, rbac: RbacConfig, auth: AuthConfig | None = None) -> RbacPolicy:
        known = {p.value: p for p in Perm}
        resolved: dict[str, frozenset[Perm]] = {}
        for role, names in rbac.roles.items():
            perms: set[Perm] = set()
            for name in names:
                if name == _WILDCARD:
                    perms.update(Perm)
                elif name in known:
                    perms.add(known[name])
                else:
                    raise PolicyError(
                        f"rbac.roles.{role}: unknown permission {name!r}. "
                        f"Known: {sorted(known)} or '*'."
                    )
            resolved[role] = frozenset(perms)
        if auth is not None:
            for issuer in auth.issuers:
                for theirs, ours in issuer.role_map.items():
                    if ours == SUPERADMIN:
                        raise PolicyError(
                            f"auth.issuers[{issuer.name}].role_map maps {theirs!r} to "
                            "'superadmin'. Superadmin is a local CLI grant only (D5)."
                        )
                    if ours not in resolved:
                        raise PolicyError(
                            f"auth.issuers[{issuer.name}].role_map maps {theirs!r} to "
                            f"{ours!r}, which rbac.roles does not define."
                        )
        return cls(resolved)

    def permissions(self, roles: Iterable[str]) -> frozenset[Perm]:
        out: set[Perm] = set()
        for r in roles:
            out |= self._roles.get(r, frozenset())
        return frozenset(out)

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self._roles)
