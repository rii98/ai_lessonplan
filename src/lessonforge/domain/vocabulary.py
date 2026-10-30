"""The shared vocabulary for grades and subjects (D9).

Metadata filtering only works when both sides say the same thing: Edboox may send
``"Class 8"`` and ``"Science and Technology"`` while the corpus is tagged ``8`` and
``"Science"``. Every value crossing into LessonForge — the Edboox user-context
mapper and corpus ingestion — is normalised here, against ``config/vocabulary.yaml``.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10, "xi": 11, "xii": 12}
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_SPACES = re.compile(r"\s+")


class UnknownTermError(ValueError):
    """A value is not in the shared vocabulary (→ 422 at ingestion)."""


def _key(text: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFC", text).strip().casefold())


class Vocabulary:
    def __init__(self, subjects: dict[str, list[str]], grade_patterns: list[str]) -> None:
        self._subjects: dict[str, str] = {}
        for canonical, aliases in subjects.items():
            for term in (canonical, *aliases):
                k = _key(str(term))
                other = self._subjects.get(k)
                if other is not None and other != canonical:
                    raise ValueError(f"vocabulary alias {term!r} maps to both {other!r} "
                                     f"and {canonical!r}")
                self._subjects[k] = canonical
        self.canonical_subjects: tuple[str, ...] = tuple(subjects)
        self._grade_patterns = [re.compile(p, re.IGNORECASE) for p in grade_patterns]

    @classmethod
    def from_yaml(cls, path: str | Path) -> Vocabulary:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(subjects=data.get("subjects") or {},
                   grade_patterns=data.get("grade_patterns") or [])

    # ── subjects ─────────────────────────────────────────────────────────────
    def subject(self, value: Any) -> str | None:
        """Canonical subject for ``value``, or ``None`` if blank/unknown."""
        if value is None or str(value).strip() == "":
            return None
        return self._subjects.get(_key(str(value)))

    def require_subject(self, value: Any) -> str:
        canonical = self.subject(value)
        if canonical is None:
            raise UnknownTermError(
                f"unknown subject {value!r}. Known: {', '.join(self.canonical_subjects)} "
                "(add an alias in config/vocabulary.yaml)."
            )
        return canonical

    # ── grades ───────────────────────────────────────────────────────────────
    def grade(self, value: Any) -> int | None:
        """``8``, ``"8"``, ``"Class 8"``, ``"VIII"``, ``"कक्षा ८"`` → ``8``; else ``None``."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if 1 <= value <= 12 else None
        text = _key(str(value)).translate(_DEVANAGARI_DIGITS)
        if not text:
            return None
        roman = _ROMAN.get(re.sub(r"^(?:class|grade)\s*", "", text))
        if roman is not None:
            return roman
        for pattern in self._grade_patterns:
            m = pattern.match(text)
            if m:
                n = int(m.group(1))
                return n if 1 <= n <= 12 else None
        return None

    def require_grade(self, value: Any) -> int:
        g = self.grade(value)
        if g is None:
            raise UnknownTermError(f"unrecognised grade {value!r} (expected 1–12)")
        return g

    # ── record metadata (ingestion) ──────────────────────────────────────────
    def normalize_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Canonicalise ``grade``/``subject`` in a corpus record's metadata;
        raise :class:`UnknownTermError` for values that would be unfilterable."""
        out = dict(metadata)
        if out.get("subject") not in (None, ""):
            out["subject"] = self.require_subject(out["subject"])
        if out.get("grade") not in (None, ""):
            out["grade"] = self.require_grade(out["grade"])
        return out


def default_vocabulary_path() -> Path:
    from ..config import _default_config_path

    return _default_config_path().parent / "vocabulary.yaml"


@lru_cache(maxsize=4)
def load_vocabulary(path: str | None = None) -> Vocabulary:
    return Vocabulary.from_yaml(Path(path) if path else default_vocabulary_path())
