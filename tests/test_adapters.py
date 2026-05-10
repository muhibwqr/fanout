"""Tests for adapters/* — mock subprocess.run; assert correct commands shaped."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from adapters import (
    BrewAdapter,
    CaskAdapter,
    CurlAdapter,
    NpmAdapter,
    PipAdapter,
    build_registry,
)
from adapters.base import InstallResult
from manifest import CurlItem


# ---------- helpers ----------


class FakeRunResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def patch_run(monkeypatch, mapping):
    """`mapping`: dict argv-tuple -> FakeRunResult, or callable(argv) -> FakeRunResult."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if callable(mapping):
            return mapping(cmd)
        key = tuple(cmd)
        if key in mapping:
            return mapping[key]
        # default success no output
        return FakeRunResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ---------- BrewAdapter ----------


def test_brew_list_installed_parses(monkeypatch):
    patch_run(monkeypatch, {
        ("brew", "list", "--formula", "-1"): FakeRunResult(stdout="git\njq\nheroku/brew/heroku\n"),
    })
    s = BrewAdapter().list_installed()
    assert s == {"git", "jq", "heroku"}


def test_brew_install_runs_correct_cmd(monkeypatch):
    calls = patch_run(monkeypatch, {})
    BrewAdapter().install(["git", "jq"])
    assert calls[-1][:2] == ["brew", "install"]
    assert "git" in calls[-1] and "jq" in calls[-1]


def test_brew_install_empty_returns_ok():
    r = BrewAdapter().install([])
    assert r.ok is True
    assert "(no items)" in r.cmd


def test_brew_uninstall_uses_ignore_dependencies(monkeypatch):
    calls = patch_run(monkeypatch, {})
    BrewAdapter().uninstall(["git"])
    assert "--ignore-dependencies" in calls[-1]


# ---------- CaskAdapter ----------


def test_cask_list_installed_parses(monkeypatch):
    patch_run(monkeypatch, {
        ("brew", "list", "--cask", "-1"): FakeRunResult(stdout="docker\nvisual-studio-code\n"),
    })
    s = CaskAdapter().list_installed()
    assert s == {"docker", "visual-studio-code"}


def test_cask_install_runs_correct_cmd(monkeypatch):
    calls = patch_run(monkeypatch, {})
    CaskAdapter().install(["docker"])
    assert calls[-1][:3] == ["brew", "install", "--cask"]


# ---------- NpmAdapter ----------


def test_npm_list_installed_parses_json(monkeypatch):
    payload = json.dumps({
        "dependencies": {"typescript": {}, "yarn": {}}
    })
    patch_run(monkeypatch, {
        ("npm", "list", "-g", "--depth=0", "--json"): FakeRunResult(stdout=payload),
    })
    assert NpmAdapter().list_installed() == {"typescript", "yarn"}


def test_npm_list_handles_bad_json(monkeypatch):
    patch_run(monkeypatch, {
        ("npm", "list", "-g", "--depth=0", "--json"): FakeRunResult(stdout="not json"),
    })
    assert NpmAdapter().list_installed() == set()


def test_npm_install_runs_correct_cmd(monkeypatch):
    calls = patch_run(monkeypatch, {})
    NpmAdapter().install(["typescript"])
    assert calls[-1] == ["npm", "install", "-g", "typescript"]


# ---------- PipAdapter ----------


def test_pip_list_installed_parses(monkeypatch):
    payload = json.dumps([{"name": "Poetry", "version": "1.0"}, {"name": "ruff"}])
    patch_run(monkeypatch, {
        ("python3", "-m", "pip", "list", "--user", "--format=json"): FakeRunResult(stdout=payload),
    })
    s = PipAdapter().list_installed()
    assert s == {"poetry", "ruff"}  # lowercased


def test_pip_install_runs_correct_cmd(monkeypatch):
    calls = patch_run(monkeypatch, {})
    PipAdapter().install(["ruff"])
    assert calls[-1] == ["python3", "-m", "pip", "install", "--user", "ruff"]


# ---------- CurlAdapter ----------


def test_curl_list_installed_uses_marker(tmp_path):
    marker = tmp_path / ".oh-my-zsh"
    marker.mkdir()
    a = CurlAdapter()
    a.register([
        CurlItem(name="oh-my-zsh", marker=str(marker), install="x"),
        CurlItem(name="nvm", marker=str(tmp_path / ".nvm"), install="y"),
    ])
    assert a.list_installed() == {"oh-my-zsh"}


def test_curl_install_skips_when_marker_present(tmp_path):
    marker = tmp_path / ".oh-my-zsh"
    marker.mkdir()
    a = CurlAdapter()
    item = CurlItem(name="oh-my-zsh", marker=str(marker), install="echo SHOULD-NOT-RUN")
    r = a.install([item])
    assert r.ok is True
    assert "already installed" in r.cmd


def test_curl_install_runs_when_marker_missing(tmp_path, monkeypatch):
    item = CurlItem(name="nvm", marker=str(tmp_path / ".nvm"), install="echo hi")
    calls = patch_run(monkeypatch, {})
    a = CurlAdapter()
    r = a.install([item])
    assert calls
    assert calls[-1][0] == "bash"
    assert calls[-1][2] == "echo hi"


# ---------- registry ----------


def test_build_registry_has_all_buckets():
    reg = build_registry()
    assert set(reg) == {"brew", "cask", "npm_global", "pip", "curl"}
