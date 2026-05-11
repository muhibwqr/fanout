"""plan / apply / verify / rollback orchestrator.

Composes manifest + state + adapters. Uses workers.dispatch_tmux for visible
parallel installs when tmux is available; falls back to sequential per-adapter.
"""
from __future__ import annotations

import asyncio
import pathlib
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set

import adapters as ad
import state as state_mod
from adapters.base import Adapter, InstallResult
from manifest import ADAPTER_BUCKETS, CurlItem, Manifest, diff
from state import State


@dataclass
class PlanResult:
    profile: str
    bucket_diffs: dict   # {bucket: {"install": [...], "remove": [...], "unchanged": [...]}}
    nothing_to_do: bool


@dataclass
class ApplyResult:
    ok: bool
    installed: Dict[str, List[str]] = field(default_factory=dict)
    removed: Dict[str, List[str]] = field(default_factory=dict)
    failures: Dict[str, List[str]] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    snapshot_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def compute_plan(
    manifest: Manifest,
    profile: str,
    registry: Optional[Dict[str, Adapter]] = None,
) -> PlanResult:
    """Compute install/remove/unchanged per bucket vs current installed reality."""
    reg = registry or ad.build_registry()
    desired = manifest.resolve(profile)

    # Register curl items so CurlAdapter knows what to inspect.
    curl_items = desired.get("curl", [])
    if isinstance(reg["curl"], ad.CurlAdapter):
        reg["curl"].register(curl_items)

    installed: Dict[str, Set[str]] = {}
    for bucket, adapter in reg.items():
        installed[bucket] = adapter.list_installed()

    bucket_diffs = diff(desired, installed)
    nothing = all(
        not bucket_diffs[b]["install"] and not bucket_diffs[b]["remove"]
        for b in ADAPTER_BUCKETS
    )
    return PlanResult(profile=profile, bucket_diffs=bucket_diffs, nothing_to_do=nothing)


def render_plan(p: PlanResult) -> str:
    """Plain text plan summary."""
    lines = [f"plan for profile: {p.profile}"]
    if p.nothing_to_do:
        lines.append("  (nothing to do)")
        return "\n".join(lines)
    for bucket in ADAPTER_BUCKETS:
        d = p.bucket_diffs[bucket]
        if not d["install"] and not d["remove"] and not d["unchanged"]:
            continue
        lines.append(f"\n[{bucket}]")
        for item in d["install"]:
            name = item.name if isinstance(item, CurlItem) else item
            lines.append(f"  + {name}")
        for item in d["remove"]:
            name = item.name if isinstance(item, CurlItem) else item
            lines.append(f"  - {name}")
        for item in d["unchanged"]:
            name = item.name if isinstance(item, CurlItem) else item
            lines.append(f"  = {name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_plan(
    plan: PlanResult,
    state: State,
    registry: Optional[Dict[str, Adapter]] = None,
    *,
    state_dir: Optional[pathlib.Path] = None,
    snapshot_before: bool = True,
    dry_run: bool = False,
) -> ApplyResult:
    """Execute the plan sequentially per adapter.

    Parallel tmux-paneled dispatch lives in `apply_plan_tmux()`. Both routes
    share the same diff input + state mutation + ApplyResult shape.
    """
    reg = registry or ad.build_registry()
    result = ApplyResult(ok=True)

    if plan.nothing_to_do:
        return result

    if snapshot_before and not dry_run:
        snap = state_mod.snapshot(state, state_dir, label=f"pre-apply-{plan.profile}")
        result.snapshot_id = snap["id"]

    for bucket in ADAPTER_BUCKETS:
        d = plan.bucket_diffs[bucket]
        adapter = reg[bucket]
        installs = d["install"]
        removes = d["remove"]

        if installs:
            items_for_adapter = installs  # str list or CurlItem list
            if dry_run:
                names = [x.name if isinstance(x, CurlItem) else x for x in items_for_adapter]
                result.logs.append(f"[dry-run] {bucket} install: {names}")
            else:
                r = adapter.install(items_for_adapter)
                result.logs.append(f"[{bucket}] install: {r.cmd}")
                if r.ok:
                    names = [x.name if isinstance(x, CurlItem) else x for x in items_for_adapter]
                    result.installed.setdefault(bucket, []).extend(names)
                    state.add_owned(bucket, names)
                else:
                    result.ok = False
                    names = [x.name if isinstance(x, CurlItem) else x for x in items_for_adapter]
                    result.failures.setdefault(bucket, []).extend(names)
                    if r.stderr:
                        result.logs.append(f"[{bucket}] error: {r.stderr[:500]}")

        if removes:
            if dry_run:
                result.logs.append(f"[dry-run] {bucket} remove: {removes}")
            else:
                # For curl adapter, items in state are names; we need to map back. For v0,
                # we just remove from state and let user run uninstall manually if needed.
                # Brew/cask/npm/pip can be removed directly by name.
                if bucket == "curl":
                    state.remove_owned(bucket, removes)
                    result.removed.setdefault(bucket, []).extend(removes)
                    result.logs.append(f"[curl] dropped from state (manual cleanup may be needed): {removes}")
                else:
                    r = adapter.uninstall(removes)
                    result.logs.append(f"[{bucket}] uninstall: {r.cmd}")
                    if r.ok:
                        result.removed.setdefault(bucket, []).extend(removes)
                        state.remove_owned(bucket, removes)
                    else:
                        result.ok = False
                        result.failures.setdefault(bucket, []).extend(removes)
                        if r.stderr:
                            result.logs.append(f"[{bucket}] error: {r.stderr[:500]}")

    if not dry_run:
        state_mod.save(state, state_dir)

    return result


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    ok: bool
    checks: List[dict] = field(default_factory=list)  # {cmd, ok, stdout, stderr}


def verify(manifest: Manifest, *, timeout: float = 30.0) -> VerifyResult:
    """Run verify-section sanity commands."""
    checks_cfg = ((manifest.settings or {}).get("verify") or {}).get("checks", []) or []
    out = VerifyResult(ok=True)
    for c in checks_cfg:
        cmd = c.get("cmd")
        if not cmd:
            continue
        try:
            cp = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            ok = cp.returncode == 0
            out.checks.append(
                {
                    "cmd": cmd,
                    "ok": ok,
                    "stdout": (cp.stdout or "").strip()[:200],
                    "stderr": (cp.stderr or "").strip()[:200],
                }
            )
            if not ok:
                out.ok = False
        except subprocess.TimeoutExpired:
            out.checks.append({"cmd": cmd, "ok": False, "stdout": "", "stderr": f"timeout {timeout}s"})
            out.ok = False
    return out


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def rollback(
    state: State,
    registry: Optional[Dict[str, Adapter]] = None,
    *,
    state_dir: Optional[pathlib.Path] = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Restore most recent snapshot. Computes inverse diff vs current ownership."""
    reg = registry or ad.build_registry()
    result = ApplyResult(ok=True)

    last = state_mod.latest_snapshot(state)
    if last is None:
        result.ok = False
        result.logs.append("no snapshot to rollback to")
        return result

    payload = state_mod.load_snapshot(last)
    target_owned = {k: set(v) for k, v in payload.get("owned", {}).items()}

    for bucket in ADAPTER_BUCKETS:
        current = state.owned_set(bucket)
        target = target_owned.get(bucket, set())
        to_install = sorted(target - current)
        to_remove = sorted(current - target)

        adapter = reg[bucket]
        if to_install:
            if dry_run:
                result.logs.append(f"[dry-run rollback] {bucket} install: {to_install}")
            elif bucket == "curl":
                result.logs.append(f"[rollback] curl re-install not automated; manual: {to_install}")
            else:
                r = adapter.install(to_install)
                result.logs.append(f"[rollback {bucket}] install: {r.cmd}")
                if r.ok:
                    state.add_owned(bucket, to_install)
                    result.installed.setdefault(bucket, []).extend(to_install)
                else:
                    result.ok = False
                    result.failures.setdefault(bucket, []).extend(to_install)

        if to_remove:
            if dry_run:
                result.logs.append(f"[dry-run rollback] {bucket} remove: {to_remove}")
            elif bucket == "curl":
                state.remove_owned(bucket, to_remove)
                result.removed.setdefault(bucket, []).extend(to_remove)
            else:
                r = adapter.uninstall(to_remove)
                result.logs.append(f"[rollback {bucket}] uninstall: {r.cmd}")
                if r.ok:
                    state.remove_owned(bucket, to_remove)
                    result.removed.setdefault(bucket, []).extend(to_remove)
                else:
                    result.ok = False
                    result.failures.setdefault(bucket, []).extend(to_remove)

    if not dry_run:
        state_mod.save(state, state_dir)
    return result


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def drift_check(state: State, registry: Optional[Dict[str, Adapter]] = None) -> list:
    """Compare state.owned vs `adapter.list_installed()` for each adapter."""
    reg = registry or ad.build_registry()
    actual: Dict[str, Set[str]] = {b: reg[b].list_installed() for b in ADAPTER_BUCKETS}
    return state_mod.drift(state, actual)


# ---------------------------------------------------------------------------
# Tmux-paneled parallel apply
# ---------------------------------------------------------------------------


def apply_plan_tmux(
    plan: PlanResult,
    state: State,
    *,
    state_dir: Optional[pathlib.Path] = None,
    snapshot_before: bool = True,
    keep_session: bool = False,
    timeout: float = 1800.0,
) -> ApplyResult:
    """Apply the plan with one tmux pane per adapter batch.

    Each pane runs the adapter's install command (e.g. `brew install A B C`)
    in parallel. Output is captured per pane via tee; we poll sentinel files.
    Falls back to sequential `apply_plan` if tmux is unavailable.
    """
    import asyncio
    import shlex
    import workers

    if not workers.tmux_available():
        result = apply_plan(
            plan, state, state_dir=state_dir,
            snapshot_before=snapshot_before, dry_run=False,
        )
        result.logs.insert(0, "[engine] tmux not on PATH; ran sequentially")
        return result

    reg = ad.build_registry()
    curl_items_desired = plan.bucket_diffs.get("curl", {}).get("install", [])
    if isinstance(reg["curl"], ad.CurlAdapter):
        reg["curl"].register(curl_items_desired)

    result = ApplyResult(ok=True)
    if plan.nothing_to_do:
        return result

    if snapshot_before:
        snap = state_mod.snapshot(state, state_dir, label=f"pre-apply-{plan.profile}")
        result.snapshot_id = snap["id"]

    # Build the shell command per adapter batch.
    tasks: List[dict] = []
    for bucket in ADAPTER_BUCKETS:
        d = plan.bucket_diffs[bucket]
        installs = d["install"]
        removes = d["remove"]
        if not installs and not removes:
            continue
        names_install = [c.name if isinstance(c, CurlItem) else c for c in installs]
        names_remove = list(removes)

        cmd = _build_adapter_shell_cmd(bucket, installs, removes)
        if cmd is None:
            continue
        tasks.append(
            {
                "bucket": bucket,
                "shell": cmd,
                "names_install": names_install,
                "names_remove": names_remove,
            }
        )

    if not tasks:
        return result

    # Dispatch to tmux. workers.dispatch_tmux runs `claude -p "<prompt>"` per
    # pane by default; we need raw shell, so we build per-pane shell commands
    # and dispatch via a thin wrapper.
    info = _dispatch_tmux_shells(
        [t["shell"] for t in tasks],
        labels=[t["bucket"] for t in tasks],
    )
    result.logs.append(f"[engine] tmux session: {info['session']}")
    result.logs.append(f"  attach: tmux attach -t {info['session']}")
    result.logs.append(f"  logs:   {info['run_dir']}/W*.out")

    pane_outputs = asyncio.run(
        workers.wait_for_done(info["pane_files"], poll_interval=2.0, timeout=timeout)
    )

    # Assemble per-task results. Naive: assume any non-error output means success.
    for task, pane in zip(tasks, pane_outputs):
        bucket = task["bucket"]
        output = pane.get("output", "")
        ok = not pane.get("error") and ("error" not in output.lower()[:200] or "0 vulnerabilities" in output.lower())
        log_excerpt = output[-500:]
        result.logs.append(f"[{bucket}] (tmux pane) {'OK' if ok else 'FAILED'}\n{log_excerpt}")
        if ok:
            if task["names_install"]:
                result.installed.setdefault(bucket, []).extend(task["names_install"])
                state.add_owned(bucket, task["names_install"])
            if task["names_remove"]:
                result.removed.setdefault(bucket, []).extend(task["names_remove"])
                state.remove_owned(bucket, task["names_remove"])
        else:
            result.ok = False
            result.failures.setdefault(bucket, []).extend(task["names_install"] + task["names_remove"])

    if not keep_session:
        workers.kill_tmux_session(info["session"])

    state_mod.save(state, state_dir)
    return result


def _build_adapter_shell_cmd(
    bucket: str, installs, removes
) -> Optional[str]:
    """Compose the raw shell command an adapter would run, for tmux dispatch."""
    if bucket == "brew":
        parts = []
        if installs:
            parts.append("brew install " + " ".join(installs))
        if removes:
            parts.append("brew uninstall --ignore-dependencies " + " ".join(removes))
        return " && ".join(parts) if parts else None
    if bucket == "cask":
        parts = []
        if installs:
            parts.append("brew install --cask " + " ".join(installs))
        if removes:
            parts.append("brew uninstall --cask " + " ".join(removes))
        return " && ".join(parts) if parts else None
    if bucket == "npm_global":
        parts = []
        if installs:
            parts.append("npm install -g " + " ".join(installs))
        if removes:
            parts.append("npm uninstall -g " + " ".join(removes))
        return " && ".join(parts) if parts else None
    if bucket == "pip":
        parts = []
        if installs:
            parts.append("python3 -m pip install --user " + " ".join(installs))
        if removes:
            parts.append("python3 -m pip uninstall -y " + " ".join(removes))
        return " && ".join(parts) if parts else None
    if bucket == "curl":
        # CurlItem entries: chain bash -c "<install>" per item.
        steps = []
        for c in installs:
            # c is a CurlItem
            steps.append(c.install)
        if not steps:
            return None
        return " && ".join(f'( {s} )' for s in steps)
    return None


def _dispatch_tmux_shells(shells: list, labels: list) -> dict:
    """Spawn one detached tmux session with one pane per raw shell command.

    Mirrors workers.dispatch_tmux but invokes raw shell instead of claude -p.
    """
    import os
    import pathlib
    import shlex
    import subprocess
    import uuid

    rid = uuid.uuid4().hex[:8]
    session = f"fanout_apply_{rid}"
    rd = pathlib.Path(f"/tmp/fanout_apply_{rid}")
    rd.mkdir(parents=True, exist_ok=True)

    pane_files = []
    for i, (shell_cmd, label) in enumerate(zip(shells, labels), start=1):
        prompt_file = rd / f"W{i}.prompt"
        out_file = rd / f"W{i}.out"
        done_file = rd / f"W{i}.done"
        prompt_file.write_text(shell_cmd)
        pane_files.append(
            {"id": i, "prompt": str(prompt_file), "out": str(out_file), "done": str(done_file)}
        )
        wrapped = (
            f"echo '=== W{i} {label} ==='; "
            f"{shell_cmd} 2>&1 | tee {shlex.quote(str(out_file))}; "
            f"echo \"exit=$?\" >> {shlex.quote(str(out_file))}; "
            f"touch {shlex.quote(str(done_file))}; "
            f"echo; echo '=== W{i} done. ctrl-b d to detach. ==='; sleep 86400"
        )
        if i == 1:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session, "-n", "apply",
                 "bash", "-c", wrapped],
                check=True,
            )
        else:
            subprocess.run(
                ["tmux", "split-window", "-t", session, "bash", "-c", wrapped],
                check=True,
            )
            subprocess.run(["tmux", "select-layout", "-t", session, "tiled"], check=False)
    subprocess.run(["tmux", "select-layout", "-t", session, "tiled"], check=False)
    return {"session": session, "run_dir": str(rd), "pane_files": pane_files}
