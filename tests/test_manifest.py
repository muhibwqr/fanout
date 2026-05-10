"""Tests for manifest.py — YAML parse, schema validation, profile resolve, diff."""
from __future__ import annotations

import pytest

from manifest import (
    CurlItem,
    Manifest,
    ManifestError,
    diff,
    load,
    loads,
)
from tests.conftest import MANIFEST_FIXTURES


def test_load_minimal_manifest():
    m = load(str(MANIFEST_FIXTURES / "minimal.yml"))
    assert m.version == 1
    assert "default" in m.profiles
    assert m.profiles["default"] == ["base"]
    assert list(m.modules) == ["base"]
    assert m.modules["base"].brew == ["git", "zsh", "jq"]


def test_load_multi_profile():
    m = load(str(MANIFEST_FIXTURES / "multi_profile.yml"))
    assert set(m.profiles) == {"default", "python-ml", "minimal"}
    assert m.profiles["python-ml"] == ["base", "python", "ml"]
    assert m.modules["python"].pip == ["poetry", "ruff"]
    assert m.modules["web"].cask == ["visual-studio-code"]


def test_load_curl_items():
    m = load(str(MANIFEST_FIXTURES / "multi_profile.yml"))
    curls = m.modules["base"].curl
    assert len(curls) == 1
    assert curls[0].name == "oh-my-zsh"
    assert curls[0].marker == "~/.oh-my-zsh"
    assert "echo install-omz" in curls[0].install


def test_invalid_unknown_module_in_profile():
    with pytest.raises(ManifestError) as exc:
        load(str(MANIFEST_FIXTURES / "invalid_unknown_module.yml"))
    msgs = "\n".join(exc.value.errors)
    assert "ghost" in msgs


def test_invalid_unknown_bucket():
    with pytest.raises(ManifestError) as exc:
        load(str(MANIFEST_FIXTURES / "invalid_bad_bucket.yml"))
    msgs = "\n".join(exc.value.errors)
    assert "yum" in msgs


def test_invalid_curl_missing_keys():
    with pytest.raises(ManifestError) as exc:
        load(str(MANIFEST_FIXTURES / "invalid_curl_missing_keys.yml"))
    msgs = "\n".join(exc.value.errors)
    assert "marker" in msgs or "install" in msgs


def test_loads_empty_raises():
    with pytest.raises(ManifestError):
        loads("")


def test_loads_wrong_version_raises():
    with pytest.raises(ManifestError) as exc:
        loads("version: 2\nprofiles:\n  default: [base]\nmodules:\n  base:\n    brew: [git]\n")
    assert any("version" in e for e in exc.value.errors)


def test_resolve_simple_profile():
    m = load(str(MANIFEST_FIXTURES / "minimal.yml"))
    res = m.resolve("default")
    assert res["brew"] == ["git", "zsh", "jq"]
    assert res["cask"] == []
    assert res["npm_global"] == []
    assert res["pip"] == []
    assert res["curl"] == []


def test_resolve_compose_modules():
    m = load(str(MANIFEST_FIXTURES / "multi_profile.yml"))
    res = m.resolve("default")
    # base + web composed
    assert "git" in res["brew"]
    assert "node" in res["brew"]
    assert "visual-studio-code" in res["cask"]
    assert "typescript" in res["npm_global"]


def test_resolve_dedupes_across_modules():
    raw = """
version: 1
profiles:
  default: [a, b]
modules:
  a:
    brew: [git, jq]
  b:
    brew: [jq, ripgrep]
"""
    m = loads(raw)
    res = m.resolve("default")
    assert res["brew"] == ["git", "jq", "ripgrep"]


def test_resolve_curl_dedupes_by_name():
    raw = """
version: 1
profiles:
  default: [a, b]
modules:
  a:
    curl:
      - { name: nvm, marker: "~/.nvm", install: "x" }
  b:
    curl:
      - { name: nvm, marker: "~/.nvm", install: "y" }
"""
    m = loads(raw)
    res = m.resolve("default")
    assert len(res["curl"]) == 1
    assert res["curl"][0].name == "nvm"


def test_resolve_unknown_profile_raises():
    m = load(str(MANIFEST_FIXTURES / "minimal.yml"))
    with pytest.raises(KeyError):
        m.resolve("ghost")


def test_diff_install_remove_unchanged():
    desired = {"brew": ["git", "jq", "ripgrep"], "cask": [], "npm_global": [], "pip": [], "curl": []}
    installed = {"brew": {"jq", "fzf"}, "cask": set(), "npm_global": set(), "pip": set(), "curl": set()}
    d = diff(desired, installed)
    assert d["brew"]["install"] == ["git", "ripgrep"]
    assert d["brew"]["remove"] == ["fzf"]
    assert d["brew"]["unchanged"] == ["jq"]


def test_diff_curl_compares_by_name():
    nvm = CurlItem(name="nvm", marker="~/.nvm/nvm.sh", install="x")
    omz = CurlItem(name="oh-my-zsh", marker="~/.oh-my-zsh", install="y")
    desired = {"brew": [], "cask": [], "npm_global": [], "pip": [], "curl": [nvm, omz]}
    installed = {"brew": set(), "cask": set(), "npm_global": set(), "pip": set(), "curl": {"oh-my-zsh", "other"}}
    d = diff(desired, installed)
    install_names = [c.name for c in d["curl"]["install"]]
    assert install_names == ["nvm"]
    assert d["curl"]["remove"] == ["other"]
    unchanged_names = [c.name for c in d["curl"]["unchanged"]]
    assert unchanged_names == ["oh-my-zsh"]
