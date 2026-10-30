"""``lessonforge-admin`` — operator commands that must never be HTTP endpoints.

Superadmin is a LOCAL grant (D5): there is no API that grants roles, and Edboox
can never assert it. This CLI needs database access — that is the boundary — and
every change it makes is written to the audit log as ``cli:<os user>``.

::

    lessonforge-admin grant   --subject teacher:1 [--issuer edboox] [--role superadmin]
    lessonforge-admin revoke  --subject teacher:1
    lessonforge-admin grants
    lessonforge-admin disable --subject student:42        # local kill switch → 403
    lessonforge-admin enable  --subject student:42
    lessonforge-admin forget  --subject student:42 --yes  # delete-on-request: cascades
    lessonforge-admin sync-context --subject student:42   # pull Edboox context now
    lessonforge-admin purge                               # expired launch jtis + old events
    lessonforge-admin audit [--limit 50]
    lessonforge-admin backfill-spaces                     # tag pre-space corpus as 'global'
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

from ..audit import AuditEntry
from ..config import Settings, load_settings
from ..platform import Platform


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # pragma: no cover - no passwd entry in some containers
        return "cli:unknown"


def _audit(platform: Platform, action: str, target: str, **detail) -> None:
    platform.audit.record(AuditEntry(actor=_actor(), action=action, target=target, detail=detail))


def cmd_grant(p: Platform, a: argparse.Namespace) -> int:
    added = p.directory.grant(a.issuer, a.subject, a.role, granted_by=_actor())
    _audit(p, "grant.add", f"{a.issuer}:{a.subject}", role=a.role, changed=added)
    print(f"granted {a.role} to {a.issuer}:{a.subject}" if added else "already granted")
    return 0


def cmd_revoke(p: Platform, a: argparse.Namespace) -> int:
    removed = p.directory.revoke(a.issuer, a.subject, a.role)
    _audit(p, "grant.remove", f"{a.issuer}:{a.subject}", role=a.role, changed=removed)
    print(f"revoked {a.role} from {a.issuer}:{a.subject}" if removed else "no such grant")
    return 0 if removed else 1


def cmd_grants(p: Platform, a: argparse.Namespace) -> int:
    rows = p.directory.grants()
    for g in rows:
        print(f"{g.issuer}:{g.subject}\t{g.role}\tby {g.granted_by}\t{g.granted_at:%Y-%m-%d %H:%M}")
    if not rows:
        print("(no local grants)")
    return 0


def _set_disabled(disabled: bool) -> Callable[[Platform, argparse.Namespace], int]:
    def run(p: Platform, a: argparse.Namespace) -> int:
        ok = p.directory.set_disabled(a.issuer, a.subject, disabled)
        _audit(p, "principal.disable" if disabled else "principal.enable",
               f"{a.issuer}:{a.subject}", found=ok)
        print(("disabled " if disabled else "enabled ") + f"{a.issuer}:{a.subject}" if ok
              else "unknown principal")
        return 0 if ok else 1
    return run


def cmd_forget(p: Platform, a: argparse.Namespace) -> int:
    if not a.yes:
        print("refusing without --yes: this deletes the user's documents, chats and profile "
              "and anonymises their activity", file=sys.stderr)
        return 2
    ok = p.directory.delete(a.issuer, a.subject)
    _audit(p, "principal.forget", f"{a.issuer}:{a.subject}", found=ok)
    print(f"forgot {a.issuer}:{a.subject}" if ok else "unknown principal")
    return 0 if ok else 1


def cmd_sync_context(p: Platform, a: argparse.Namespace) -> int:
    rec = p.directory.find(a.issuer, a.subject)
    if rec is None:
        print("unknown principal (they must have logged in once)", file=sys.stderr)
        return 1
    from ..auth.principal import Principal

    principal = Principal(id=rec.id, issuer=rec.issuer, subject=rec.subject,
                          role=rec.external_role, roles=frozenset({rec.external_role}),
                          permissions=frozenset(), tenant_id=rec.tenant_id,
                          display_name=rec.display_name)
    ctx = p.context.refresh(principal)
    if ctx is None:
        print("no context returned (upstream unavailable or user unknown); last-known kept")
        return 1
    print(ctx.model_dump_json(indent=2))
    return 0


def cmd_purge(p: Platform, a: argparse.Namespace) -> int:
    tokens = p.replay.purge()
    events = p.activity_reader.purge_older_than(p.settings.activity.retention_days)
    _audit(p, "maintenance.purge", "platform", launch_tokens=tokens, activity_events=events)
    print(f"purged {tokens} expired launch token id(s), {events} activity event(s) older than "
          f"{p.settings.activity.retention_days} days")
    return 0


def cmd_audit(p: Platform, a: argparse.Namespace) -> int:
    for e in p.audit.recent(a.limit):
        print(f"{e.at:%Y-%m-%d %H:%M:%S}\t{e.actor}\t{e.action}\t{e.target or ''}\t{e.detail}")
    return 0


def cmd_backfill_spaces(p: Platform, a: argparse.Namespace) -> int:
    from ..knowledge.scope import GLOBAL_SPACE
    from ..providers import registry as _  # noqa: F401  (registers adapters)
    from ..providers.registry import build_vector_store
    from ..rag.documents import Collection

    store = build_vector_store(p.settings.vector_store)
    total = 0
    for col in Collection:
        n = store.backfill_space(col.value, GLOBAL_SPACE)
        total += max(n, 0)
        print(f"{col.value}: {n} point(s) tagged '{GLOBAL_SPACE}'")
    _audit(p, "corpus.backfill_spaces", "vector_store", points=total)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lessonforge-admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def who(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--issuer", default="edboox", help="configured issuer name")
        sp.add_argument("--subject", required=True, help="token 'sub', e.g. teacher:1")
        return sp

    for name, fn in (("grant", cmd_grant), ("revoke", cmd_revoke)):
        sp = who(sub.add_parser(name))
        sp.add_argument("--role", default="superadmin", choices=["superadmin"])
        sp.set_defaults(fn=fn)
    sub.add_parser("grants").set_defaults(fn=cmd_grants)
    who(sub.add_parser("disable")).set_defaults(fn=_set_disabled(True))
    who(sub.add_parser("enable")).set_defaults(fn=_set_disabled(False))
    sp = who(sub.add_parser("forget"))
    sp.add_argument("--yes", action="store_true", help="confirm the irreversible delete")
    sp.set_defaults(fn=cmd_forget)
    who(sub.add_parser("sync-context")).set_defaults(fn=cmd_sync_context)
    sub.add_parser("purge").set_defaults(fn=cmd_purge)
    sp = sub.add_parser("audit")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_audit)
    sub.add_parser("backfill-spaces").set_defaults(fn=cmd_backfill_spaces)
    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None,
         platform: Platform | None = None) -> int:
    args = build_parser().parse_args(argv)
    platform = platform or Platform(settings or load_settings())
    try:
        return int(args.fn(platform, args))
    finally:
        platform.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
