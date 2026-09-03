"""The QA chatbot subsystem: a streaming, persistent, advanced-RAG assistant
over the grounding collections.

Each stage is its own small, single-responsibility unit behind a narrow
interface, composed by :class:`~lessonforge.services.chat.pipeline.ChatPipeline`
and wired in the :class:`~lessonforge.container.Container`:

    memory ─► transform ─► retrieve ─► context ─► synthesize ─► persist

Storage (:class:`ChatStore`) and query understanding (:class:`QueryTransformer`)
are pluggable ports resolved by config, exactly like the rest of the codebase.
"""
