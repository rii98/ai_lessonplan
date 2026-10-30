"""The ``/api/v1`` contract guard (API contract rule 3).

``docs/openapi.v1.json`` is the published contract Edboox generates its client
from. :func:`breaking_changes` compares it with the live schema and lists every
change that would break an existing client:

- a removed path or method;
- a new *required* parameter, or an optional one that became required;
- a request-body property that became required;
- a response property that disappeared, or any property whose type changed.

Additive changes (new paths, new optional inputs, new response fields) stay in
v1. A breaking change needs ``/api/v2`` with v1 kept alive for a deprecation window.

Regenerate the snapshot after an intentional, additive change::

    python -m lessonforge.api.contract --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SNAPSHOT = Path(__file__).resolve().parents[3] / "docs" / "openapi.v1.json"
_METHODS = ("get", "put", "post", "delete", "patch")


def current_schema() -> dict[str, Any]:
    from .main import create_app

    spec = create_app().openapi()
    spec["paths"] = {p: ops for p, ops in spec["paths"].items() if p.startswith("/api/v1")}
    return spec


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    if isinstance(node, dict) and "$ref" in node and depth < 8:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _resolve(spec, spec.get("components", {}).get("schemas", {}).get(name, {}), depth + 1)
    return node


def _props(spec: dict[str, Any], schema: Any) -> tuple[dict[str, Any], set[str]]:
    s = _resolve(spec, schema) or {}
    for key in ("allOf", "anyOf", "oneOf"):
        if key in s and len(s[key]) == 1:
            s = _resolve(spec, s[key][0])
    return s.get("properties", {}) or {}, set(s.get("required", []) or [])


def _type(spec: dict[str, Any], schema: Any) -> Any:
    s = _resolve(spec, schema) or {}
    if "anyOf" in s:
        return sorted(json.dumps(_type(spec, x), sort_keys=True) for x in s["anyOf"])
    return s.get("type"), s.get("format")


def _body(spec: dict[str, Any], op: dict[str, Any]) -> Any:
    return (op.get("requestBody", {}).get("content", {}).get("application/json", {})
            .get("schema"))


def _response(spec: dict[str, Any], op: dict[str, Any]) -> Any:
    ok = op.get("responses", {}).get("200", {})
    return ok.get("content", {}).get("application/json", {}).get("schema")


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for path, old_ops in old.get("paths", {}).items():
        new_ops = new.get("paths", {}).get(path)
        if new_ops is None:
            out.append(f"removed path {path}")
            continue
        for method in _METHODS:
            if method not in old_ops:
                continue
            where = f"{method.upper()} {path}"
            if method not in new_ops:
                out.append(f"removed {where}")
                continue
            o, n = old_ops[method], new_ops[method]
            old_params = {(p["in"], p["name"]): p for p in o.get("parameters", [])}
            for p in n.get("parameters", []):
                before = old_params.get((p["in"], p["name"]))
                if p.get("required") and (before is None or not before.get("required")):
                    out.append(f"{where}: parameter {p['name']!r} is now required")
                if before is not None and _type(new, p.get("schema")) != _type(old, before.get("schema")):
                    out.append(f"{where}: parameter {p['name']!r} changed type")
            if _body(old, o) is not None or _body(new, n) is not None:
                oprops, oreq = _props(old, _body(old, o))
                nprops, nreq = _props(new, _body(new, n))
                for name in sorted(nreq - oreq):
                    out.append(f"{where}: request field {name!r} is now required")
                if o.get("requestBody") is None and n.get("requestBody", {}).get("required"):
                    out.append(f"{where}: a request body is now required")
                for name in sorted(set(oprops) & set(nprops)):
                    if _type(old, oprops[name]) != _type(new, nprops[name]):
                        out.append(f"{where}: request field {name!r} changed type")
            oprops, _ = _props(old, _response(old, o))
            nprops, _ = _props(new, _response(new, n))
            for name in sorted(set(oprops) - set(nprops)):
                out.append(f"{where}: response field {name!r} removed")
            for name in sorted(set(oprops) & set(nprops)):
                if _type(old, oprops[name]) != _type(new, nprops[name]):
                    out.append(f"{where}: response field {name!r} changed type")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or refresh the /api/v1 contract.")
    parser.add_argument("--write", action="store_true", help="rewrite docs/openapi.v1.json")
    args = parser.parse_args(argv)
    new = current_schema()
    if args.write:
        if SNAPSHOT.exists():
            problems = breaking_changes(json.loads(SNAPSHOT.read_text()), new)
            if problems:
                print("refusing to overwrite: breaking changes (needs /api/v2):", *problems,
                      sep="\n  ", file=sys.stderr)
                return 1
        SNAPSHOT.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")
        print(f"wrote {SNAPSHOT}")
        return 0
    problems = breaking_changes(json.loads(SNAPSHOT.read_text()), new)
    print("\n".join(problems) or "no breaking changes")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
