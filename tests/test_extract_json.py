"""Tests for fanout.extract_json — robust JSON extraction from messy LLM output."""
from __future__ import annotations

import pytest

from fanout import PlanParseError, extract_json
from workers import parse_claude_envelope


def test_extract_pure_json():
    assert extract_json('{"n": 4}') == {"n": 4}


def test_extract_with_fence():
    assert extract_json('```json\n{"n": 4}\n```') == {"n": 4}


def test_extract_with_fence_no_lang():
    assert extract_json('```\n{"n": 4}\n```') == {"n": 4}


def test_extract_with_preamble():
    assert extract_json('Here is the plan:\n{"n": 4}') == {"n": 4}


def test_extract_with_trailing_text():
    assert extract_json('{"n": 4}\nThat\'s the plan!') == {"n": 4}


def test_extract_nested_braces():
    s = '{"a": {"b": [1, 2, {"c": 3}]}}'
    assert extract_json(s) == {"a": {"b": [1, 2, {"c": 3}]}}


def test_extract_string_with_brace():
    """The brace inside a string must not confuse the depth counter."""
    s = '{"msg": "hello } world", "n": 2}'
    assert extract_json(s) == {"msg": "hello } world", "n": 2}


def test_extract_string_with_escaped_quote():
    s = '{"msg": "she said \\"hi\\"", "n": 1}'
    assert extract_json(s) == {"msg": 'she said "hi"', "n": 1}


def test_extract_array_at_top():
    assert extract_json('[1, 2, 3]') == [1, 2, 3]


def test_extract_invalid_raises():
    with pytest.raises(PlanParseError):
        extract_json("no json here at all")


def test_extract_empty_raises():
    with pytest.raises(PlanParseError):
        extract_json("")


def test_envelope_unwraps_result():
    raw = '{"type": "result", "result": "{\\"n\\": 4}", "duration_ms": 100}'
    inner = parse_claude_envelope(raw)
    assert extract_json(inner) == {"n": 4}


def test_envelope_passthrough_when_not_envelope():
    raw = '{"n": 4}'
    assert parse_claude_envelope(raw) == raw
