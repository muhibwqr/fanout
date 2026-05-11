"""fanout — workstation bootstrap with declarative manifest, plan/apply/state/verify/rollback.

Subcommands:
  init       create ~/.fanout/workstation.yml from a template
  edit       open ~/.fanout/workstation.yml in $EDITOR
  plan       show diff: manifest vs installed
  apply      converge state to manifest
  state      show owned items
  verify     run sanity-check commands
  rollback   restore most recent snapshot
  ai         generate a manifest from a prose description via Claude
  claude     direct dispatch: fanout claude "task a" "task b" -> N panes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

import engine
import manifest as mf
import reports
import state as state_mod

DEFAULT_DIR = pathlib.Path(os.path.expanduser("~/.fanout"))
DEFAULT_MANIFEST = DEFAULT_DIR / "workstation.yml"
REPO_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATE_DEFAULT = REPO_DIR / "manifests" / "default.yml"
TEMPLATE_WORKSTATION = REPO_DIR / "manifests" / "workstation-port.yml"


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="fanout",
        description="Declarative workstation bootstrap. Manifest in, plan/apply out.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create ~/.fanout/workstation.yml from template.")
    p_init.add_argument(
        "--from-workstation",
        action="store_true",
        help="Use the LeuAlmeida/workstation port as the starting template.",
    )
    p_init.add_argument("--force", action="store_true")

    sub.add_parser("edit", help="Open manifest in $EDITOR.")

    p_plan = sub.add_parser("plan", help="Show diff: manifest vs installed.")
    p_plan.add_argument("--profile", default="default")
    p_plan.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    _add_html_flags(p_plan)

    p_apply = sub.add_parser("apply", help="Converge state to manifest.")
    p_apply.add_argument("--profile", default="default")
    p_apply.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--no-snapshot", action="store_true")
    p_apply.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    _add_html_flags(p_apply)

    p_state = sub.add_parser("state", help="Show owned items.")
    p_state.add_argument("subcmd", nargs="?", choices=["diff"], help='"diff" shows drift.')
    _add_html_flags(p_state)

    p_verify = sub.add_parser("verify", help="Run verify-section sanity checks.")
    p_verify.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    _add_html_flags(p_verify)

    p_roll = sub.add_parser("rollback", help="Restore most recent snapshot.")
    p_roll.add_argument("--dry-run", action="store_true")
    _add_html_flags(p_roll)

    p_ai = sub.add_parser("ai", help='Generate manifest from prose: fanout ai "set up Python ML rig"')
    p_ai.add_argument("description", help="Prose description of the desired workstation.")
    p_ai.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p_ai.add_argument("--auto", action="store_true", help="Skip the approval gate.")
    _add_html_flags(p_ai)

    p_claude = sub.add_parser(
        "claude",
        help='Direct dispatch: fanout claude "task a" "task b" -> N parallel claude -p panes.',
    )
    p_claude.add_argument("tasks", nargs="+")
    p_claude.add_argument("--no-tmux", action="store_true")
    p_claude.add_argument("--keep-tmux", action="store_true")
    p_claude.add_argument("--timeout", type=float, default=600.0)
    _add_html_flags(p_claude)

    return ap.parse_args(argv)


def _add_html_flags(p) -> None:
    """Default-on HTML report emission. --no-html to skip; --no-open to write but not open."""
    p.add_argument(
        "--no-html",
        dest="html",
        action="store_false",
        help="Skip the HTML report (terminal output only).",
    )
    p.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Write the HTML report but don't auto-open in the browser.",
    )
    p.set_defaults(html=True, open_browser=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ensure_dir() -> None:
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)


def _load_manifest(path: str):
    try:
        return mf.load(path)
    except FileNotFoundError:
        print(
            f"[fanout] no manifest at {path}\n"
            f"        run `fanout init` (or `fanout init --from-workstation`).",
            file=sys.stderr,
        )
        sys.exit(1)
    except mf.ManifestError as e:
        print(f"[fanout] manifest invalid:", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(2)


def _print_logs(logs: List[str]) -> None:
    for line in logs:
        print(line, file=sys.stderr)


# ---------------------------------------------------------------------------
# subcommand: init
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    _ensure_dir()
    if DEFAULT_MANIFEST.exists() and not args.force:
        print(f"[fanout] manifest already exists at {DEFAULT_MANIFEST}", file=sys.stderr)
        print("        re-run with --force to overwrite.", file=sys.stderr)
        return 1
    src = TEMPLATE_WORKSTATION if args.from_workstation else TEMPLATE_DEFAULT
    shutil.copy(src, DEFAULT_MANIFEST)
    print(f"[fanout] wrote {DEFAULT_MANIFEST}", file=sys.stderr)
    print(f"        template: {src.name}", file=sys.stderr)
    print(f"        next: `fanout plan` then `fanout apply`", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: edit
# ---------------------------------------------------------------------------


def cmd_edit(args) -> int:
    if not DEFAULT_MANIFEST.exists():
        print(f"[fanout] no manifest at {DEFAULT_MANIFEST}. run `fanout init`.", file=sys.stderr)
        return 1
    editor = os.environ.get("EDITOR", "vi")
    return subprocess.call([editor, str(DEFAULT_MANIFEST)])


# ---------------------------------------------------------------------------
# subcommand: plan
# ---------------------------------------------------------------------------


def cmd_plan(args) -> int:
    manifest = _load_manifest(args.manifest)
    p = engine.compute_plan(manifest, args.profile)
    print(engine.render_plan(p))
    if getattr(args, "html", True):
        html_text = reports.render_plan(args.profile, p)
        path = reports.emit("plan", html_text, open_browser=getattr(args, "open_browser", True))
        print(f"\n[fanout] report: {path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: apply
# ---------------------------------------------------------------------------


def cmd_apply(args) -> int:
    manifest = _load_manifest(args.manifest)
    p = engine.compute_plan(manifest, args.profile)
    print(engine.render_plan(p))
    if p.nothing_to_do:
        return 0

    if not args.yes and not args.dry_run:
        sys.stdout.write("\napply this plan? [y/N] ")
        sys.stdout.flush()
        ans = sys.stdin.readline().strip().lower()
        if ans not in ("y", "yes"):
            print("[fanout] aborted", file=sys.stderr)
            return 0

    s = state_mod.load()
    result = engine.apply_plan(
        p, s, state_dir=DEFAULT_DIR,
        snapshot_before=(not args.no_snapshot) and (not args.dry_run),
        dry_run=args.dry_run,
    )
    _print_logs(result.logs)

    print("\n[apply summary]", file=sys.stderr)
    for bucket, items in result.installed.items():
        if items:
            print(f"  installed {bucket}: {items}", file=sys.stderr)
    for bucket, items in result.removed.items():
        if items:
            print(f"  removed   {bucket}: {items}", file=sys.stderr)
    for bucket, items in result.failures.items():
        if items:
            print(f"  FAILED    {bucket}: {items}", file=sys.stderr)
    if getattr(args, "html", True):
        html_text = reports.render_apply(args.profile, p, result, dry_run=args.dry_run)
        path = reports.emit("apply", html_text, open_browser=getattr(args, "open_browser", True))
        print(f"\n[fanout] report: {path}", file=sys.stderr)
    return 0 if result.ok else 3


# ---------------------------------------------------------------------------
# subcommand: state / state diff
# ---------------------------------------------------------------------------


def cmd_state(args) -> int:
    s = state_mod.load()
    if args.subcmd == "diff":
        drift_reports = engine.drift_check(s)
        if not drift_reports:
            print("no drift")
            if getattr(args, "html", True):
                html_text = reports.render_state_diff([])
                path = reports.emit("state-diff", html_text, open_browser=getattr(args, "open_browser", True))
                print(f"[fanout] report: {path}", file=sys.stderr)
            return 0
        for r in drift_reports:
            print(f"[{r.bucket}]")
            for a in r.added:
                print(f"  + {a}  (installed off-manifest)")
            for rm in r.removed:
                print(f"  - {rm}  (in state but no longer installed)")
        if getattr(args, "html", True):
            html_text = reports.render_state_diff(drift_reports)
            path = reports.emit("state-diff", html_text, open_browser=getattr(args, "open_browser", True))
            print(f"\n[fanout] report: {path}", file=sys.stderr)
        return 2
    # plain `state`
    print(json.dumps(s.to_dict(), indent=2))
    if getattr(args, "html", True):
        html_text = reports.render_state(s)
        path = reports.emit("state", html_text, open_browser=getattr(args, "open_browser", True))
        print(f"\n[fanout] report: {path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: verify
# ---------------------------------------------------------------------------


def cmd_verify(args) -> int:
    manifest = _load_manifest(args.manifest)
    res = engine.verify(manifest)
    if not res.checks:
        print("(no verify checks configured)")
    else:
        for c in res.checks:
            mark = "✓" if c["ok"] else "✗"
            print(f"  {mark} {c['cmd']}")
            if not c["ok"] and c.get("stderr"):
                print(f"      {c['stderr']}")
    if getattr(args, "html", True):
        html_text = reports.render_verify(res)
        path = reports.emit("verify", html_text, open_browser=getattr(args, "open_browser", True))
        print(f"\n[fanout] report: {path}", file=sys.stderr)
    return 0 if res.ok else 3


# ---------------------------------------------------------------------------
# subcommand: rollback
# ---------------------------------------------------------------------------


def cmd_rollback(args) -> int:
    s = state_mod.load()
    res = engine.rollback(s, state_dir=DEFAULT_DIR, dry_run=args.dry_run)
    _print_logs(res.logs)
    if getattr(args, "html", True):
        html_text = reports.render_rollback(res)
        path = reports.emit("rollback", html_text, open_browser=getattr(args, "open_browser", True))
        print(f"\n[fanout] report: {path}", file=sys.stderr)
    return 0 if res.ok else 3


# ---------------------------------------------------------------------------
# subcommand: ai
# ---------------------------------------------------------------------------


async def _ai_generate(description: str, current_manifest: Optional[str]) -> str:
    from prompts import WORKSTATION_PLANNER_SYSTEM
    from workers import call_claude, parse_claude_envelope

    payload = json.dumps(
        {"description": description, "current_manifest_yaml": current_manifest or ""},
        indent=2,
    )
    raw = await call_claude(payload, system=WORKSTATION_PLANNER_SYSTEM, timeout=300.0)
    return parse_claude_envelope(raw).strip()


def cmd_ai(args) -> int:
    _ensure_dir()
    current = None
    if pathlib.Path(args.manifest).exists():
        current = pathlib.Path(args.manifest).read_text()

    print(f"[fanout ai] generating manifest for: {args.description}", file=sys.stderr)
    try:
        text = asyncio.run(_ai_generate(args.description, current))
    except Exception as e:
        print(f"[fanout ai] generation failed: {e}", file=sys.stderr)
        return 3

    # Strip code fences if Claude added them.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    # Validate.
    try:
        m = mf.loads(text)
    except mf.ManifestError as e:
        print("[fanout ai] generated manifest failed validation:", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        # Save it anyway for inspection.
        debug_path = DEFAULT_DIR / "ai-attempt.yml"
        debug_path.write_text(text)
        print(f"        full output saved to {debug_path}", file=sys.stderr)
        return 3

    print("\n--- generated manifest ---\n")
    print(text)
    print("\n--- end manifest ---\n")

    if args.auto:
        choice = "a"
    else:
        sys.stdout.write("[a]ccept / [e]dit / [r]egen / [q]uit > ")
        sys.stdout.flush()
        choice = sys.stdin.readline().strip().lower()

    if choice in ("a", "accept", ""):
        pathlib.Path(args.manifest).write_text(text)
        print(f"[fanout ai] wrote {args.manifest}", file=sys.stderr)
        if getattr(args, "html", True):
            html_text = reports.render_ai(args.description, text, accepted=True)
            path = reports.emit("ai", html_text, open_browser=getattr(args, "open_browser", True))
            print(f"[fanout] report: {path}", file=sys.stderr)
        return 0
    if choice in ("e", "edit"):
        with tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False) as tf:
            tf.write(text)
            tf_path = tf.name
        editor = os.environ.get("EDITOR", "vi")
        subprocess.call([editor, tf_path])
        edited = pathlib.Path(tf_path).read_text()
        pathlib.Path(args.manifest).write_text(edited)
        print(f"[fanout ai] wrote edited manifest to {args.manifest}", file=sys.stderr)
        return 0
    if choice in ("r", "regen"):
        print("[fanout ai] regen: re-run `fanout ai \"...\"` with a tighter description", file=sys.stderr)
        return 0
    print("[fanout ai] discarded", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: claude (direct dispatch)
# ---------------------------------------------------------------------------


async def _run_claude_dispatch(args) -> int:
    from workers import (
        call_claude,
        dispatch_tmux,
        kill_tmux_session,
        tmux_available,
        wait_for_done,
    )

    n = len(args.tasks)
    print(f"[fanout claude] dispatching {n} tasks", file=sys.stderr)
    use_tmux = (not args.no_tmux) and tmux_available()
    results: list = []
    if use_tmux:
        info = dispatch_tmux(list(args.tasks))
        print(f"[fanout claude] tmux session: {info['session']}", file=sys.stderr)
        print(f"  attach: tmux attach -t {info['session']}", file=sys.stderr)
        print(f"  logs:   {info['run_dir']}/W*.out", file=sys.stderr)
        results = await wait_for_done(info["pane_files"], timeout=args.timeout * 2)
        for r in results:
            print(f"\n=== W{r['id']} ===\n{r['output']}")
        if not args.keep_tmux:
            kill_tmux_session(info["session"])
    else:
        coros = [call_claude(t, timeout=args.timeout) for t in args.tasks]
        outs = await asyncio.gather(*coros, return_exceptions=True)
        for i, o in enumerate(outs, start=1):
            text = o if not isinstance(o, Exception) else f"[ERROR] {o}"
            print(f"\n=== W{i} ===\n{text}")
            results.append({"id": i, "output": text})
    if getattr(args, "html", True):
        html_text = reports.render_claude(results)
        path = reports.emit("claude", html_text, open_browser=getattr(args, "open_browser", True))
        print(f"\n[fanout] report: {path}", file=sys.stderr)
    return 0


def cmd_claude(args) -> int:
    return asyncio.run(_run_claude_dispatch(args))


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


HANDLERS = {
    "init": cmd_init,
    "edit": cmd_edit,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "state": cmd_state,
    "verify": cmd_verify,
    "rollback": cmd_rollback,
    "ai": cmd_ai,
    "claude": cmd_claude,
}


def main() -> int:
    args = parse_args()
    handler = HANDLERS[args.cmd]
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
