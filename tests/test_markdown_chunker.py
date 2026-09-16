"""The structure-aware MarkdownChunker preserves header hierarchy as metadata and
splits large sections without shattering the breadcrumb."""

from __future__ import annotations

from lessonforge.rag.chunkers import MarkdownChunker
from lessonforge.rag.documents import Collection

_DOC = """\
# Force

Intro to force.

## Newton's Laws

Overview of the three laws.

### First Law

An object stays at rest unless acted on by a force.

### Second Law

F = ma relates force, mass, and acceleration.
"""


def _chunk(text: str, **kw):
    return MarkdownChunker(**kw).chunk(
        text, collection=Collection.reference, source="book.md"
    )


def test_breadcrumb_metadata_reflects_header_hierarchy():
    chunks = _chunk(_DOC)
    by_heading = {c.metadata.get("heading"): c for c in chunks}
    assert "First Law" in by_heading
    first = by_heading["First Law"]
    assert first.metadata["heading_path"] == "Force > Newton's Laws > First Law"
    assert first.metadata["heading_level"] == 3
    # the passage text carries the breadcrumb so the embedding has context
    assert "[Force > Newton's Laws > First Law]" in first.text
    assert "stays at rest" in first.text


def test_every_section_becomes_a_chunk_and_preamble_is_kept():
    headings = {c.metadata.get("heading") for c in _chunk(_DOC)}
    assert {"Force", "Newton's Laws", "First Law", "Second Law"} <= headings


def test_long_section_splits_but_shares_the_breadcrumb():
    long_body = "\n\n".join(f"Paragraph {i} about the first law of motion." for i in range(12))
    doc = f"# Force\n\n## First Law\n\n{long_body}\n"
    chunks = [c for c in _chunk(doc, max_chars=120) if c.metadata.get("heading") == "First Law"]
    assert len(chunks) > 1  # the big section was paragraph-packed into several chunks
    assert all(c.metadata["heading_path"] == "Force > First Law" for c in chunks)


def test_split_levels_limits_chunk_granularity():
    # only split on H1/H2 → the H3 sub-headings stay inside their H2 section's body
    chunks = _chunk(_DOC, split_levels=(1, 2))
    headings = {c.metadata.get("heading") for c in chunks}
    assert "First Law" not in headings  # folded into "Newton's Laws"
    laws = next(c for c in chunks if c.metadata.get("heading") == "Newton's Laws")
    assert "First Law" in laws.text and "Second Law" in laws.text


def test_hash_in_fenced_code_is_not_a_heading():
    doc = "# Title\n\n```python\n# this is a comment, not a heading\nx = 1\n```\n"
    chunks = _chunk(doc)
    headings = {c.metadata.get("heading") for c in chunks}
    assert headings == {"Title"}
    assert "this is a comment" in chunks[0].text


def test_chunking_is_deterministic_and_ids_are_stable():
    a = _chunk(_DOC)
    b = _chunk(_DOC)
    assert [c.id for c in a] == [c.id for c in b]
    assert [c.text for c in a] == [c.text for c in b]


def test_document_metadata_and_breadcrumb_both_land_in_payload():
    chunks = MarkdownChunker().chunk(
        "# Only\n\nBody here.", collection=Collection.reference,
        source="book.md", metadata={"grade": 6, "subject": "Science"},
    )
    payload = chunks[0].payload()
    assert payload["grade"] == 6 and payload["subject"] == "Science"
    assert payload["heading"] == "Only"
    assert payload["collection"] == "reference"


# ── chapter bucketing (per-book chapter_level) ────────────────────────────────
# A Science book's chapter is its "# Unit N" (H1); a Math book's is a
# "## Chapter N" nested under a "# Unit" grouping (H2). The chunker must bucket
# each per chapter, not blindly at the outermost heading.

_SCIENCE = """\
# Unit 11 : Electricity

Overview.

## 11.1 Electric Current

Charge in motion.
"""

_MATH = """\
# Unit IV : Algebra

## Chapter 11 : Exponential Equation

### 11.1 Using Quadratic Equations

Solve by substitution.

## Chapter 12 : Area

### 12.1 Triangles

Base times height.
"""


def test_default_chapter_is_the_outermost_heading_when_no_markers():
    # the Force doc has no Chapter/Unit markers → chapter = outermost H1 (back-compat)
    for c in _chunk(_DOC):
        assert c.metadata["chapter"] == "Force"
        assert c.metadata["chapter_level"] == 1


def test_unit_heading_is_detected_as_the_chapter_level():
    chunks = _chunk(_SCIENCE)
    sec = next(c for c in chunks if c.metadata.get("heading") == "11.1 Electric Current")
    assert sec.metadata["chapter"] == "Unit 11 : Electricity"
    assert sec.metadata["chapter_level"] == 1


def test_chapter_heading_beats_unit_wrapper_and_buckets_per_chapter():
    chunks = _chunk(_MATH)
    by_heading = {c.metadata.get("heading"): c for c in chunks}
    # chapter_level auto-detects to 2 (the "Chapter N" level), NOT the "Unit IV" H1
    s11 = by_heading["11.1 Using Quadratic Equations"]
    s12 = by_heading["12.1 Triangles"]
    assert s11.metadata["chapter_level"] == 2
    # the two sections land in DIFFERENT chapter buckets — the unit no longer
    # swallows every chapter into one
    assert s11.metadata["chapter"] == "Chapter 11 : Exponential Equation"
    assert s12.metadata["chapter"] == "Chapter 12 : Area"


def test_explicit_chapter_level_overrides_detection():
    # force chapter granularity at H2 even though this book has no markers
    chunks = _chunk(_MATH, chapter_level=1)
    s11 = next(c for c in chunks if c.metadata.get("heading") == "11.1 Using Quadratic Equations")
    # at level 1 the whole unit is one chapter again
    assert s11.metadata["chapter"] == "Unit IV : Algebra"
    assert s11.metadata["chapter_level"] == 1
