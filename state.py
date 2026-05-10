"""State file IO, snapshots, drift detection."""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set

DEFAULT_STATE_DIR = pathlib.Path(os.path.expanduser("~/.fanout"))


@dataclass
class State:
    version: int = 1
    last_apply: Optional[str] = None
    owned: Dict[str, List[str]] = field(
        default_factory=lambda: {"brew": [], "cask": [], "npm_global": [], "pip": [], "curl": []}
    )
    snapshots: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def add_owned(self, bucket: str, items: List[str]) -> None:
        if bucket not in self.owned:
            self.owned[bucket] = []
        seen = set(self.owned[bucket])
        for it in items:
            if it not in seen:
                self.owned[bucket].append(it)
                seen.add(it)

    def remove_owned(self, bucket: str, items: List[str]) -> None:
        if bucket not in self.owned:
            return
        drop = set(items)
        self.owned[bucket] = [x for x in self.owned[bucket] if x not in drop]

    def owned_set(self, bucket: str) -> Set[str]:
        return set(self.owned.get(bucket, []))


def state_path(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    d = state_dir or DEFAULT_STATE_DIR
    return d / "state.json"


def snapshots_dir(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    d = state_dir or DEFAULT_STATE_DIR
    return d / "snapshots"


def load(state_dir: Optional[pathlib.Path] = None) -> State:
    """Load the state file. Returns a fresh empty State if absent."""
    p = state_path(state_dir)
    if not p.exists():
        return State()
    raw = json.loads(p.read_text())
    s = State()
    s.version = raw.get("version", 1)
    s.last_apply = raw.get("last_apply")
    owned = raw.get("owned") or {}
    for k in ("brew", "cask", "npm_global", "pip", "curl"):
        s.owned[k] = list(owned.get(k, []))
    s.snapshots = list(raw.get("snapshots") or [])
    return s


def save(state: State, state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    d = state_dir or DEFAULT_STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = state_path(d)
    state.last_apply = _now_iso()
    p.write_text(json.dumps(state.to_dict(), indent=2))
    return p


def snapshot(
    state: State,
    state_dir: Optional[pathlib.Path] = None,
    *,
    label: Optional[str] = None,
) -> dict:
    """Take a snapshot of the current state. Returns the snapshot metadata."""
    d = state_dir or DEFAULT_STATE_DIR
    sd = snapshots_dir(d)
    sd.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    path = sd / f"{ts}.json"
    payload = {
        "id": ts,
        "label": label,
        "owned": {k: list(v) for k, v in state.owned.items()},
    }
    path.write_text(json.dumps(payload, indent=2))
    meta = {"id": ts, "label": label, "path": str(path)}
    state.snapshots.append(meta)
    return meta


def latest_snapshot(state: State) -> Optional[dict]:
    if not state.snapshots:
        return None
    return state.snapshots[-1]


def load_snapshot(meta: dict) -> dict:
    """Load a snapshot's owned-items payload from disk."""
    p = pathlib.Path(meta["path"])
    return json.loads(p.read_text())


@dataclass
class DriftReport:
    bucket: str
    added: List[str] = field(default_factory=list)    # installed off-manifest (not in state.owned)
    removed: List[str] = field(default_factory=list)  # in state.owned but no longer installed


def drift(
    state: State,
    actually_installed: Dict[str, Set[str]],
) -> List[DriftReport]:
    """Compare state.owned vs reality. Returns list of DriftReports per bucket with drift."""
    reports: List[DriftReport] = []
    for bucket in ("brew", "cask", "npm_global", "pip", "curl"):
        owned = state.owned_set(bucket)
        live = actually_installed.get(bucket, set())
        added = sorted(live - owned)
        removed = sorted(owned - live)
        if added or removed:
            reports.append(DriftReport(bucket=bucket, added=added, removed=removed))
    return reports


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
