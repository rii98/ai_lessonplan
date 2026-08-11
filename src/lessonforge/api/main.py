"""FastAPI application factory."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator

from ..container import Container
from ..domain.ldd import IntakeRequest, LessonDesignDocument, NormalizedBrief
from ..domain.rubric import Critique
from ..export import ArtifactKind, RenderedArtifact
from .deps import get_container


class GenerateRequest(BaseModel):
    """A generation request. ``topic`` may be omitted when ``existing_plan`` is
    supplied — intake extracts it from the pasted plan (US-3)."""

    topic: str | None = Field(default=None, examples=["Components of Environment: Biotic and Abiotic"])
    grade: int | None = Field(default=None, ge=1, le=12, examples=[6])
    subject: str | None = Field(default=None, examples=["Science"])
    duration_min: Literal[30, 45, 60] | None = None
    language: Literal["en", "ne", "en-ne"] | None = None
    framework: Literal["5E", "gradual_release", "inquiry"] | None = None
    existing_plan: str | None = None

    @model_validator(mode="after")
    def _need_topic_or_plan(self) -> GenerateRequest:
        if not self.topic and not self.existing_plan:
            raise ValueError("provide `topic`, or `existing_plan` for intake to parse")
        return self


def create_app() -> FastAPI:
    app = FastAPI(
        title="LessonForge — AI Lesson Plan Creator",
        version="0.1.0",
        summary="AI that thinks like an experienced teacher.",
    )

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/providers", tags=["ops"])
    def provider_health(c: Container = Depends(get_container)) -> dict[str, object]:
        """Reports which configured backend is wired for each component and
        whether the networked ones are reachable."""
        s = c.settings
        return {
            "llm": {"provider": s.llm.provider, "model": s.llm.model, "reachable": c.llm.health()},
            "embedding": {"provider": s.embedding.provider, "model": s.embedding.model},
            "reranker": {"provider": s.reranker.provider, "model": s.reranker.model},
            "vector_store": {
                "provider": s.vector_store.provider,
                "reachable": c.vector_store.health(),
            },
        }

    @app.post("/lessons/generate", response_model=LessonDesignDocument, tags=["lessons"])
    def generate_lesson(
        req: GenerateRequest, c: Container = Depends(get_container)
    ) -> LessonDesignDocument:
        """Full pipeline: intake → enrichment → critique & revise → validated LDD."""
        try:
            return c.pipeline.run(IntakeRequest(**req.model_dump()))
        except ValueError as exc:  # intake couldn't determine topic/grade
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/lessons/intake", response_model=NormalizedBrief, tags=["lessons"])
    def intake_lesson(
        req: GenerateRequest, c: Container = Depends(get_container)
    ) -> NormalizedBrief:
        """US-3 preview: normalize a raw request / pasted plan into a brief,
        without generating — so a teacher can confirm what was parsed."""
        try:
            return c.intake.normalize(IntakeRequest(**req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/lessons/critique", response_model=Critique, tags=["lessons"])
    def critique_lesson(
        ldd: LessonDesignDocument, c: Container = Depends(get_container)
    ) -> Critique:
        """Score an already-generated LDD against the anti-generic rubric."""
        return c.pipeline.critique(ldd)

    # ── export: LDD → downloadable artifacts ─────────────────────────────────
    def _file_response(art: RenderedArtifact) -> Response:
        return Response(
            content=art.content,
            media_type=art.media_type,
            headers={"Content-Disposition": f'attachment; filename="{art.filename}"'},
        )

    @app.get("/export/manifest", tags=["export"])
    def export_manifest(c: Container = Depends(get_container)) -> dict[str, object]:
        """What the export subsystem can produce and in which formats."""
        return c.exporter.manifest()

    @app.post("/lessons/export/{kind}", tags=["export"])
    def export_artifact(
        kind: ArtifactKind,
        ldd: LessonDesignDocument,
        c: Container = Depends(get_container),
        fmt: str | None = Query(default=None, description="Override the configured format"),
    ) -> Response:
        """Render one artifact from an already-generated LDD."""
        try:
            art = c.exporter.render(kind, ldd, fmt=fmt)
        except ValueError as exc:  # unknown/unsupported format
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _file_response(art)

    @app.post("/lessons/export/bundle/zip", tags=["export"])
    def export_bundle(
        ldd: LessonDesignDocument, c: Container = Depends(get_container)
    ) -> Response:
        """One-click zip of every configured artifact."""
        return _file_response(c.exporter.bundle(ldd))

    return app


app = create_app()
