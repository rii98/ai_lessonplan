"""Lenient JSON extraction — real models wrap JSON in fences/prose."""

from __future__ import annotations

import pytest

from lessonforge.util import extract_json


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_json_code_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_bare_fence():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_with_surrounding_prose():
    text = 'Here is the lesson:\n{"a": 1, "b": [2, 3]}\nHope that helps!'
    assert extract_json(text) == {"a": 1, "b": [2, 3]}


def test_unparseable_raises():
    with pytest.raises(ValueError, match="could not extract JSON"):
        extract_json("no json here at all")
