"""Pytest fixtures shared across the fanout test suite."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

FIXTURE_REPO = pathlib.Path(__file__).parent / "fixtures" / "sample_repo"
SAMPLE_PLANS = pathlib.Path(__file__).parent / "sample_plans"


def _ensure_prune_sentinels() -> None:
    """Create .git/HEAD inside the fixture repo at test time.

    Cannot be checked into the outer git repo (nested .git is a gitlink boundary),
    so we materialise it on-demand for the prune tests.
    """
    git_head = FIXTURE_REPO / ".git" / "HEAD"
    if not git_head.exists():
        git_head.parent.mkdir(parents=True, exist_ok=True)
        git_head.write_text("ref: refs/heads/main\n")


_ensure_prune_sentinels()


@pytest.fixture
def fixture_repo() -> pathlib.Path:
    return FIXTURE_REPO


@pytest.fixture
def fixture_bundle():
    """Bundle built against the static sample_repo with src/auth/* + src/db/*."""
    from context import build_bundle

    return build_bundle(
        repo=str(FIXTURE_REPO),
        files=["src/auth/*.py", "src/db/*.py"],
        refs=[],
    )


def load_plan(name: str) -> dict:
    return json.loads((SAMPLE_PLANS / name).read_text())
