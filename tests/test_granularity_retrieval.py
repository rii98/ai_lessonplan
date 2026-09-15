"""Multi-granularity retrieval (Phase 1): a narrow hit can be dereferenced back
to its whole section or chapter, reassembled in document order, with no LLM.

Everything runs on the in-process fakes (embedder/store/reranker from conftest),
so the small-to-big machinery is exercised deterministically."""

from __future__ import annotations

from lessonforge.config import GroundingConfig
from lessonforge.rag.chunkers import MarkdownChunker
from lessonforge.rag.documents import (
    CHAPTER_KEY,
    CHUNK_INDEX_KEY,
    DOC_ID_KEY,
    HEADING_PATH_KEY,
    Collection,
    Document,
)
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import Retriever

_BOOK = """\
# Scientific Study

## Variables

A variable is any factor that can change during an experiment.
Identifying variables is the first step of a fair test.

## Fundamental Units

The seven SI fundamental quantities include length, mass, and time.
Mass is measured in kilograms.

# Force

## Newton's Laws

An object stays at rest unless a force acts on it.
"""


def _chunks():
    return MarkdownChunker(max_chars=200).chunk(
        _BOOK, collection=Collection.reference, source="Grade 10 Science",
        metadata={DOC_ID_KEY: "g10sci"},
    )


# ── 1a. hierarchy ids are stamped ─────────────────────────────────────────────
def test_chunks_carry_hierarchy_ids():
    chunks = _chunks()
    by_heading = {c.metadata.get("heading"): c for c in chunks}
    variables = by_heading["Variables"]
    assert variables.metadata[DOC_ID_KEY] == "g10sci"
    assert variables.metadata[CHAPTER_KEY] == "Scientific Study"
    assert variables.metadata[HEADING_PATH_KEY] == "Scientific Study > Variables"
    # chunk_index is document order and monotonic across the whole doc
    indices = [c.metadata[CHUNK_INDEX_KEY] for c in chunks]
    assert indices == sorted(indices)
    assert indices == list(range(len(chunks)))


def test_chunk_index_is_in_the_id_so_positions_stay_distinct():
    # two identical passages at different positions must not collapse to one id
    text = "# A\n\nsame words here.\n\n# B\n\nsame words here.\n"
    chunks = MarkdownChunker().chunk(
        text, collection=Collection.reference, source="s", metadata={DOC_ID_KEY: "d"}
    )
    bodies = [c.text for c in chunks]
    assert bodies[0].endswith("same words here.") and bodies[1].endswith("same words here.")
    assert chunks[0].id != chunks[1].id  # disambiguated by chunk_index


# ── 1b. section / broad expansion ─────────────────────────────────────────────
def _retriever(fake_embedder, fake_store, fake_reranker):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="g10sci", text=_BOOK, source="Grade 10 Science",
                  metadata={"grade": 10, "subject": "Science"})],
        chunker=MarkdownChunker(max_chars=200),
    )
    return Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)


def test_narrow_returns_just_the_matched_chunk(fake_embedder, fake_store, fake_reranker):
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    hits = r.retrieve(Collection.reference.value, "variable factor change", top_n=1)
    assert len(hits) == 1
    assert "variable" in hits[0].text.lower()
    # narrow does not pull in the sibling "fair test" sentence's neighbours only —
    # it's the single reranked chunk, not the whole section
    assert "kilograms" not in hits[0].text.lower()


def test_section_expands_to_the_whole_section(fake_embedder, fake_store, fake_reranker):
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    hits = r.retrieve(
        Collection.reference.value, "variable factor change", top_n=1, granularity="section"
    )
    assert len(hits) == 1
    text = hits[0].text.lower()
    # the Variables section, whole — but NOT the Fundamental Units section
    assert "variable" in text and "fair test" in text
    assert "kilograms" not in text


def test_broad_expands_to_the_whole_chapter(fake_embedder, fake_store, fake_reranker):
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    hits = r.retrieve(
        Collection.reference.value, "variable factor change", top_n=1, granularity="broad"
    )
    assert len(hits) == 1
    text = hits[0].text.lower()
    # the whole "Scientific Study" chapter: both Variables AND Fundamental Units
    assert "variable" in text and "kilograms" in text
    # but not the other chapter (Force)
    assert "newton" not in text


def test_expansion_dedupes_multiple_hits_in_one_unit(fake_embedder, fake_store, fake_reranker):
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    # two hits likely land in the Variables section; broad must emit its chapter once
    hits = r.retrieve(
        Collection.reference.value, "variable fair test experiment", top_n=3, granularity="broad"
    )
    texts = [h.text for h in hits]
    assert len(texts) == len(set(texts))  # no duplicated chapters


def test_broad_respects_the_assembly_budget(fake_embedder, fake_store, fake_reranker):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="g10sci", text=_BOOK, source="Grade 10 Science")],
        chunker=MarkdownChunker(max_chars=200),
    )
    r = Retriever(
        embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker,
        assembly_max_chars=120,
    )
    hits = r.retrieve(
        Collection.reference.value, "variable factor change", top_n=1, granularity="broad"
    )
    # capped: at least one chunk is always kept, but the budget stops the rest
    assert len(hits[0].text) <= 120 + 200  # one chunk may exceed, then it stops


def test_flat_source_without_hierarchy_falls_back_to_narrow(
    fake_embedder, fake_store, fake_reranker
):
    from lessonforge.rag.chunkers import ParagraphChunker

    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.curriculum,
        [Document(id="c1", text="Students learn about variables and fair tests.",
                  source="CDC")],
        chunker=ParagraphChunker(),
    )
    r = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    # no heading hierarchy → broad simply returns the narrow hit, never crashes
    hits = r.retrieve(Collection.curriculum.value, "variables", granularity="broad")
    assert hits and "variables" in hits[0].text.lower()


# ── 1c. free heading skeleton ─────────────────────────────────────────────────
def test_skeleton_lists_section_headings_in_order(fake_embedder, fake_store, fake_reranker):
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig())
    toc = gr.skeleton(Collection.reference.value, doc_id="g10sci")
    assert toc == [
        "Scientific Study > Variables",
        "Scientific Study > Fundamental Units",
        "Force > Newton's Laws",
    ]


def test_skeleton_can_scope_to_one_chapter(fake_embedder, fake_store, fake_reranker):
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig())
    toc = gr.skeleton(Collection.reference.value, doc_id="g10sci", chapter="Scientific Study")
    assert toc == [
        "Scientific Study > Variables",
        "Scientific Study > Fundamental Units",
    ]


# ── 1d. coverage outline: anchor-then-enumerate (unit planning) ────────────────
def test_outline_enumerates_the_whole_chapter_a_single_anchor_located(
    fake_embedder, fake_store, fake_reranker
):
    """The core guarantee: one vector hit only *locates* the chapter; the outline
    then lists EVERY section of it — including a sibling the query never mentioned —
    and does not leak into a chapter no anchor pointed at."""
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig(coverage_anchors=1))
    outline = gr.outline(query="variable factor change", grade=10, subject="Science")
    # Fundamental Units is unrelated to "variable" but shares the chapter → kept.
    assert outline.sections == [
        "Scientific Study > Variables",
        "Scientific Study > Fundamental Units",
    ]
    assert outline.chapters == ["Scientific Study"]
    # a single anchor in one chapter must not pull in the Force chapter
    assert "Force > Newton's Laws" not in outline.sections


def test_outline_prunes_a_stray_chapter_to_the_dominant_one(
    fake_embedder, fake_store, fake_reranker
):
    """A unit maps to ONE chapter: a chapter that only caught a stray anchor
    (Force, 1 hit) is pruned in favour of the dominant one (Scientific Study, 2) —
    so the outline is that chapter's sections, not a cross-chapter mix."""
    r = _retriever(fake_embedder, fake_store, fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig(coverage_anchors=8))
    outline = gr.outline(query="variable newton force units", grade=10, subject="Science")
    assert outline.sections == [
        "Scientific Study > Variables",
        "Scientific Study > Fundamental Units",
    ]
    assert outline.chapters == ["Scientific Study"]
    assert "Force > Newton's Laws" not in outline.sections  # the stray chapter is pruned


_TWO_CHAPTER_BOOK = """\
# Optics

## Reflection

Light bounces off a surface.

## Refraction

Light bends passing between media.

# Sound

## Wavelength

Sound travels as a wave.

## Frequency

Pitch depends on frequency.
"""


def test_outline_keeps_two_co_dominant_chapters(fake_embedder, fake_store, fake_reranker):
    """A unit that genuinely spans two chapters (each with equal anchor weight) is
    NOT pruned — both chapters' sections are enumerated in full."""
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="g10phys", text=_TWO_CHAPTER_BOOK, source="Grade 10 Physics",
                  metadata={"grade": 10, "subject": "Science"})],
        chunker=MarkdownChunker(max_chars=200),
    )
    r = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig(coverage_anchors=8))
    outline = gr.outline(query="reflection sound wave", grade=10, subject="Science")
    assert set(outline.sections) == {
        "Optics > Reflection", "Optics > Refraction",
        "Sound > Wavelength", "Sound > Frequency",
    }


def test_outline_is_empty_for_a_flat_source_without_hierarchy(
    fake_embedder, fake_store, fake_reranker
):
    from lessonforge.rag.chunkers import ParagraphChunker

    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.curriculum,
        [Document(id="c1", text="Students learn about variables and fair tests.",
                  source="CDC", metadata={"grade": 10, "subject": "Science"})],
        chunker=ParagraphChunker(),
    )
    r = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig())
    outline = gr.outline(
        query="variables", grade=10, subject="Science", collection="curriculum"
    )
    assert outline.is_empty  # no chapter/heading metadata → nothing to enumerate


_NOISY_BOOK = """\
# Unit 12 : The Universe

## **12.1 Introduction to the Universe**

The universe is everything that exists.

## **12.5 Probable Future of the Universe**

### **Do You Know**

The fate of the universe depends on its density.

### **Big bang vs Big crunch**

Two possible ends of the universe.

## **12.6 Type of Universe**

### a. Open Universe

An open universe expands forever.

### b. Closed Universe

A closed universe eventually recollapses.
"""


def _noisy_grounder(fake_embedder, fake_store, fake_reranker, config=None):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="sci10", text=_NOISY_BOOK, source="SCIENCE_10.md",
                  metadata={"grade": 10, "subject": "Science"})],
        chunker=MarkdownChunker(max_chars=200),
    )
    r = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    return GroundingRetriever(r, config or GroundingConfig())


def test_outline_collapses_callouts_and_strips_markdown(
    fake_embedder, fake_store, fake_reranker
):
    """Real CDC headings carry Markdown bold and deep callout boxes. The outline
    must strip the ``**`` and collapse sub-headings into their numbered section, and
    a section that exists ONLY via sub-headings (12.5, 12.6 here) must survive."""
    gr = _noisy_grounder(fake_embedder, fake_store, fake_reranker)  # coverage_depth=1
    outline = gr.outline(query="universe", grade=10, subject="Science")
    assert outline.sections == [
        "Unit 12 : The Universe > 12.1 Introduction to the Universe",
        "Unit 12 : The Universe > 12.5 Probable Future of the Universe",
        "Unit 12 : The Universe > 12.6 Type of Universe",
    ]


def test_coverage_gaps_matches_across_markdown_and_numbering(
    fake_embedder, fake_store, fake_reranker
):
    """The false '16/16 missing' bug: numbering + bold made every section look
    uncovered. With token-overlap matching, a plan that reuses a section's title
    counts as covering it, and only the genuinely absent topic is reported."""
    from lessonforge.domain.ldd import CurriculumRef
    from lessonforge.domain.unit import DayPlan, UnitPlan
    from lessonforge.rag.grounding import coverage_gaps

    gr = _noisy_grounder(fake_embedder, fake_store, fake_reranker)
    outline = gr.outline(query="universe", grade=10, subject="Science")
    plan = UnitPlan(
        title="The Universe",
        curriculum_ref=CurriculumRef(grade=10, subject="Science"),
        days=[
            DayPlan(day=1, topic="Introduction to the Universe"),
            DayPlan(day=2, topic="Type of Universe", objective_seeds=["Open and closed"]),
        ],
    )
    # 12.1 and 12.6 are reused in the plan → covered; 12.5 is absent → the lone gap.
    assert coverage_gaps(outline.sections, plan) == [
        "Unit 12 : The Universe > 12.5 Probable Future of the Universe",
    ]


def test_coverage_gaps_flags_only_the_unreferenced_sections():
    from lessonforge.domain.ldd import CurriculumRef
    from lessonforge.domain.unit import DayPlan, UnitPlan
    from lessonforge.rag.grounding import coverage_gaps

    plan = UnitPlan(
        title="Scientific Study",
        curriculum_ref=CurriculumRef(grade=10, subject="Science"),
        big_idea="Science measures variables",
        days=[
            DayPlan(day=1, topic="Variables", objective_seeds=["Define variable"]),
            DayPlan(day=2, topic="Types of Variables", objective_seeds=["Classify"]),
        ],
    )
    sections = [
        "Scientific Study > Variables",
        "Scientific Study > Types of Variables",
        "Scientific Study > Fundamental Units",  # not in any day → a gap
    ]
    assert coverage_gaps(sections, plan) == ["Scientific Study > Fundamental Units"]


# ── grounding honors granularity only for authoritative collections ───────────
def test_grounding_expands_only_authoritative_collections(
    fake_embedder, fake_store, fake_reranker
):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="g10sci", text=_BOOK, source="Grade 10 Science",
                  metadata={"grade": 10, "subject": "Science"})],
        chunker=MarkdownChunker(max_chars=200),
    )
    # a flat pedagogical seed chunk that mentions the same words
    ing.ingest_documents(
        Collection.pedagogical,
        [Document(id="p1", text="Teach variables with a hands-on fair test.",
                  source="Pedagogy", metadata={"grade": 10, "subject": "Science"})],
    )
    r = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    gr = GroundingRetriever(r, GroundingConfig())  # reference authoritative by default

    bundle = gr.ground(query="variable fair test", grade=10, subject="Science", granularity="broad")
    # reference expanded to the chapter (has kilograms from the sibling section)
    ref_text = " ".join(c.text.lower() for c in bundle.chunks["reference"])
    assert "kilograms" in ref_text
    # pedagogical stayed narrow (its single chunk, unchanged)
    ped_text = " ".join(c.text for c in bundle.chunks["pedagogical"])
    assert ped_text == "Teach variables with a hands-on fair test."
