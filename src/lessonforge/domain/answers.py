"""Answer equivalence — ONE definition of "these two answers say the same thing".

Every place that compares a student's words with a key (the quiz/learn grader, the
answer-leak guard, the item-quality validator) goes through this module, so a fix
here fixes all of them, and they can never disagree ("3" graded wrong while the
guard treats "three" as the answer).

Production answer checkers work in tiers, cheapest and most certain first; this
module is the deterministic tiers. The model-judged tier lives in the grader
(``LLMGrader``), and only runs for what these tiers could not settle.

1. **canonical form** — Unicode NFKC (so ``NH₃`` → ``NH3``, full-width digits →
   ASCII), case-fold, Devanagari digits → ASCII, punctuation and leading articles
   dropped;
2. **numbers as values** — a number written as digits or as words ("three",
   "twenty-one", "तीन", "३") is the same number; compared with a relative tolerance
   and the unit ignored unless the student wrote a different one;
3. **listed alternatives** — ``"vibrates / vibrating"`` accepts either;
4. **typo tolerance** — a small edit on a long *word* answer is accepted; never on
   anything containing digits ("NH3" vs "NH4" are different answers, not typos).

Pure functions, no I/O — the test seam is the module itself.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# ── canonical text ───────────────────────────────────────────────────────────
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_ARTICLES = re.compile(r"^(?:a|an|the)\s+")
_PUNCT = re.compile(r"[^\w\s.%/+\u0900-\u0963\u0966-\u097F-]", re.UNICODE)   # keep Devanagari vowel signs, drop the danda
_SPACE = re.compile(r"\s+")


def canonical(s: object) -> str:
    """The comparable form of an answer: NFKC, case-folded, ASCII digits, no
    punctuation (a decimal point and %, /, +, - are kept), no leading article."""
    t = unicodedata.normalize("NFKC", str(s if s is not None else "")).translate(_DEVANAGARI_DIGITS)
    t = t.casefold().replace("’", "'")
    t = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", t)     # thousands separators: 1,200 / 1,00,000
    t = _PUNCT.sub(" ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)       # a point only inside a decimal
    t = _SPACE.sub(" ", t).strip()
    return _ARTICLES.sub("", t)


# ── numbers written as words ─────────────────────────────────────────────────
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
    # Nepali (Devanagari) — the medium of instruction for many of our students
    "शून्य": 0, "एक": 1, "दुई": 2, "तीन": 3, "तिन": 3, "चार": 4, "पाँच": 5, "पाच": 5,
    "छ": 6, "सात": 7, "आठ": 8, "नौ": 9, "दश": 10, "दस": 10, "एघार": 11, "बाह्र": 12,
    "तेह्र": 13, "चौध": 14, "पन्ध्र": 15, "सोह्र": 16, "सत्र": 17, "अठार": 18, "उन्नाइस": 19,
    "बीस": 20, "बिस": 20,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "million": 1_000_000,
           "सय": 100, "हजार": 1_000, "लाख": 100_000}
_UNITS = {unicodedata.normalize("NFKC", k): v for k, v in _UNITS.items()}
_SCALES = {unicodedata.normalize("NFKC", k): v for k, v in _SCALES.items()}
_NUM_WORDS = {**_UNITS, **_TENS, **_SCALES}
_WORD_HYPHEN = re.compile(r"(?<=[^\W\d])-(?=[^\W\d])")     # "twenty-one", not "-5"
_NUMBER = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?|[-+]?\.\d+")


def _words_value(tokens: list[str]) -> float | None:
    """"twenty one" / "two hundred" / "तीन" → the value; ``None`` if not a number."""
    total = current = 0
    seen = False
    for w in tokens:
        if w == "and" and seen:
            continue
        if w in _UNITS or w in _TENS:
            current += _UNITS.get(w, _TENS.get(w, 0))
        elif w in _SCALES:
            current = max(current, 1) * _SCALES[w]
            if _SCALES[w] >= 1000:
                total, current = total + current, 0
        else:
            return None
        seen = True
    return float(total + current) if seen else None


def numbers(s: object) -> list[float]:
    """Every number in ``s``, written as digits or as words, in order."""
    t = _WORD_HYPHEN.sub(" ", canonical(s))
    out: list[float] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            v = _words_value(run)
            if v is not None:
                out.append(v)
            run.clear()

    for tok in t.split():
        if tok in _NUM_WORDS or (tok == "and" and run):
            run.append(tok)
            continue
        flush()
        for m in _NUMBER.finditer(tok):
            if m.start() == 0 or not tok[m.start() - 1].isalpha():   # not the 3 in "nh3"
                try:
                    out.append(float(m.group().replace(",", "")))
                except ValueError:
                    pass
    flush()
    return out


def number_value(s: object) -> float | None:
    """The answer as ONE number, if it is just a number (with an optional unit)."""
    t = canonical(s)
    if not t:
        return None
    toks = _WORD_HYPHEN.sub(" ", t).split()
    if all(tok in _NUM_WORDS or tok == "and" for tok in toks):
        return _words_value(toks)
    m = _NUMBER.match(t)
    if m is None:
        return None
    rest = t[m.end():].strip()
    if rest and not re.fullmatch(r"[^\d\s]{1,12}(?:\s*/\s*[^\d\s]{1,8})?(?:\s+\S{1,10})?", rest):
        return None                  # "3 and 4", "3 x 4" are not one number
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def _unit(s: object) -> str:
    t = canonical(s)
    m = _NUMBER.match(t)
    return t[m.end():].strip() if m else ""


def same_number(a: float, b: float, *, rel: float = 0.01) -> bool:
    return abs(a - b) <= max(abs(b) * rel, 1e-9)


# ── alternatives ─────────────────────────────────────────────────────────────
_ALT = re.compile(r"\s*(?:/|\||;|\bor\b|\bवा\b)\s*")


def alternatives(answer: str) -> list[str]:
    """``"vibrates / vibrating"`` → both. A key that is one fraction-like number
    ("1/2", "km/h") is kept whole."""
    raw = str(answer or "").strip()
    if not raw:
        return []
    if re.fullmatch(r"[\d\s.,]+/[\d\s.,]+|\S*\d\S*\s*\w+/\w+", raw):   # 1/2, 30 km/h
        return [raw]
    parts = [p for p in _ALT.split(raw) if p.strip()]
    return parts or [raw]


# ── the comparison ───────────────────────────────────────────────────────────
def text_equivalent(response: object, answer: str, *, typo_ratio: float = 0.88) -> bool:
    """Does a typed short answer say the same as the key (any listed alternative)?"""
    r = canonical(response)
    if not r:
        return False
    rn = number_value(response)
    for alt in alternatives(answer):
        a = canonical(alt)
        if not a:
            continue
        if r == a:
            return True
        an = number_value(alt)
        if rn is not None and an is not None:       # "3" == "three" == "३" == "3.0"
            ru, au = _unit(response), _unit(alt)
            if same_number(rn, an) and (not ru or not au or ru == au):
                return True
            continue
        has_digit = any(ch.isdigit() for ch in r + a)
        if not has_digit and len(a) >= 5 and difflib.SequenceMatcher(None, r, a).ratio() >= typo_ratio:
            return True                              # a small typo shouldn't cost the mark
    return False


def numeric_equivalent(response: object, answer: str, *, rel: float = 0.01) -> bool:
    """A numerical answer: the value matters (digits or words), the unit only when
    the student wrote a different one."""
    want = number_value(answer)
    if want is None:
        nums = numbers(answer)
        want = nums[0] if len(nums) == 1 else None
    if want is None:
        return text_equivalent(response, answer)
    got = number_value(response)
    if got is None:
        nums = numbers(response)
        got = nums[-1] if nums else None          # "120 / 4 = 30 km/h" → the result
    if got is None or not same_number(got, want, rel=rel):
        return False
    ru, au = _unit(response), _unit(answer)
    return not ru or not au or ru == au or ru.replace(" ", "") == au.replace(" ", "")


_TRUE = {"true", "t", "yes", "y", "correct", "right", "सत्य", "सही", "ठीक", "हो"}
_FALSE = {"false", "f", "no", "n", "incorrect", "wrong", "असत्य", "गलत", "होइन"}


def truth_value(s: object) -> bool | None:
    t = canonical(s)
    return True if t in _TRUE else False if t in _FALSE else None


# ── "does this text contain the answer?" (leak guard, item validator) ─────────
def mentions(text: str, answer: str, *, strict: bool = False) -> bool:
    """Does ``text`` state ``answer`` (any alternative, digits or words)?

    ``strict=False`` (the runtime leak guard): a whole-word / whole-phrase match, and a
    small integer (< 10) only counts as written in words or as a standalone number —
    "3" in a reply is everywhere in prose, so the guard looks for the *phrase*.
    ``strict=True`` (authoring-time validation, where a false alarm only costs one
    regenerated item): any occurrence, including a digit inside a formula ("NH3"
    gives away "three")."""
    t = f" {canonical(text)} "
    for alt in alternatives(answer):
        a = canonical(alt)
        if not a:
            continue
        v = number_value(alt)
        if v is not None:
            found = numbers(text)
            if strict:
                found += [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
            if v.is_integer() and abs(v) < 10 and not strict:
                # small numbers: only the exact phrase (e.g. "three" for key "three")
                if len(a) >= 3 and not a.isdigit() and f" {a} " in t:
                    return True
                continue
            if any(same_number(x, v) for x in found):
                return True
            continue
        if len(a) >= 3 and f" {a} " in t:
            return True
    return False
