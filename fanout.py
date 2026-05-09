"""Operation Fanout — local multi-agent task decomposition orchestrator.

Pipeline: build_bundle -> plan -> dispatch N workers in parallel -> reduce.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from typing import Any, List, Optional

from context import build_bundle
from prompts import PLANNER_SYSTEM, REDUCER_SYSTEM, planner_schema
from workers import (
    BackendError,
    BackendTimeout,
    call_claude,
    parse_claude_envelope,
)

VALID_MODES = ("scratch", "extend", "greenfield")
VALID_STRATEGIES = ("by_file", "by_dimension", "by_phase", "by_hypothesis")
VALID_MERGE_PLANS = ("concat", "vote", "rank", "synthesize")
VALID_N = (2, 4, 6, 8, 10)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanParseError(RuntimeError):
    """Planner output could not be parsed as JSON."""


class PlanValidationError(RuntimeError):
    """Planner output parsed but failed one or more validation rules."""

    def __init__(self, errors: List[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------


def extract_json(raw: str) -> Any:
    """Extract a JSON value from `raw`, tolerating fences, preambles, and trailing text.

    Strategy:
      1. Try direct json.loads on the full string.
      2. Walk the string finding the first balanced {..} or [..], honoring strings.
    """
    if raw is None:
        raise PlanParseError("empty planner output")
    s = raw.strip()
    if not s:
        raise PlanParseError("empty planner output")

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Walk to find first balanced JSON value.
    for i, c in enumerate(s):
        if c not in "{[":
            continue
        candidate = _balanced_slice(s, i)
        if candidate is None:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise PlanParseError(f"no parseable JSON in:\n{raw[:500]}")


def _balanced_slice(s: str, start: int) -> Optional[str]:
    """Return s[start:end+1] where end closes the brace/bracket at `start`. None on failure."""
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for j in range(start, len(s)):
        c = s[j]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return s[start : j + 1]
    return None


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------


def validate_plan(
    plan: Any,
    command: str,
    n: int,
    mode: str,
    bundle: dict,
) -> dict:
    """Validate a plan dict. Returns the plan if OK; raises PlanValidationError otherwise."""
    errors: List[str] = []
    errors += _check_schema(plan)
    if errors:
        # Schema failures make further checks unsafe.
        raise PlanValidationError(errors)
    errors += _check_n(plan, n)
    errors += _check_mode(plan, mode)
    errors += _check_unique_ids(plan)
    errors += _check_files_in_bundle(plan, mode, bundle)
    errors += _check_overlap(plan)
    errors += _check_self_contained(plan)
    if errors:
        raise PlanValidationError(errors)
    return plan


def _check_schema(plan: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(plan, dict):
        return [f"plan must be a JSON object, got {type(plan).__name__}"]
    for key in ("n", "mode", "strategy", "subtasks", "merge_plan"):
        if key not in plan:
            errs.append(f"missing required key: {key}")
    if errs:
        return errs
    if not isinstance(plan["n"], int):
        errs.append("`n` must be int")
    if plan.get("mode") not in VALID_MODES:
        errs.append(f"`mode` must be one of {VALID_MODES}, got {plan.get('mode')!r}")
    if plan.get("strategy") not in VALID_STRATEGIES:
        errs.append(
            f"`strategy` must be one of {VALID_STRATEGIES}, got {plan.get('strategy')!r}"
        )
    if plan.get("merge_plan") not in VALID_MERGE_PLANS:
        errs.append(
            f"`merge_plan` must be one of {VALID_MERGE_PLANS}, got {plan.get('merge_plan')!r}"
        )
    if not isinstance(plan.get("subtasks"), list):
        errs.append("`subtasks` must be a list")
        return errs
    for i, sub in enumerate(plan["subtasks"]):
        if not isinstance(sub, dict):
            errs.append(f"subtask[{i}] must be an object")
            continue
        for key, ty in (
            ("id", int),
            ("title", str),
            ("instructions", str),
            ("read_files", list),
            ("refs", list),
            ("expected_output", str),
        ):
            if key not in sub:
                errs.append(f"subtask[{i}] missing key: {key}")
            elif not isinstance(sub[key], ty):
                errs.append(
                    f"subtask[{i}].{key} must be {ty.__name__}, got {type(sub[key]).__name__}"
                )
    return errs


def _check_n(plan: dict, n: int) -> List[str]:
    errs: List[str] = []
    if plan["n"] != n:
        errs.append(f"n mismatch: plan declares n={plan['n']}, requested n={n}")
    if len(plan["subtasks"]) != n:
        errs.append(
            f"subtask count mismatch: {len(plan['subtasks'])} subtasks, requested n={n}"
        )
    titles = [s.get("title", "") for s in plan["subtasks"]]
    if len(titles) > 1 and len(set(titles)) < len(titles):
        errs.append(
            "duplicate subtask titles — planner may not have found N meaningful slices; "
            "consider re-running with smaller -n"
        )
    return errs


def _check_mode(plan: dict, mode: str) -> List[str]:
    errs: List[str] = []
    if plan["mode"] != mode:
        errs.append(f"mode mismatch: plan={plan['mode']!r}, requested={mode!r}")
    if mode == "extend":
        for s in plan["subtasks"]:
            if not s.get("read_files"):
                errs.append(
                    f"subtask {s.get('id')}: extend mode requires non-empty read_files"
                )
    elif mode in ("scratch", "greenfield"):
        for s in plan["subtasks"]:
            if s.get("read_files"):
                errs.append(
                    f"subtask {s.get('id')}: {mode} mode forbids read_files (got {s['read_files']})"
                )
    return errs


def _check_unique_ids(plan: dict) -> List[str]:
    errs: List[str] = []
    ids = [s.get("id") for s in plan["subtasks"]]
    if len(set(ids)) != len(ids):
        errs.append(f"duplicate subtask ids: {ids}")
    expected = set(range(1, len(ids) + 1))
    if set(ids) != expected:
        errs.append(f"subtask ids must be 1..{len(ids)}, got {ids}")
    return errs


def _check_files_in_bundle(plan: dict, mode: str, bundle: dict) -> List[str]:
    if mode != "extend":
        return []
    bundle_paths = {f["path"] for f in bundle.get("files", [])}
    errs: List[str] = []
    for s in plan["subtasks"]:
        for fp in s.get("read_files", []):
            if fp not in bundle_paths:
                errs.append(
                    f"subtask {s.get('id')}: read_files references {fp!r} not in bundle "
                    f"({len(bundle_paths)} known paths)"
                )
    return errs


def _check_overlap(plan: dict) -> List[str]:
    if plan["strategy"] != "by_file":
        return []
    errs: List[str] = []
    subs = plan["subtasks"]
    for i in range(len(subs)):
        for j in range(i + 1, len(subs)):
            shared = set(subs[i].get("read_files", [])) & set(
                subs[j].get("read_files", [])
            )
            if len(shared) > 1:
                errs.append(
                    f"by_file overlap rule: subtasks {subs[i]['id']} and {subs[j]['id']} "
                    f"share {len(shared)} files: {sorted(shared)}"
                )
    return errs


def _check_self_contained(plan: dict) -> List[str]:
    errs: List[str] = []
    bad = ("previous worker", "previous subtask", "worker n's output")
    for s in plan["subtasks"]:
        instr = s.get("instructions", "").lower()
        for needle in bad:
            if needle in instr:
                errs.append(
                    f"subtask {s.get('id')}: instructions reference cross-worker output "
                    f"(found {needle!r}); subtasks must be independent"
                )
                break
    return errs


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def plan(
    command: str,
    n: int,
    mode: str,
    bundle: dict,
    *,
    timeout: float = 600.0,
) -> dict:
    payload = json.dumps(
        {"command": command, "n": n, "mode": mode, "context_bundle": bundle},
        indent=2,
    )
    raw = await call_claude(
        payload,
        system=PLANNER_SYSTEM,
        timeout=timeout,
    )
    inner = parse_claude_envelope(raw)
    obj = extract_json(inner)
    return validate_plan(obj, command, n, mode, bundle)


async def run_worker(
    sub: dict,
    bundle: dict,
    repo: Optional[str],
    *,
    timeout: float = 600.0,
) -> dict:
    needed = [f for f in bundle.get("files", []) if f["path"] in sub.get("read_files", [])]
    worker_ctx = {"files": needed, "refs": sub.get("refs", [])}
    prompt = json.dumps({"task": sub, "context": worker_ctx}, indent=2)
    out = await call_claude(prompt, cwd=repo, timeout=timeout)
    return {
        "id": sub["id"],
        "title": sub["title"],
        "output": out,
        "error": False,
    }


async def reduce_(command: str, plan_dict: dict, results: List[dict]) -> str:
    payload = json.dumps(
        {"command": command, "plan": plan_dict, "results": results}, indent=2
    )
    return await call_claude(payload, system=REDUCER_SYSTEM)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="fanout",
        description="Local multi-agent task decomposition orchestrator.",
    )
    ap.add_argument("command", help="The task to decompose.")
    ap.add_argument("-n", type=int, default=4, choices=VALID_N)
    ap.add_argument("--mode", choices=VALID_MODES, default="greenfield")
    ap.add_argument("--repo", help="Repo root for extend mode.")
    ap.add_argument("--files", nargs="*", default=[], help="Glob patterns (repo-relative).")
    ap.add_argument("--refs", nargs="*", default=[], help="URLs / paths passed as refs.")
    ap.add_argument("--dry-run", action="store_true", help="Stop after planner.")
    ap.add_argument("--plan", dest="plan_path", help="Skip planner; load JSON plan from path.")
    ap.add_argument("--out-plan", help="Write the validated plan JSON to this path.")
    ap.add_argument(
        "--no-reducer",
        action="store_true",
        help="Print raw worker outputs instead of running the reducer.",
    )
    ap.add_argument("--timeout", type=float, default=600.0, help="Per-call timeout (seconds).")
    return ap.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> Optional[str]:
    if args.mode == "extend" and not args.repo:
        return "extend mode requires --repo"
    if args.mode == "greenfield" and (args.repo or args.files or args.refs):
        return "greenfield mode takes no context inputs (--repo/--files/--refs)"
    if args.mode == "scratch" and args.repo:
        return "scratch mode does not take --repo (use extend if you have a repo)"
    return None


def _print_plan_summary(p: dict, file=sys.stderr) -> None:
    print(
        f"[plan] mode={p['mode']} strategy={p['strategy']} n={p['n']} "
        f"merge={p['merge_plan']}",
        file=file,
    )


async def _main(args: argparse.Namespace) -> int:
    err = _validate_args(args)
    if err:
        print(f"[fanout] error: {err}", file=sys.stderr)
        return 1

    print(
        f"[fanout] mode={args.mode}  n={args.n}  repo={args.repo or '(none)'}",
        file=sys.stderr,
    )

    if args.mode == "greenfield":
        bundle = {"repo_map": "", "files": [], "refs": []}
    else:
        bundle = build_bundle(args.repo, args.files, args.refs)
    print(
        f"[fanout] bundle: {len(bundle['files'])} files, "
        f"repo_map={'yes' if bundle['repo_map'] else 'no'}",
        file=sys.stderr,
    )

    if args.plan_path:
        print(f"[fanout] using plan from {args.plan_path}", file=sys.stderr)
        try:
            obj = json.loads(pathlib.Path(args.plan_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[fanout] error reading plan: {exc}", file=sys.stderr)
            return 1
        try:
            p = validate_plan(obj, args.command, args.n, args.mode, bundle)
        except PlanValidationError as e:
            print("[fanout] plan validation failed:", file=sys.stderr)
            for line in e.errors:
                print(f"  - {line}", file=sys.stderr)
            return 2
    else:
        print("[fanout] calling planner...", file=sys.stderr)
        t0 = time.monotonic()
        try:
            p = await plan(args.command, args.n, args.mode, bundle, timeout=args.timeout)
        except PlanParseError as e:
            print(f"[fanout] planner output unparseable: {e}", file=sys.stderr)
            return 3
        except PlanValidationError as e:
            print("[fanout] plan validation failed:", file=sys.stderr)
            for line in e.errors:
                print(f"  - {line}", file=sys.stderr)
            return 2
        except (BackendError, BackendTimeout) as e:
            print(f"[fanout] backend error: {e}", file=sys.stderr)
            return 3
        print(
            f"[fanout] planner returned in {time.monotonic() - t0:.1f}s",
            file=sys.stderr,
        )

    print("[fanout] validating plan... OK", file=sys.stderr)
    _print_plan_summary(p)

    if args.out_plan:
        pathlib.Path(args.out_plan).write_text(json.dumps(p, indent=2))
        print(f"[fanout] wrote plan to {args.out_plan}", file=sys.stderr)

    if args.dry_run:
        print("[fanout] dry-run: not dispatching workers", file=sys.stderr)
        print(json.dumps(p, indent=2))
        return 0

    print(f"[fanout] dispatching {len(p['subtasks'])} workers...", file=sys.stderr)
    coros = [run_worker(s, bundle, args.repo, timeout=args.timeout) for s in p["subtasks"]]
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    results: List[dict] = []
    failed = 0
    for sub, r in zip(p["subtasks"], raw_results):
        if isinstance(r, Exception):
            failed += 1
            results.append(
                {
                    "id": sub["id"],
                    "title": sub["title"],
                    "output": f"[ERROR] {type(r).__name__}: {r}",
                    "error": True,
                }
            )
        else:
            results.append(r)
    print(
        f"[fanout] {len(results) - failed} ok, {failed} failed",
        file=sys.stderr,
    )

    if args.no_reducer:
        for r in results:
            print(f"\n=== W{r['id']} — {r['title']} ===\n")
            print(r["output"])
        return 0 if failed == 0 else 3

    print("[fanout] reducing...", file=sys.stderr)
    try:
        final = await reduce_(args.command, p, results)
    except (BackendError, BackendTimeout) as e:
        print(f"[fanout] reducer failed: {e}", file=sys.stderr)
        return 3
    print(final)
    return 0 if failed == 0 else 3


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
