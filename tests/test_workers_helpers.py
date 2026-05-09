"""Tests for v1 helpers: worker prompt build, tmux availability, envelope edge cases."""
from __future__ import annotations

import json
import shutil

from fanout import _build_worker_prompt
from workers import parse_claude_envelope, tmux_available


def test_build_worker_prompt_scopes_files_and_refs():
    bundle = {
        "files": [
            {"path": "src/a.py", "abs_path": "/x/src/a.py", "symbols": [], "excerpt": "a"},
            {"path": "src/b.py", "abs_path": "/x/src/b.py", "symbols": [], "excerpt": "b"},
            {"path": "src/c.py", "abs_path": "/x/src/c.py", "symbols": [], "excerpt": "c"},
        ],
        "refs": ["https://example.com", "https://other.com"],
    }
    sub = {
        "id": 1,
        "title": "t",
        "instructions": "do",
        "read_files": ["src/a.py", "src/c.py"],
        "refs": ["https://example.com"],
        "expected_output": "md",
    }
    out = _build_worker_prompt(sub, bundle)
    parsed = json.loads(out)
    assert parsed["task"]["id"] == 1
    paths = sorted(f["path"] for f in parsed["context"]["files"])
    assert paths == ["src/a.py", "src/c.py"]  # b.py excluded
    assert parsed["context"]["refs"] == ["https://example.com"]


def test_build_worker_prompt_empty_read_files():
    bundle = {"files": [], "refs": []}
    sub = {
        "id": 1,
        "title": "t",
        "instructions": "do",
        "read_files": [],
        "refs": [],
        "expected_output": "md",
    }
    out = _build_worker_prompt(sub, bundle)
    parsed = json.loads(out)
    assert parsed["context"]["files"] == []
    assert parsed["context"]["refs"] == []


def test_tmux_available_matches_path():
    assert tmux_available() == (shutil.which("tmux") is not None)


def test_envelope_handles_non_json():
    assert parse_claude_envelope("hello world") == "hello world"
    assert parse_claude_envelope("") == ""


def test_envelope_handles_envelope_without_result():
    raw = '{"type": "result", "duration_ms": 100}'
    # No `result` field — passthrough.
    assert parse_claude_envelope(raw) == raw


def test_envelope_handles_non_string_result():
    raw = '{"type": "result", "result": 42}'
    # Non-string result — passthrough rather than coercing.
    assert parse_claude_envelope(raw) == raw
