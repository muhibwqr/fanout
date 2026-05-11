"""Tests for fanout.py — argparse, repo packing, JSON extractor."""
from __future__ import annotations

import json

import pytest

import fanout


# ---------- argparse: claude ----------


def test_parse_claude_single_prompt():
    a = fanout.parse_args(["claude", "2", "build a backend"])
    assert a.cmd == "claude"
    assert a.n == 2
    assert a.prompts == ["build a backend"]


def test_parse_claude_multiple_prompts():
    a = fanout.parse_args(["claude", "3", "task a", "task b", "task c"])
    assert a.n == 3
    assert a.prompts == ["task a", "task b", "task c"]


def test_parse_claude_with_repo_and_no_tmux():
    a = fanout.parse_args(["claude", "2", "p", "--repo", "/x", "--no-tmux"])
    assert a.repo == "/x"
    assert a.no_tmux is True


def test_parse_claude_requires_n_and_prompt():
    with pytest.raises(SystemExit):
        fanout.parse_args(["claude"])
    with pytest.raises(SystemExit):
        fanout.parse_args(["claude", "2"])


# ---------- argparse: plan ----------


def test_parse_plan_basic():
    a = fanout.parse_args(["plan", "build a backend for a typing test app"])
    assert a.cmd == "plan"
    assert a.task == "build a backend for a typing test app"
    assert a.n_hint is None


def test_parse_plan_with_hint_and_dry_run():
    a = fanout.parse_args(["plan", "audit auth", "-n", "4", "--dry-run"])
    assert a.n_hint == 4
    assert a.dry_run is True


def test_parse_plan_with_repo():
    a = fanout.parse_args(["plan", "review this", "--repo", "/tmp/my-repo"])
    assert a.repo == "/tmp/my-repo"


# ---------- repo packing ----------


def test_pack_repo_basic(tmp_path):
    (tmp_path / "main.py").write_text("def hello():\n    return 'hi'\n")
    (tmp_path / "README.md").write_text("# Hi\n")
    out = fanout.pack_repo(str(tmp_path))
    assert "main.py" in out
    assert "README.md" in out
    assert "def hello" in out


def test_pack_repo_skips_pruned_dirs(tmp_path):
    (tmp_path / "src.py").write_text("x = 1\n")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "lib.js").write_text("module.exports={}\n")
    out = fanout.pack_repo(str(tmp_path))
    assert "src.py" in out
    assert ".git" not in out
    assert "node_modules" not in out
    assert "HEAD" not in out


def test_pack_repo_skips_binary_suffixes(tmp_path):
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "code.py").write_text("print('ok')\n")
    out = fanout.pack_repo(str(tmp_path))
    assert "logo.png" not in out
    assert "code.py" in out


def test_pack_repo_skips_oversized_files(tmp_path):
    huge = tmp_path / "huge.txt"
    huge.write_text("x" * 200_000)  # > _MAX_FILE_BYTES
    small = tmp_path / "small.txt"
    small.write_text("ok\n")
    out = fanout.pack_repo(str(tmp_path))
    assert "huge.txt" not in out
    assert "small.txt" in out


def test_pack_repo_truncates_at_total_budget(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i:02d}.txt").write_text("y" * 50_000)
    out = fanout.pack_repo(str(tmp_path))
    assert "[TRUNCATED" in out


def test_pack_repo_single_file(tmp_path):
    f = tmp_path / "lone.py"
    f.write_text("hello\n")
    out = fanout.pack_repo(str(f))
    assert "lone.py" in out
    assert "hello" in out


def test_pack_repo_raises_on_missing_path():
    with pytest.raises(FileNotFoundError):
        fanout.pack_repo("/does/not/exist/at/all/xyz123")


# ---------- context preamble ----------


def test_build_preamble_repo_only(tmp_path):
    (tmp_path / "a.py").write_text("ok\n")
    pre = fanout.build_context_preamble(str(tmp_path), paste=False)
    assert "<repo" in pre
    assert "a.py" in pre


def test_build_preamble_none():
    pre = fanout.build_context_preamble(None, paste=False)
    assert pre == ""


def test_wrap_with_context_no_preamble():
    out = fanout.wrap_with_context("do the thing", "")
    assert out == "do the thing"


def test_wrap_with_context_adds_task_tag():
    out = fanout.wrap_with_context("do the thing", "<repo>x</repo>\n")
    assert "<task>" in out
    assert "do the thing" in out
    assert "<repo>x</repo>" in out


# ---------- JSON extractor ----------


def test_extract_json_pure():
    assert fanout._extract_json('{"n": 2}') == {"n": 2}


def test_extract_json_with_fence():
    assert fanout._extract_json('```json\n{"n": 2}\n```') == {"n": 2}


def test_extract_json_with_preamble():
    assert fanout._extract_json('Here you go:\n{"n": 2, "tasks": ["a", "b"]}') == {
        "n": 2,
        "tasks": ["a", "b"],
    }


def test_extract_json_nested():
    payload = {"n": 1, "tasks": ["a"], "rationale": "single agent"}
    assert fanout._extract_json(json.dumps(payload)) == payload


def test_extract_json_string_with_brace():
    s = '{"task": "use { and } in code"}'
    assert fanout._extract_json(s) == {"task": "use { and } in code"}


def test_extract_json_empty_raises():
    with pytest.raises(ValueError):
        fanout._extract_json("")


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        fanout._extract_json("not json at all")
