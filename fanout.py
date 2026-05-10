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
from prompts import (
    INTENT_QUESTIONS_SYSTEM,
    INTENT_REFINE_SYSTEM,
    LENS_AUDITOR_REDUCER_SYSTEM,
    PLANNER_SYSTEM,
    REDUCER_SYSTEM,
    WORKER_LENS_INSTRUCTION,
    planner_schema,
)
from workers import (
    BackendError,
    BackendTimeout,
    call_claude,
    dispatch_tmux,
    kill_tmux_session,
    parse_claude_envelope,
    tmux_available,
    wait_for_done,
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
# Intent loop (interactive Q&A pre-planner)
# ---------------------------------------------------------------------------


async def intent_questions(command: str, hints: dict, *, timeout: float = 120.0) -> dict:
    """Ask the intent-questions agent for 2-3 clarifying questions."""
    payload = json.dumps({"command": command, "hints": hints}, indent=2)
    raw = await call_claude(payload, system=INTENT_QUESTIONS_SYSTEM, timeout=timeout)
    inner = parse_claude_envelope(raw)
    obj = extract_json(inner)
    if not isinstance(obj, dict) or "questions" not in obj:
        raise PlanParseError(f"intent agent returned bad shape: {obj!r}")
    return obj


async def intent_refine(
    command: str,
    questions: list,
    answers: list,
    *,
    timeout: float = 120.0,
) -> dict:
    """Send Q+A back to the refiner; get tightened {command, mode, n, files, refs}."""
    payload = json.dumps(
        {"command": command, "questions": questions, "answers": answers}, indent=2
    )
    raw = await call_claude(payload, system=INTENT_REFINE_SYSTEM, timeout=timeout)
    inner = parse_claude_envelope(raw)
    obj = extract_json(inner)
    required = {"command", "mode", "n", "files", "refs"}
    if not isinstance(obj, dict) or not required.issubset(obj):
        raise PlanParseError(f"intent refiner returned bad shape: {list(obj) if isinstance(obj, dict) else obj}")
    return obj


def _read_multiline_answer(prompt: str) -> str:
    """Read a single-line answer (or multi-line if user enters \\ at end)."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    return line.rstrip("\n")


async def run_intent_loop(args: argparse.Namespace) -> argparse.Namespace:
    """Drive the pre-planner Q&A. Returns args mutated with refined fields."""
    print("\n[intent] thinking about your command...", file=sys.stderr)
    hints = {
        "mode_hint": args.mode,
        "n_hint": args.n,
        "has_repo": bool(args.repo),
        "has_files": bool(args.files),
        "has_refs": bool(args.refs),
    }
    try:
        q_obj = await intent_questions(args.command, hints, timeout=args.timeout)
    except (PlanParseError, BackendError, BackendTimeout) as e:
        print(f"[intent] agent failed ({e}); skipping intent loop", file=sys.stderr)
        return args

    qs = q_obj.get("questions", [])
    guess = q_obj.get("guess", {})
    if guess:
        print(
            f"[intent] guess: mode={guess.get('mode')!r} n={guess.get('n')} — "
            f"{guess.get('rationale', '')}",
            file=sys.stderr,
        )

    answers = []
    print("\n--- intent Q&A — press enter to accept default ---\n")
    for q in qs:
        print(f"Q{q.get('id', '?')}: {q.get('text', '')}")
        if q.get("why"):
            print(f"   ({q['why']})")
        a = _read_multiline_answer("> ")
        answers.append({"id": q.get("id"), "text": q.get("text"), "answer": a})
    print()

    print("[intent] refining...", file=sys.stderr)
    try:
        refined = await intent_refine(args.command, qs, answers, timeout=args.timeout)
    except (PlanParseError, BackendError, BackendTimeout) as e:
        print(f"[intent] refiner failed ({e}); using original args", file=sys.stderr)
        return args

    print(f"\n[intent] refined: {refined.get('summary', '(no summary)')}", file=sys.stderr)
    print(
        f"[intent]   command={refined['command']!r}",
        file=sys.stderr,
    )
    print(
        f"[intent]   mode={refined['mode']} n={refined['n']} "
        f"files={refined['files']} refs={refined['refs']}",
        file=sys.stderr,
    )

    # User can override CLI defaults with refined values, but explicit CLI flags win.
    args.command = refined["command"]
    if refined["mode"] in VALID_MODES and not _flag_was_explicit("--mode"):
        args.mode = refined["mode"]
    if refined["n"] in VALID_N and not _flag_was_explicit("-n"):
        args.n = refined["n"]
    if refined["files"] and not args.files:
        args.files = list(refined["files"])
    if refined["refs"] and not args.refs:
        args.refs = list(refined["refs"])
    return args


def _flag_was_explicit(flag: str) -> bool:
    """Crude check: was this flag passed on the command line?"""
    return any(a == flag or a.startswith(flag + "=") for a in sys.argv[1:])


# ---------------------------------------------------------------------------
# Plan approval gate (interactive)
# ---------------------------------------------------------------------------


def plan_gate(p: dict) -> str:
    """Show plan, prompt user for accept/edit/regen/quit. Returns one of those four words."""
    import os
    import subprocess as _sp
    import tempfile

    print("\n=== PROPOSED PLAN ===\n", file=sys.stderr)
    print(json.dumps(p, indent=2))
    print("\n", file=sys.stderr)

    while True:
        sys.stdout.write("[plan] [a]ccept / [e]dit / [r]egen / [q]uit > ")
        sys.stdout.flush()
        choice = sys.stdin.readline().strip().lower()
        if choice in ("a", "accept", ""):
            return "accept"
        if choice in ("q", "quit", "abort"):
            return "quit"
        if choice in ("r", "regen", "regenerate"):
            return "regen"
        if choice in ("e", "edit"):
            editor = os.environ.get("EDITOR", "vi")
            with tempfile.NamedTemporaryFile(
                "w+", suffix=".json", delete=False
            ) as tf:
                tf.write(json.dumps(p, indent=2))
                path = tf.name
            try:
                _sp.run([editor, path], check=False)
                with open(path) as f:
                    edited = json.load(f)
                p.clear()
                p.update(edited)
                return "edited"
            except (json.JSONDecodeError, OSError) as e:
                print(f"[plan] edit failed: {e}", file=sys.stderr)
                continue
        print("(unrecognized; try a / e / r / q)", file=sys.stderr)


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
    lens: bool = False,
) -> dict:
    prompt = _build_worker_prompt(sub, bundle, lens=lens)
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


async def auditor_reduce(
    command: str, plan_dict: dict, bundle: dict, lens_report, *, strict: bool = False
) -> str:
    """Reducer that consumes a LensReport. Filters low-trust if --lens-strict."""
    from dataclasses import asdict as _asdict

    from lens import LensReport  # noqa: F401

    high = [_asdict(c) for c in lens_report.high_trust]
    med = [_asdict(c) for c in lens_report.med_trust]
    low = [] if strict else [_asdict(c) for c in lens_report.low_trust]
    suspect = [] if strict else [_asdict(c) for c in lens_report.suspect]
    workers = [_asdict(w) for w in lens_report.workers]
    scores_by_id = {k: _asdict(v) for k, v in lens_report.scores.items()}

    payload = json.dumps(
        {
            "command": command,
            "plan": plan_dict,
            "high_trust": high,
            "med_trust": med,
            "low_trust": low,
            "suspect": suspect,
            "workers": workers,
            "scores": scores_by_id,
        },
        indent=2,
    )
    return await call_claude(payload, system=LENS_AUDITOR_REDUCER_SYSTEM)


async def lens_retry_flagged(args, p, bundle, lens_report):
    """Re-dispatch flagged workers with steering instruction. One retry per worker."""
    from lens import lens_pass

    flagged_ids = [w.worker_id for w in lens_report.workers if w.flagged_for_retry]
    if not flagged_ids:
        return lens_report

    print(
        f"[fanout] lens-retry: re-running flagged workers {flagged_ids}", file=sys.stderr
    )
    flagged_subs = [s for s in p["subtasks"] if s["id"] in flagged_ids]
    steered_plan = {**p, "subtasks": []}
    for s in flagged_subs:
        steered = dict(s)
        steered["instructions"] = (
            "[LENS RETRY — your previous output was flagged as low-grounding-density. "
            "Re-do the task. For every specific claim, cite an exact file:line from "
            "the read_files. Do not theorize. If you cannot ground a claim, omit it.] "
            + s["instructions"]
        )
        steered_plan["subtasks"].append(steered)

    coros = [
        run_worker(s, bundle, args.repo, timeout=args.timeout, lens=True)
        for s in steered_plan["subtasks"]
    ]
    retry_outputs = await asyncio.gather(*coros, return_exceptions=True)
    retry_results = []
    for s, out in zip(steered_plan["subtasks"], retry_outputs):
        if isinstance(out, Exception):
            retry_results.append(
                {
                    "id": s["id"],
                    "title": s["title"],
                    "output": f"[ERROR] retry failed: {out}",
                    "error": True,
                }
            )
        else:
            retry_results.append(out)

    fresh_report = await lens_pass(
        p, bundle, retry_results, skip_reconstruction=getattr(args, "lens_fast", False)
    )
    # Merge: replace flagged workers with fresh entries.
    keep_workers = [w for w in lens_report.workers if w.worker_id not in flagged_ids]
    keep_claims = [c for c in lens_report.claims if c.worker_id not in flagged_ids]
    keep_scores = {
        k: v
        for k, v in lens_report.scores.items()
        if all(not k.startswith(f"W{wid}-") for wid in flagged_ids)
    }
    lens_report.workers = keep_workers + fresh_report.workers
    lens_report.claims = keep_claims + fresh_report.claims
    keep_scores.update(fresh_report.scores)
    lens_report.scores = keep_scores
    from lens import bucket_claims as _bucket

    high, med, low, suspect = _bucket(lens_report.claims, lens_report.scores)
    lens_report.high_trust = high
    lens_report.med_trust = med
    lens_report.low_trust = low
    lens_report.suspect = suspect
    return lens_report


def _build_worker_prompt(sub: dict, bundle: dict, *, lens: bool = False) -> str:
    needed = [f for f in bundle.get("files", []) if f["path"] in sub.get("read_files", [])]
    worker_ctx = {"files": needed, "refs": sub.get("refs", [])}
    payload = {"task": sub, "context": worker_ctx}
    if lens:
        payload["lens_instruction"] = WORKER_LENS_INSTRUCTION
    return json.dumps(payload, indent=2)


async def _dispatch_headless(args, p, bundle):
    print(f"[fanout] dispatching {len(p['subtasks'])} workers (headless)...", file=sys.stderr)
    coros = [run_worker(s, bundle, args.repo, timeout=args.timeout, lens=getattr(args, "lens", False)) for s in p["subtasks"]]
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
    return results, failed


async def _dispatch_tmux(args, p, bundle):
    print(f"[fanout] dispatching {len(p['subtasks'])} workers (tmux)...", file=sys.stderr)
    prompts = [_build_worker_prompt(s, bundle, lens=getattr(args, "lens", False)) for s in p["subtasks"]]
    info = dispatch_tmux(prompts, cwd=args.repo)
    session = info["session"]
    print(f"[fanout] tmux session: {session}", file=sys.stderr)
    print(f"[fanout]   attach: tmux attach -t {session}", file=sys.stderr)
    print(f"[fanout]   logs:   {info['run_dir']}/W*.out", file=sys.stderr)

    raw_results = await wait_for_done(
        info["pane_files"],
        poll_interval=2.0,
        timeout=args.timeout * 2,
    )

    by_id = {r["id"]: r for r in raw_results}
    results: List[dict] = []
    failed = 0
    for sub in p["subtasks"]:
        r = by_id.get(sub["id"], {})
        if r.get("error") or not r.get("output"):
            failed += 1
            results.append(
                {
                    "id": sub["id"],
                    "title": sub["title"],
                    "output": r.get("output") or "[ERROR] no output captured",
                    "error": True,
                }
            )
        else:
            results.append(
                {
                    "id": sub["id"],
                    "title": sub["title"],
                    "output": r["output"],
                    "error": False,
                }
            )

    if not args.keep_tmux:
        kill_tmux_session(session)
        print(f"[fanout] killed tmux session {session}", file=sys.stderr)
    else:
        print(
            f"[fanout] keeping tmux session {session} (--keep-tmux). "
            f"Kill with: tmux kill-session -t {session}",
            file=sys.stderr,
        )
    return results, failed


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
    ap.add_argument(
        "--auto",
        action="store_true",
        help="Skip the intent Q&A and the plan-approval gate. For scripted use.",
    )
    ap.add_argument(
        "--no-intent",
        action="store_true",
        help="Skip the intent Q&A only (plan gate still shown unless --auto).",
    )
    ap.add_argument(
        "--no-tmux",
        action="store_true",
        help="Force headless asyncio.gather dispatch, even when tmux is available.",
    )
    ap.add_argument(
        "--keep-tmux",
        action="store_true",
        help="Don't kill the tmux session after workers complete. Useful for inspection.",
    )
    ap.add_argument(
        "--lens",
        action="store_true",
        help="Run NLA-grounded quality filter on worker outputs before reducing.",
    )
    ap.add_argument(
        "--lens-retry",
        action="store_true",
        help="Re-run any worker the lens flags as fake-success (one retry max).",
    )
    ap.add_argument(
        "--lens-strict",
        action="store_true",
        help="With --lens: drop low-trust claims entirely (default is to surface in Suspect).",
    )
    ap.add_argument(
        "--lens-report",
        help="With --lens: write the full LensReport JSON to this path.",
    )
    ap.add_argument(
        "--lens-fast",
        action="store_true",
        help="Skip the per-claim reconstruction LLM call (cheaper; relies on ground-check + recurrence only).",
    )
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
    if not args.auto and not args.no_intent and not args.plan_path:
        try:
            args = await run_intent_loop(args)
        except KeyboardInterrupt:
            print("\n[fanout] aborted in intent loop", file=sys.stderr)
            return 130

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
        while True:
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
            if args.auto or args.dry_run:
                break
            choice = plan_gate(p)
            if choice in ("accept", "edited"):
                if choice == "edited":
                    try:
                        validate_plan(p, args.command, args.n, args.mode, bundle)
                    except PlanValidationError as e:
                        print("[fanout] edited plan failed validation:", file=sys.stderr)
                        for line in e.errors:
                            print(f"  - {line}", file=sys.stderr)
                        return 2
                break
            if choice == "regen":
                continue
            if choice == "quit":
                print("[fanout] aborted at plan gate", file=sys.stderr)
                return 0

    print("[fanout] validating plan... OK", file=sys.stderr)
    _print_plan_summary(p)

    if args.out_plan:
        pathlib.Path(args.out_plan).write_text(json.dumps(p, indent=2))
        print(f"[fanout] wrote plan to {args.out_plan}", file=sys.stderr)

    if args.dry_run:
        print("[fanout] dry-run: not dispatching workers", file=sys.stderr)
        print(json.dumps(p, indent=2))
        return 0

    use_tmux = (not args.no_tmux) and tmux_available()
    if use_tmux:
        results, failed = await _dispatch_tmux(args, p, bundle)
    else:
        results, failed = await _dispatch_headless(args, p, bundle)
    print(
        f"[fanout] {len(results) - failed} ok, {failed} failed",
        file=sys.stderr,
    )

    if args.no_reducer:
        for r in results:
            print(f"\n=== W{r['id']} — {r['title']} ===\n")
            print(r["output"])
        return 0 if failed == 0 else 3

    if getattr(args, "lens", False):
        print("[fanout] running lens pass...", file=sys.stderr)
        from dataclasses import asdict as _asdict

        from lens import lens_pass, report_to_dict

        try:
            lens_report = await lens_pass(
                p, bundle, results, skip_reconstruction=getattr(args, "lens_fast", False)
            )
        except (BackendError, BackendTimeout) as e:
            print(f"[fanout] lens pass failed: {e}", file=sys.stderr)
            return 3
        for w in lens_report.workers:
            print(
                f"[lens] W{w.worker_id} '{w.title[:40]}' high={w.high_count} "
                f"med={w.med_count} low={w.low_count} fake_success={w.fake_success_score} "
                f"flagged={'YES' if w.flagged_for_retry else 'no'}",
                file=sys.stderr,
            )
        if getattr(args, "lens_retry", False):
            try:
                lens_report = await lens_retry_flagged(args, p, bundle, lens_report)
            except (BackendError, BackendTimeout) as e:
                print(f"[fanout] lens retry failed: {e}", file=sys.stderr)
        if getattr(args, "lens_report", None):
            pathlib.Path(args.lens_report).write_text(
                json.dumps(report_to_dict(lens_report), indent=2)
            )
            print(f"[fanout] lens report → {args.lens_report}", file=sys.stderr)
        print("[fanout] auditor-reducing...", file=sys.stderr)
        try:
            final = await auditor_reduce(
                args.command,
                p,
                bundle,
                lens_report,
                strict=getattr(args, "lens_strict", False),
            )
        except (BackendError, BackendTimeout) as e:
            print(f"[fanout] auditor-reducer failed: {e}", file=sys.stderr)
            return 3
        print(final)
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
