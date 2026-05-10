"""Manifest: YAML parse, schema validate, profile resolution, diff."""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

ADAPTER_BUCKETS = ("brew", "cask", "npm_global", "pip", "curl")


class ManifestError(ValueError):
    """Manifest failed schema validation."""

    def __init__(self, errors: List[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class CurlItem:
    name: str
    marker: str
    install: str
    uninstall: Optional[str] = None


@dataclass
class Module:
    name: str
    brew: List[str] = field(default_factory=list)
    cask: List[str] = field(default_factory=list)
    npm_global: List[str] = field(default_factory=list)
    pip: List[str] = field(default_factory=list)
    curl: List[CurlItem] = field(default_factory=list)


@dataclass
class Manifest:
    version: int
    profiles: Dict[str, List[str]]
    modules: Dict[str, Module]
    settings: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def resolve(self, profile_name: str) -> Dict[str, List[str]]:
        """Resolve profile -> {adapter_bucket: [items]} by composing modules.

        Returns dict keyed by adapter bucket ('brew', 'cask', 'npm_global', 'pip', 'curl').
        For 'curl', values are CurlItem dataclasses; others are str.
        """
        if profile_name not in self.profiles:
            raise KeyError(f"profile {profile_name!r} not in {list(self.profiles)}")
        module_names = self.profiles[profile_name]
        out: Dict[str, list] = {b: [] for b in ADAPTER_BUCKETS}
        for mn in module_names:
            if mn not in self.modules:
                raise KeyError(f"profile {profile_name!r} references unknown module {mn!r}")
            m = self.modules[mn]
            out["brew"].extend(m.brew)
            out["cask"].extend(m.cask)
            out["npm_global"].extend(m.npm_global)
            out["pip"].extend(m.pip)
            out["curl"].extend(m.curl)
        # Deduplicate non-curl lists preserving order.
        for k in ("brew", "cask", "npm_global", "pip"):
            seen, dedup = set(), []
            for it in out[k]:
                if it not in seen:
                    seen.add(it)
                    dedup.append(it)
            out[k] = dedup
        # Curl: dedup by name.
        seen_n, dedup_c = set(), []
        for it in out["curl"]:
            if it.name not in seen_n:
                seen_n.add(it.name)
                dedup_c.append(it)
        out["curl"] = dedup_c
        return out

    def all_modules_referenced(self) -> List[str]:
        seen = set()
        for items in self.profiles.values():
            seen.update(items)
        return sorted(seen)


def load(path: str) -> Manifest:
    """Load and validate a manifest from a YAML file."""
    text = pathlib.Path(path).read_text()
    return loads(text)


def loads(text: str) -> Manifest:
    """Load and validate a manifest from a YAML string."""
    raw = yaml.safe_load(text)
    if raw is None:
        raise ManifestError(["manifest is empty"])
    return _validate(raw)


def _validate(raw: dict) -> Manifest:
    errs: List[str] = []
    if not isinstance(raw, dict):
        raise ManifestError([f"top-level must be a mapping, got {type(raw).__name__}"])

    if raw.get("version") != 1:
        errs.append(f"version must be 1, got {raw.get('version')!r}")

    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        errs.append("`profiles` must be a non-empty mapping of name -> [module names]")
        profiles_raw = {}

    profiles: Dict[str, List[str]] = {}
    for pname, items in profiles_raw.items():
        if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
            errs.append(f"profile {pname!r} must be a list of module-name strings")
            continue
        profiles[pname] = list(items)

    modules_raw = raw.get("modules")
    if not isinstance(modules_raw, dict):
        errs.append("`modules` must be a mapping")
        modules_raw = {}

    modules: Dict[str, Module] = {}
    for mname, body in (modules_raw or {}).items():
        if not isinstance(body, dict):
            errs.append(f"module {mname!r} must be a mapping")
            continue
        unknown = set(body) - set(ADAPTER_BUCKETS)
        if unknown:
            errs.append(
                f"module {mname!r} has unknown buckets: {sorted(unknown)}; "
                f"valid: {ADAPTER_BUCKETS}"
            )
        m = Module(name=mname)
        for bucket in ("brew", "cask", "npm_global", "pip"):
            vals = body.get(bucket, []) or []
            if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
                errs.append(f"module {mname!r}.{bucket} must be list of strings")
                continue
            setattr(m, bucket, list(vals))
        curl_vals = body.get("curl", []) or []
        if curl_vals and not isinstance(curl_vals, list):
            errs.append(f"module {mname!r}.curl must be a list")
            curl_vals = []
        for i, c in enumerate(curl_vals):
            if not isinstance(c, dict):
                errs.append(f"module {mname!r}.curl[{i}] must be a mapping")
                continue
            missing = [k for k in ("name", "marker", "install") if k not in c]
            if missing:
                errs.append(f"module {mname!r}.curl[{i}] missing keys: {missing}")
                continue
            m.curl.append(
                CurlItem(
                    name=c["name"],
                    marker=c["marker"],
                    install=c["install"],
                    uninstall=c.get("uninstall"),
                )
            )
        modules[mname] = m

    # Cross-reference: every profile's modules must exist.
    for pname, mlist in profiles.items():
        for mn in mlist:
            if mn not in modules:
                errs.append(f"profile {pname!r} references unknown module {mn!r}")

    if errs:
        raise ManifestError(errs)

    settings = raw.get("settings") or {}
    if not isinstance(settings, dict):
        raise ManifestError(["`settings` must be a mapping"])

    return Manifest(
        version=raw["version"],
        profiles=profiles,
        modules=modules,
        settings=settings,
        raw=raw,
    )


def diff(desired: Dict[str, list], installed: Dict[str, set]) -> dict:
    """Compute install/remove/unchanged per adapter bucket.

    `desired` from Manifest.resolve(profile). `installed` is {bucket: set(item_id)}.
    For 'curl' bucket, desired items are CurlItem; we compare by .name.
    Returns {bucket: {"install": [...], "remove": [...], "unchanged": [...]}}.
    """
    out: dict = {}
    for bucket in ADAPTER_BUCKETS:
        desired_items = desired.get(bucket, [])
        installed_set = installed.get(bucket, set())
        if bucket == "curl":
            desired_names = {c.name for c in desired_items}
            to_install = [c for c in desired_items if c.name not in installed_set]
            unchanged = [c for c in desired_items if c.name in installed_set]
            to_remove = sorted(installed_set - desired_names)
            out[bucket] = {
                "install": to_install,
                "remove": to_remove,
                "unchanged": unchanged,
            }
        else:
            desired_set = set(desired_items)
            out[bucket] = {
                "install": sorted(desired_set - installed_set),
                "remove": sorted(installed_set - desired_set),
                "unchanged": sorted(desired_set & installed_set),
            }
    return out
