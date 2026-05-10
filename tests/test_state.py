"""Tests for state.py — state file IO, snapshots, drift."""
from __future__ import annotations

import json

import pytest

from state import (
    DriftReport,
    State,
    drift,
    latest_snapshot,
    load,
    load_snapshot,
    save,
    snapshot,
    state_path,
)


def test_load_when_no_state_file_returns_empty(tmp_path):
    s = load(tmp_path)
    assert s.version == 1
    assert s.last_apply is None
    assert s.owned["brew"] == []
    assert s.snapshots == []


def test_save_and_reload_round_trip(tmp_path):
    s = State()
    s.add_owned("brew", ["git", "jq"])
    s.add_owned("cask", ["docker"])
    p = save(s, tmp_path)
    assert p.exists()
    s2 = load(tmp_path)
    assert s2.owned["brew"] == ["git", "jq"]
    assert s2.owned["cask"] == ["docker"]
    assert s2.last_apply is not None  # save() stamps it


def test_save_writes_to_state_json(tmp_path):
    s = State()
    s.add_owned("brew", ["jq"])
    save(s, tmp_path)
    raw = json.loads(state_path(tmp_path).read_text())
    assert raw["owned"]["brew"] == ["jq"]
    assert raw["version"] == 1


def test_add_owned_dedupes():
    s = State()
    s.add_owned("brew", ["git", "jq"])
    s.add_owned("brew", ["jq", "ripgrep"])
    assert s.owned["brew"] == ["git", "jq", "ripgrep"]


def test_remove_owned():
    s = State()
    s.add_owned("brew", ["git", "jq", "ripgrep"])
    s.remove_owned("brew", ["jq"])
    assert s.owned["brew"] == ["git", "ripgrep"]


def test_remove_owned_nonexistent_bucket_is_noop():
    s = State()
    s.remove_owned("ghost", ["x"])  # should not raise


def test_snapshot_persists_owned(tmp_path):
    s = State()
    s.add_owned("brew", ["git", "jq"])
    s.add_owned("cask", ["docker"])
    meta = snapshot(s, tmp_path, label="before-apply")
    assert meta["label"] == "before-apply"
    assert (tmp_path / "snapshots").exists()
    payload = load_snapshot(meta)
    assert payload["owned"]["brew"] == ["git", "jq"]
    assert payload["owned"]["cask"] == ["docker"]


def test_latest_snapshot_returns_most_recent(tmp_path):
    s = State()
    assert latest_snapshot(s) is None
    snapshot(s, tmp_path, label="first")
    snapshot(s, tmp_path, label="second")
    last = latest_snapshot(s)
    assert last is not None
    assert last["label"] == "second"


def test_drift_no_drift_returns_empty():
    s = State()
    s.add_owned("brew", ["git"])
    actual = {"brew": {"git"}, "cask": set(), "npm_global": set(), "pip": set(), "curl": set()}
    assert drift(s, actual) == []


def test_drift_detects_added():
    s = State()
    s.add_owned("brew", ["git"])
    actual = {"brew": {"git", "fzf"}, "cask": set(), "npm_global": set(), "pip": set(), "curl": set()}
    reports = drift(s, actual)
    assert len(reports) == 1
    assert reports[0].bucket == "brew"
    assert reports[0].added == ["fzf"]
    assert reports[0].removed == []


def test_drift_detects_removed():
    s = State()
    s.add_owned("brew", ["git", "jq"])
    actual = {"brew": {"git"}, "cask": set(), "npm_global": set(), "pip": set(), "curl": set()}
    reports = drift(s, actual)
    assert len(reports) == 1
    assert reports[0].removed == ["jq"]


def test_drift_multi_bucket():
    s = State()
    s.add_owned("brew", ["git"])
    s.add_owned("cask", ["docker"])
    actual = {
        "brew": {"git", "fzf"},
        "cask": set(),
        "npm_global": {"yarn"},
        "pip": set(),
        "curl": set(),
    }
    reports = {r.bucket: r for r in drift(s, actual)}
    assert reports["brew"].added == ["fzf"]
    assert reports["cask"].removed == ["docker"]
    assert reports["npm_global"].added == ["yarn"]
