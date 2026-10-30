"""Edboox adapter: pull a user's context projection and map it into OUR model.

This is the anti-corruption layer (§6): Edboox's shapes (``"Class 8"``, long
subject names, ``"nepali"`` medium) are translated through the shared vocabulary
here and nowhere else — if Edboox renames a field, this file is the only change.

Contract 3::

    GET {base_url}/api/v1/ai/users/{sub}      X-Api-Key: <read-only key>
    200 {"sub", "role", "school": {...}, "student": {...} | null,
         "teacher": {"assignments": [...]} | null, "preferred_language", "updated_at"}
    404 → the user is unknown upstream

How we authenticate is hidden behind this adapter, so moving to OAuth client
credentials later changes nothing outside it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ..config import ContextConfig
from ..context.model import (
    SchoolRef,
    StudentContext,
    TeacherContext,
    TeachingAssignment,
    UserContext,
)
from ..context.provider import ContextUnavailable, UserContextProvider, register_context_provider
from ..domain.vocabulary import Vocabulary

if TYPE_CHECKING:
    from ..auth.principal import Principal

log = logging.getLogger(__name__)

_LANG = {"en": "en", "english": "en", "ne": "ne", "np": "ne", "nepali": "ne", "नेपाली": "ne"}


class EdbooxMapper:
    """Edboox projection → :class:`UserContext`. Tolerant: an unknown or missing
    field drops that fact (context is a hint), it never fails the whole record."""

    def __init__(self, vocabulary: Vocabulary) -> None:
        self.vocab = vocabulary

    def map(self, data: dict[str, Any]) -> UserContext:
        school = data.get("school") or None
        student = data.get("student") or None
        teacher = data.get("teacher") or None
        return UserContext(
            school=SchoolRef(
                id=str(school["id"]), name=_s(school.get("name")),
                district=_s(school.get("district")), province=_s(school.get("province")),
            ) if isinstance(school, dict) and school.get("id") else None,
            language=_lang(data.get("preferred_language")),
            student=self._student(student) if isinstance(student, dict) else None,
            teacher=self._teacher(teacher) if isinstance(teacher, dict) else None,
            source="edboox_http",
        )

    def _student(self, s: dict[str, Any]) -> StudentContext:
        optional = [self.vocab.subject(x) for x in s.get("optional_subjects") or []]
        return StudentContext(
            grade=self.vocab.grade(s.get("grade")),
            section=_s(s.get("section")),
            medium=_lang(s.get("medium")),
            stream=_s(s.get("stream")),
            optional_subjects=[x for x in optional if x],
        )

    def _teacher(self, t: dict[str, Any]) -> TeacherContext:
        out: list[TeachingAssignment] = []
        for a in t.get("assignments") or []:
            if not isinstance(a, dict):
                continue
            grade, subject = self.vocab.grade(a.get("grade")), self.vocab.subject(a.get("subject"))
            if grade is None or subject is None:
                log.info("edboox: dropping unmappable assignment grade=%r subject=%r",
                         a.get("grade"), a.get("subject"))
                continue
            out.append(TeachingAssignment(grade=grade, subject=subject,
                                          section=_s(a.get("section")),
                                          class_id=_s(a.get("class_id"))))
        return TeacherContext(assignments=out)


class EdbooxHttpContextProvider(UserContextProvider):
    name = "edboox_http"

    def __init__(self, cfg: ContextConfig, vocabulary: Vocabulary,
                 *, http: httpx.Client | None = None) -> None:
        if not cfg.base_url:
            raise ValueError("context.base_url is required for the edboox_http provider")
        self.cfg = cfg
        self.mapper = EdbooxMapper(vocabulary)
        self._http = http or httpx.Client(timeout=cfg.timeout_s)

    def fetch(self, principal: Principal) -> UserContext | None:
        url = f"{self.cfg.base_url.rstrip('/')}/api/v1/ai/users/{quote(principal.subject, safe='')}"
        try:
            resp = self._http.get(url, headers={"X-Api-Key": self.cfg.api_key})
        except httpx.HTTPError as exc:
            raise ContextUnavailable(f"edboox context endpoint unreachable: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ContextUnavailable(f"edboox context endpoint returned {resp.status_code}")
        try:
            return self.mapper.map(resp.json())
        except Exception as exc:   # malformed projection: keep last-known context
            raise ContextUnavailable(f"edboox context payload unusable: {exc}") from exc


@register_context_provider("edboox_http")
def _build(cfg: ContextConfig, vocabulary: Vocabulary) -> UserContextProvider:
    return EdbooxHttpContextProvider(cfg, vocabulary)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _lang(v: Any) -> str | None:
    return _LANG.get(str(v).strip().casefold()) if v is not None else None
