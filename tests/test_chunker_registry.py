"""The chunker registry resolves a name → chunker and tolerates cross-strategy
params, mirroring the loader/provider registries."""

from __future__ import annotations

import pytest

from lessonforge.rag.chunkers import (
    MarkdownChunker,
    ParagraphChunker,
    build_chunker,
)
from lessonforge.rag.ingest import ChunkerPolicy


def test_build_resolves_registered_names():
    assert isinstance(build_chunker("paragraph"), ParagraphChunker)
    assert isinstance(build_chunker("markdown"), MarkdownChunker)


def test_unknown_chunker_fails_loudly_with_available():
    with pytest.raises(ValueError, match="Unknown chunker 'nope'"):
        build_chunker("nope")


def test_params_are_filtered_to_the_constructor():
    # split_levels is markdown-only; paragraph must not choke on it
    para = build_chunker("paragraph", max_chars=400, split_levels=(1,))
    assert isinstance(para, ParagraphChunker) and para.max_chars == 400
    md = build_chunker("markdown", max_chars=400, split_levels=(1, 2))
    assert isinstance(md, MarkdownChunker) and md.split_levels == frozenset({1, 2})


def test_policy_picks_chunker_by_format():
    policy = ChunkerPolicy(default="paragraph", by_format={"markdown": "markdown"})
    assert isinstance(policy.for_format("jsonl"), ParagraphChunker)
    assert isinstance(policy.for_format("markdown"), MarkdownChunker)
    # cached: same instance back for the same format
    assert policy.for_format("markdown") is policy.for_format("markdown")


def test_policy_from_config_uses_configured_defaults():
    from lessonforge.config import ChunkingConfig

    policy = ChunkerPolicy.from_config(ChunkingConfig(params={"max_chars": 999}))
    md = policy.for_format("markdown")
    assert isinstance(md, MarkdownChunker) and md.max_chars == 999
