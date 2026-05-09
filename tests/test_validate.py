"""Tests for fanout.validate_plan."""
from __future__ import annotations

import copy
import json

import pytest

from fanout import PlanValidationError, validate_plan
from tests.conftest import load_plan


def test_validate_valid_extend(fixture_bundle):
    p = load_plan("valid_extend.json")
    validated = validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert validated["n"] == 2


def test_validate_n_mismatch(fixture_bundle):
    p = load_plan("invalid_n_mismatch.json")
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=4, mode="extend", bundle=fixture_bundle)
    msgs = "\n".join(exc.value.errors)
    assert "subtask count mismatch" in msgs or "n mismatch" in msgs


def test_validate_mode_mismatch(fixture_bundle):
    p = load_plan("valid_extend.json")
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="scratch", bundle=fixture_bundle)
    assert any("mode mismatch" in e for e in exc.value.errors)


def test_validate_bad_strategy(fixture_bundle):
    p = load_plan("invalid_bad_strategy.json")
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert any("strategy" in e for e in exc.value.errors)


def test_validate_bad_merge_plan(fixture_bundle):
    p = load_plan("valid_extend.json")
    p = copy.deepcopy(p)
    p["merge_plan"] = "shuffle"
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert any("merge_plan" in e for e in exc.value.errors)


def test_validate_missing_keys(fixture_bundle):
    p = load_plan("valid_extend.json")
    p = copy.deepcopy(p)
    del p["subtasks"][0]["read_files"]
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert any("read_files" in e for e in exc.value.errors)


def test_validate_duplicate_ids(fixture_bundle):
    p = load_plan("valid_extend.json")
    p = copy.deepcopy(p)
    p["subtasks"][1]["id"] = 1
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    msgs = "\n".join(exc.value.errors)
    assert "duplicate" in msgs or "1..2" in msgs


def test_validate_extend_outside_repo(fixture_bundle):
    p = load_plan("invalid_extend_outside_repo.json")
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    msgs = "\n".join(exc.value.errors)
    assert "src/auth/oauth.py" in msgs


def test_validate_greenfield_with_files():
    p = load_plan("invalid_greenfield_with_files.json")
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(
            p, "explore", n=2, mode="greenfield", bundle={"repo_map": "", "files": [], "refs": []}
        )
    assert any("greenfield" in e or "forbids read_files" in e for e in exc.value.errors)


def test_validate_byfile_overlap_violation(fixture_bundle):
    p = load_plan("invalid_overlap.json")
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert any("by_file overlap" in e for e in exc.value.errors)


def test_validate_byfile_one_overlap_passes(fixture_bundle):
    p = load_plan("valid_byfile_one_overlap.json")
    validated = validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert validated is p


def test_validate_dimension_full_overlap_passes(fixture_bundle):
    p = load_plan("valid_dimension_full_overlap.json")
    validated = validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert validated is p


def test_validate_aggregates_errors(fixture_bundle):
    """Multiple violations should all surface together."""
    p = load_plan("valid_extend.json")
    p = copy.deepcopy(p)
    p["merge_plan"] = "shuffle"
    p["strategy"] = "by_vibes"
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    msgs = "\n".join(exc.value.errors)
    assert "merge_plan" in msgs
    assert "strategy" in msgs


def test_validate_extend_subtask_must_have_read_files(fixture_bundle):
    p = load_plan("valid_extend.json")
    p = copy.deepcopy(p)
    p["subtasks"][0]["read_files"] = []
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert any("non-empty read_files" in e for e in exc.value.errors)


def test_validate_self_contained_advisory(fixture_bundle):
    p = load_plan("valid_extend.json")
    p = copy.deepcopy(p)
    p["subtasks"][0]["instructions"] = "Use the previous worker's output to refine."
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(p, "audit", n=2, mode="extend", bundle=fixture_bundle)
    assert any("cross-worker" in e for e in exc.value.errors)
