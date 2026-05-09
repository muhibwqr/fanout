"""Tests for context.py: bundle builder, repo_map fallback, file_digest."""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from context import (
    MAX_FILE_BYTES,
    _fallback_tree,
    build_bundle,
    file_digest,
    repo_map,
)
from tests.conftest import FIXTURE_REPO


def test_fallback_tree_basic():
    out = _fallback_tree(str(FIXTURE_REPO))
    assert "middleware.py" in out
    assert "README.md" in out


def test_fallback_tree_prunes_dotgit():
    out = _fallback_tree(str(FIXTURE_REPO))
    assert ".git" not in out
    assert "HEAD" not in out


def test_fallback_tree_prunes_node_modules():
    out = _fallback_tree(str(FIXTURE_REPO))
    assert "node_modules" not in out


def test_fallback_tree_respects_depth():
    shallow = _fallback_tree(str(FIXTURE_REPO), depth=1)
    deep = _fallback_tree(str(FIXTURE_REPO), depth=3)
    assert "src" in shallow or "src/" in shallow
    assert "middleware.py" not in shallow
    assert "middleware.py" in deep


def test_fallback_tree_max_entries(tmp_path):
    # 20 dirs * 30 files = 600 entries; cap to 50.
    for i in range(20):
        d = tmp_path / f"d{i:02d}"
        d.mkdir()
        for j in range(30):
            (d / f"f{j:02d}.txt").write_text("x")
    out = _fallback_tree(str(tmp_path), depth=3, max_entries=50)
    assert "[truncated" in out
    assert out.count("\n") < 60  # 50 entries + truncation marker


def test_repo_map_uses_fallback_when_no_tree(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no tree")

    monkeypatch.setattr(subprocess, "run", boom)
    out = repo_map(str(FIXTURE_REPO))
    assert "middleware.py" in out


def test_repo_map_handles_tree_nonzero(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "err"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    out = repo_map(str(FIXTURE_REPO))
    # Falls back rather than raising.
    assert "middleware.py" in out


def test_file_digest_basic():
    d = file_digest("src/auth/middleware.py", repo=str(FIXTURE_REPO))
    assert any("authenticate" in s for s in d["symbols"])
    assert any("AuthMiddleware" in s for s in d["symbols"])
    assert "verify_token" in d["excerpt"]


def test_file_digest_truncation(tmp_path):
    big = tmp_path / "big.py"
    big.write_text("\n".join(f"line_{i}" for i in range(600)))
    d = file_digest(str(big))
    assert "[TRUNCATED]" in d["excerpt"]


def test_file_digest_too_large(tmp_path):
    huge = tmp_path / "huge.py"
    huge.write_text("a" * (MAX_FILE_BYTES + 1))
    d = file_digest(str(huge))
    assert "FILE TOO LARGE" in d["excerpt"]
    assert d["symbols"] == []


def test_file_digest_repo_relative_path():
    d = file_digest("src/auth/middleware.py", repo=str(FIXTURE_REPO))
    assert d["path"] == "src/auth/middleware.py"
    assert d["abs_path"].endswith("src/auth/middleware.py")


def test_file_digest_missing_file():
    d = file_digest("does/not/exist.py", repo=str(FIXTURE_REPO))
    assert d["excerpt"] == "[FILE NOT FOUND]"
    assert d["symbols"] == []


def test_build_bundle_extend():
    b = build_bundle(
        repo=str(FIXTURE_REPO),
        files=["src/auth/*.py"],
        refs=["https://example.com"],
    )
    assert b["repo_map"]
    paths = sorted(d["path"] for d in b["files"])
    assert paths == ["src/auth/middleware.py", "src/auth/tokens.py"]
    assert b["refs"] == ["https://example.com"]


def test_build_bundle_glob_relative_to_repo(tmp_path, monkeypatch):
    # Regression: invoking from elsewhere must still find files via repo-relative globs.
    monkeypatch.chdir(tmp_path)
    b = build_bundle(repo=str(FIXTURE_REPO), files=["src/auth/*.py"], refs=[])
    assert len(b["files"]) == 2


def test_build_bundle_no_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    (tmp_path / "b.py").write_text("class C:\n    pass\n")
    b = build_bundle(repo=None, files=["*.py"], refs=[])
    assert b["repo_map"] == ""
    assert sorted(d["path"] for d in b["files"]) == ["a.py", "b.py"]


def test_build_bundle_budget_truncation(tmp_path):
    big1 = tmp_path / "big1.py"
    big2 = tmp_path / "big2.py"
    big1.write_text("def f():\n" + "    x=1\n" * 5000)
    big2.write_text("def g():\n" + "    y=2\n" * 5000)
    b = build_bundle(repo=str(tmp_path), files=["*.py"], refs=[], budget=1000)
    for d in b["files"]:
        assert "[BUDGET TRUNCATED]" in d["excerpt"]


def test_build_bundle_refs_passthrough():
    b = build_bundle(repo=None, files=[], refs=["a", "b", "c"])
    assert b["refs"] == ["a", "b", "c"]
