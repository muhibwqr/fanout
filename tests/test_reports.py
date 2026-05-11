"""Tests for reports.py — each renderer returns a valid self-contained HTML doc."""
from __future__ import annotations

import pathlib

import pytest

import reports
from engine import ApplyResult, PlanResult, VerifyResult
from manifest import CurlItem
from state import DriftReport, State, snapshot


# ---------- shell sanity ----------


def test_shell_wraps_with_doctype():
    out = reports._shell("title", "<p>hi</p>")
    assert out.startswith("<!doctype html>")
    assert "<title>title</title>" in out
    assert "<p>hi</p>" in out
    assert "fanout report" in out
    assert "</html>" in out.rstrip().splitlines()[-1] or "</html>" in out


def test_shell_escapes_title():
    out = reports._shell("<dangerous>", "")
    assert "<dangerous>" not in out
    assert "&lt;dangerous&gt;" in out


# ---------- plan ----------


def test_render_plan_nothing_to_do():
    p = PlanResult(profile="default", bucket_diffs={
        b: {"install": [], "remove": [], "unchanged": []}
        for b in ("brew", "cask", "npm_global", "pip", "curl")
    }, nothing_to_do=True)
    out = reports.render_plan("default", p)
    assert "Nothing to do" in out
    assert "default" in out


def test_render_plan_with_diffs():
    p = PlanResult(
        profile="web",
        bucket_diffs={
            "brew": {"install": ["node"], "remove": ["bun"], "unchanged": ["git"]},
            "cask": {"install": ["docker"], "remove": [], "unchanged": []},
            "npm_global": {"install": [], "remove": [], "unchanged": []},
            "pip": {"install": [], "remove": [], "unchanged": []},
            "curl": {"install": [], "remove": [], "unchanged": []},
        },
        nothing_to_do=False,
    )
    out = reports.render_plan("web", p)
    assert "node" in out
    assert "bun" in out
    assert "docker" in out
    assert "git" in out
    assert "install" in out


def test_render_plan_handles_curl_items():
    nvm = CurlItem(name="nvm", marker="~/.nvm", install="x")
    p = PlanResult(
        profile="default",
        bucket_diffs={
            "brew": {"install": [], "remove": [], "unchanged": []},
            "cask": {"install": [], "remove": [], "unchanged": []},
            "npm_global": {"install": [], "remove": [], "unchanged": []},
            "pip": {"install": [], "remove": [], "unchanged": []},
            "curl": {"install": [nvm], "remove": [], "unchanged": []},
        },
        nothing_to_do=False,
    )
    out = reports.render_plan("default", p)
    assert "nvm" in out
    assert "[curl]" in out


# ---------- state ----------


def test_render_state_empty():
    s = State()
    out = reports.render_state(s)
    assert "No owned items yet" in out
    assert "(never applied)" in out


def test_render_state_with_owned():
    s = State()
    s.add_owned("brew", ["git", "jq"])
    s.add_owned("cask", ["docker"])
    s.last_apply = "2026-05-10T16-00-00Z"
    out = reports.render_state(s)
    assert "git" in out
    assert "jq" in out
    assert "docker" in out
    assert "2026-05-10T16-00-00Z" in out


def test_render_state_with_snapshots(tmp_path):
    s = State()
    s.add_owned("brew", ["git"])
    snapshot(s, tmp_path, label="before-apply")
    out = reports.render_state(s)
    assert "Snapshots" in out
    assert "before-apply" in out


# ---------- state diff ----------


def test_render_state_diff_no_drift():
    out = reports.render_state_diff([])
    assert "No drift" in out


def test_render_state_diff_with_drift():
    r = DriftReport(bucket="brew", added=["fzf"], removed=["jq"])
    out = reports.render_state_diff([r])
    assert "fzf" in out
    assert "jq" in out
    assert "[brew]" in out
    assert "off-manifest" in out


# ---------- apply ----------


def test_render_apply_empty():
    p = PlanResult(profile="default", bucket_diffs={
        b: {"install": [], "remove": [], "unchanged": []}
        for b in ("brew", "cask", "npm_global", "pip", "curl")
    }, nothing_to_do=True)
    res = ApplyResult(ok=True)
    out = reports.render_apply("default", p, res)
    assert "fanout apply" in out
    assert "+0" in out  # zero installed


def test_render_apply_with_installs_removes():
    p = PlanResult(profile="default", bucket_diffs={}, nothing_to_do=False)
    res = ApplyResult(
        ok=True,
        installed={"brew": ["git", "jq"]},
        removed={"brew": ["bun"]},
        failures={"cask": ["docker"]},
        logs=["[brew] install: brew install git jq"],
        snapshot_id="2026-05-10T19-00-00Z",
    )
    out = reports.render_apply("default", p, res)
    assert "Installed" in out
    assert "git" in out
    assert "Removed" in out
    assert "bun" in out
    assert "Failures" in out
    assert "docker" in out
    assert "2026-05-10T19-00-00Z" in out
    assert "brew install git jq" in out


# ---------- verify ----------


def test_render_verify_no_checks():
    res = VerifyResult(ok=True, checks=[])
    out = reports.render_verify(res)
    assert "No verify checks configured" in out


def test_render_verify_with_checks():
    res = VerifyResult(ok=False, checks=[
        {"cmd": "git --version", "ok": True, "stdout": "git 2.x", "stderr": ""},
        {"cmd": "bogus --version", "ok": False, "stdout": "", "stderr": "command not found"},
    ])
    out = reports.render_verify(res)
    assert "git --version" in out
    assert "bogus --version" in out
    assert "command not found" in out


# ---------- rollback ----------


def test_render_rollback():
    res = ApplyResult(ok=True, installed={"brew": ["jq"]}, removed={"brew": ["fzf"]}, logs=["[rollback brew] install"])
    out = reports.render_rollback(res)
    assert "jq" in out
    assert "fzf" in out
    assert "[rollback brew] install" in out


# ---------- ai ----------


def test_render_ai_preview():
    yaml_text = "version: 1\nprofiles:\n  default: [base]\nmodules:\n  base:\n    brew: [git]\n"
    out = reports.render_ai("set up a python ml rig", yaml_text, accepted=False)
    assert "python ml rig" in out
    assert "preview only" in out
    assert "version: 1" in out


def test_render_ai_accepted():
    out = reports.render_ai("set up x", "version: 1\n", accepted=True)
    assert "accepted, written to manifest" in out


# ---------- claude ----------


def test_render_claude_outputs():
    outputs = [
        {"id": 1, "output": "Hello from W1"},
        {"id": 2, "output": "Hello from W2"},
    ]
    out = reports.render_claude(outputs)
    assert "W1" in out
    assert "W2" in out
    assert "Hello from W1" in out
    assert "Hello from W2" in out


# ---------- emit ----------


def test_emit_writes_file_and_returns_path(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    p = reports.emit("test-slug", "<html><body>ok</body></html>", open_browser=False)
    assert p.exists()
    assert "test-slug" in p.name
    assert p.suffix == ".html"
