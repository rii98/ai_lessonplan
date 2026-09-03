"""Inline rich-text for the Office exporters.

Generated field text (objectives, activities, questions…) can carry the same
lightweight markup the chat surface renders: ``**bold**``, ``*italic*``,
``` `code` ``` and LaTeX math (``$x^2$``, ``$$\\frac{a}{b}$$``, ``\\(..\\)``,
``\\[..\\]``). A browser renders that with KaTeX + Markdown; a .docx / .pptx run
cannot, so without help the delimiters leak into the document as literal
``$``/``**`` characters (exactly the "weird symbols" problem, one layer down).

:func:`parse_inline` turns a string into styled :class:`Segment` runs the DOCX
and PPTX renderers apply with their own font machinery. Math has no native
equivalent we can rely on across Word/PowerPoint versions, so we convert LaTeX
to readable Unicode (``x^2`` → ``x²``, ``\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}`` →
``(-b ± √(b² - 4ac))/(2a)``). It is a pragmatic transliteration, not a full TeX
engine: anything it does not recognise degrades to cleaned-up source rather than
raw backslashes, so the output is always legible.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass


@dataclass
class Segment:
    """A run of text with the inline styles that apply to it.

    ``block`` marks a segment that must stand on its own line(s) — a fenced code
    block — so renderers give it a paragraph rather than flowing it inline. Its
    ``text`` may contain newlines, which renderers turn into real line breaks.
    """

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    block: bool = False


# ── LaTeX → Unicode ──────────────────────────────────────────────────────────

# Symbol commands (longest first so \leq wins over \le's prefix, etc.).
_SYMBOLS: dict[str, str] = {
    r"\pm": "±", r"\mp": "∓", r"\times": "×", r"\div": "÷", r"\cdot": "·",
    r"\ast": "∗", r"\star": "⋆", r"\leq": "≤", r"\le": "≤", r"\geq": "≥",
    r"\ge": "≥", r"\neq": "≠", r"\ne": "≠", r"\equiv": "≡", r"\approx": "≈",
    r"\sim": "∼", r"\propto": "∝", r"\infty": "∞", r"\partial": "∂",
    r"\nabla": "∇", r"\sum": "∑", r"\prod": "∏", r"\int": "∫",
    r"\Rightarrow": "⇒", r"\Leftarrow": "⇐", r"\Leftrightarrow": "⇔",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\leftrightarrow": "↔",
    r"\to": "→", r"\mapsto": "↦", r"\ldots": "…", r"\cdots": "⋯",
    r"\dots": "…", r"\deg": "°", r"\circ": "∘", r"\angle": "∠",
    r"\perp": "⊥", r"\parallel": "∥", r"\in": "∈", r"\notin": "∉",
    r"\subset": "⊂", r"\subseteq": "⊆", r"\supset": "⊃", r"\cup": "∪",
    r"\cap": "∩", r"\emptyset": "∅", r"\forall": "∀", r"\exists": "∃",
    r"\land": "∧", r"\lor": "∨", r"\neg": "¬", r"\therefore": "∴",
    r"\because": "∵", r"\prime": "′", r"\pm ": "± ", r"\%": "%", r"\$": "$",
    r"\&": "&", r"\#": "#", r"\_": "_", r"\{": "{", r"\}": "}",
    # Greek
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "ϑ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π",
    r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ",
    r"\phi": "φ", r"\varphi": "φ", r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ", r"\Psi": "Ψ",
    r"\Omega": "Ω",
}
# Spacing / no-op commands stripped to a single space or nothing.
_SPACING = {r"\quad": "  ", r"\qquad": "    ", r"\,": " ", r"\;": " ",
            r"\:": " ", r"\!": "", r"\ ": " ", r"\left": "", r"\right": ""}

_SUP = str.maketrans("0123456789+-=()n i.", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ ⁱ˙")
_SUB = str.maketrans("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")


def _to_script(body: str, table: dict[int, int], fallback_prefix: str) -> str:
    """Superscript/subscript ``body`` if every char has a Unicode form, else
    fall back to a legible ``^(…)`` / ``_(…)`` spelling."""
    body = latex_to_unicode(body)
    mapped = body.translate(table)
    if all(ord(c) in table or c == " " for c in body):
        return mapped
    return f"{fallback_prefix}({body})" if len(body) > 1 else f"{fallback_prefix}{body}"


def _take_group(s: str, i: int) -> tuple[str, int]:
    """Read a ``{...}`` group (or the single next char) starting at ``s[i]``.
    Returns the inner text and the index just past it."""
    if i < len(s) and s[i] == "{":
        depth, j = 0, i
        while j < len(s):
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    return s[i + 1:j], j + 1
            j += 1
        return s[i + 1:], len(s)
    if i < len(s):
        return s[i], i + 1
    return "", i


_CMD = re.compile(r"\\[A-Za-z]+|\\.")


def latex_to_unicode(tex: str) -> str:
    """Best-effort transliteration of a LaTeX math fragment to Unicode text."""
    if not tex:
        return ""
    s = tex
    # \frac{a}{b} and \sqrt[n]{x} handled first (they consume grouped args).
    s = _expand_fracs(s)
    s = _expand_sqrts(s)
    s = _expand_text(s)
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "^" or ch == "_":
            body, j = _take_group(s, i + 1)
            out.append(_to_script(body, _SUP if ch == "^" else _SUB, ch))
            i = j
            continue
        if ch == "\\":
            m = _CMD.match(s, i)
            if m:
                tok = m.group(0)
                if tok in _SPACING:
                    out.append(_SPACING[tok])
                elif tok in _SYMBOLS:
                    out.append(_SYMBOLS[tok])
                else:
                    out.append(tok[1:])  # unknown command: drop the backslash
                i = m.end()
                continue
        if ch in "{}":
            i += 1
            continue
        out.append(ch)
        i += 1
    # collapse the runs of spaces our stripping can introduce
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def _expand_fracs(s: str) -> str:
    while (k := s.find(r"\frac")) != -1:
        num, j = _take_group(s, k + 5)
        den, j = _take_group(s, j)
        num_u, den_u = latex_to_unicode(num), latex_to_unicode(den)
        num_s = f"({num_u})" if _needs_parens(num_u) else num_u
        den_s = f"({den_u})" if _needs_parens(den_u) else den_u
        s = s[:k] + f"{num_s}/{den_s}" + s[j:]
    return s


def _expand_sqrts(s: str) -> str:
    while (k := s.find(r"\sqrt")) != -1:
        j = k + 5
        root = ""
        if j < len(s) and s[j] == "[":
            end = s.find("]", j)
            if end != -1:
                root = _to_script(s[j + 1:end], _SUP, "^")
                j = end + 1
        body, j = _take_group(s, j)
        s = s[:k] + f"{root}√({latex_to_unicode(body)})" + s[j:]
    return s


def _expand_text(s: str) -> str:
    for cmd in (r"\text", r"\mathrm", r"\mathbf", r"\mathit", r"\operatorname"):
        while (k := s.find(cmd)) != -1:
            body, j = _take_group(s, k + len(cmd))
            s = s[:k] + body + s[j:]
    return s


def _needs_parens(u: str) -> bool:
    """A fraction part needs wrapping unless it is a single token."""
    return bool(re.search(r"[+\-−±/ ]", u.strip())) or len(u.strip()) > 3


# ── HTML → Markdown ──────────────────────────────────────────────────────────

# The LLM is asked for Markdown, but may emit HTML. There is no faithful .docx /
# .pptx equivalent for arbitrary HTML, so we fold the common inline formatting
# tags onto their Markdown spelling, turn <br> into a newline, drop every other
# tag (keeping its text), then decode entities. Block structure (tables, lists)
# is lost rather than dumped as angle-bracket garbage.
_TAG_TO_MD = {
    "b": "**", "strong": "**", "i": "*", "em": "*",
    "code": "`", "tt": "`", "kbd": "`", "samp": "`",
}
_HTML_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6])>", re.IGNORECASE)


def _looks_like_html(s: str) -> bool:
    return "<" in s and bool(_HTML_TAG.search(s)) or "&" in s


def _html_to_markdown(s: str) -> str:
    if not _looks_like_html(s):
        return s
    s = _BR.sub("\n", s)
    s = _BLOCK_END.sub("\n", s)           # close of a block element → line break
    s = _HTML_TAG.sub(lambda m: _TAG_TO_MD.get(m.group(1).lower(), ""), s)
    s = _html.unescape(s)                 # &lt; &amp; &#39; … → characters
    return re.sub(r"\n{3,}", "\n\n", s).strip("\n")


# ── fenced code blocks ───────────────────────────────────────────────────────

# ```lang\n … ``` → one block segment; the language tag and fences are dropped
# so they never leak, and the body keeps its newlines for real line breaks.
_FENCE = re.compile(r"```[ \t]*[\w+.-]*[ \t]*\r?\n?(.*?)```", re.DOTALL)


# ── inline Markdown ──────────────────────────────────────────────────────────

_MATH_PATTERNS = (
    re.compile(r"\$\$(.+?)\$\$", re.DOTALL),          # $$ … $$
    re.compile(r"\\\[(.+?)\\\]", re.DOTALL),          # \[ … \]
    re.compile(r"\\\((.+?)\\\)", re.DOTALL),          # \( … \)
    re.compile(r"\$(?!\s)(.+?)(?<!\s)\$", re.DOTALL),  # $ … $  (tight, skips "$5")
)
_O, _C = chr(0xE000), chr(0xE001)  # private-use sentinels for stashed spans
_PLACEHOLDER = re.compile(_O + r"([mc])(\d+)" + _C)
# code, then **bold**, then *italic* (italic tight so "a * b" is left alone)
_INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\*(?!\s)([^*]+?)(?<!\s)\*", re.DOTALL)


def _parse_emphasis(s: str) -> list[Segment]:
    segs: list[Segment] = []
    pos = 0
    for m in _INLINE.finditer(s):
        if m.start() > pos:
            segs.append(Segment(s[pos:m.start()]))
        if m.group(1) is not None:
            segs.append(Segment(m.group(1), code=True))
        elif m.group(2) is not None:
            segs.append(Segment(m.group(2), bold=True))
        else:
            segs.append(Segment(m.group(3), italic=True))
        pos = m.end()
    if pos < len(s):
        segs.append(Segment(s[pos:]))
    return segs


def parse_inline(text: str) -> list[Segment]:
    """Split ``text`` into styled segments.

    Handles, in a protect-first order (so each stage never mangles the next):
    fenced code blocks, LaTeX math (→ Unicode), inline HTML (→ Markdown +
    decoded entities), then Markdown emphasis / inline code. Fenced blocks and
    math are stashed behind sentinels before any Markdown or HTML rewriting runs,
    the same discipline the web renderer uses. Returns ``[]`` only for empty
    input.
    """
    if not text:
        return []
    code_blocks: list[str] = []
    math: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        code_blocks.append(m.group(1).rstrip("\n"))
        return f"{_O}c{len(code_blocks) - 1}{_C}"

    def _stash_math(m: re.Match[str]) -> str:
        math.append(latex_to_unicode(m.group(1)))
        return f"{_O}m{len(math) - 1}{_C}"

    protected = _FENCE.sub(_stash_code, text)   # fenced code first (literal)
    for pat in _MATH_PATTERNS:                  # then math (before HTML/Markdown)
        protected = pat.sub(_stash_math, protected)
    protected = _html_to_markdown(protected)    # HTML → Markdown + entities

    out: list[Segment] = []
    for seg in _parse_emphasis(protected):
        parts = _PLACEHOLDER.split(seg.text)
        # split() yields: text, kind, index, text, kind, index, … so step by 3
        for idx in range(0, len(parts), 3):
            plain = parts[idx]
            if plain:
                out.append(Segment(plain, seg.bold, seg.italic, seg.code))
            if idx + 2 < len(parts):
                kind, num = parts[idx + 1], int(parts[idx + 2])
                if kind == "m":  # math run — italic, math convention
                    out.append(Segment(math[num], seg.bold, True, False))
                else:            # fenced code block — its own monospace lines
                    out.append(Segment(code_blocks[num], code=True, block=True))
    return out or [Segment(text)]
