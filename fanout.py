"""fanout — launch N parallel claude sessions in tmux panes.

Two commands:

  fanout claude N "prompt"                  N panes, all running the same prompt
  fanout claude N "p1" "p2" ... "pN"        N panes, one prompt each (count must match N)
  fanout plan "task description"            orchestrator-Claude decides N + prompts, then launches

Universal flags:
  --repo PATH         package repo contents as preamble to every prompt
  --paste             read context from stdin and prepend to every prompt
  --no-tmux           use asyncio.gather instead of tmux panes (output prints sequentially)
  --keep-tmux         don't kill the tmux session after panes finish
  --timeout SECONDS   per-pane timeout (default 600)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shlex
import sys
from typing import List, Optional, Tuple

from prompts import ORCHESTRATOR_SYSTEM
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


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="fanout",
        description="Launch N parallel Claude sessions in tmux panes.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_claude = sub.add_parser(
        "claude",
        help='Launch N claude sessions: fanout claude N "prompt" [more prompts...]',
    )
    p_claude.add_argument("n", type=int, help="Number of panes (1-10).")
    p_claude.add_argument(
        "prompts",
        nargs="+",
        help='1 prompt (replicated N times) OR exactly N prompts (one per pane).',
    )
    _add_common(p_claude)

    p_plan = sub.add_parser(
        "plan",
        help='Orchestrator-Claude decides N and the per-pane prompts, then launches them.',
    )
    p_plan.add_argument("task", help='Task description, in prose.')
    p_plan.add_argument(
        "-n",
        dest="n_hint",
        type=int,
        default=None,
        help="Hint to orchestrator on desired N (still its decision).",
    )
    _add_common(p_plan)
    p_plan.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the orchestrator's plan; do not launch panes.",
    )

    return ap.parse_args(argv)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--repo",
        help='Path to a repo or directory; its contents are packaged as preamble in every prompt.',
    )
    p.add_argument(
        "--paste",
        action="store_true",
        help="Read context from stdin and prepend to every prompt.",
    )
    p.add_argument(
        "--no-tmux",
        action="store_true",
        help="Use asyncio.gather instead of tmux panes (single-terminal output).",
    )
    p.add_argument(
        "--keep-tmux",
        action="store_true",
        help="Don't kill the tmux session after panes finish.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Per-pane timeout in seconds (default 600).",
    )


# ---------------------------------------------------------------------------
# Context: repo packaging + stdin paste
# ---------------------------------------------------------------------------


# Files and directories we never include in a repo dump.
_SKIP_DIRS = frozenset(
    {
        ".git", "node_modules", "dist", "build", "__pycache__",
        ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
        ".idea", ".vscode", ".next", ".cache", "target",
    }
)
_SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".class",
    ".o", ".a", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".DS_Store",
)
_MAX_FILE_BYTES = 100_000        # skip individual files bigger than this
_MAX_TOTAL_BYTES = 400_000       # truncate repo dump at this many bytes


def pack_repo(repo_path: str) -> str:
    """Walk repo and produce a text dump suitable for pasting into a Claude prompt.

    Output format:
        ===== file: path/to/file.py =====
        <content>
        ===== file: path/to/other.md =====
        <content>

    Skips binaries, prune dirs, oversized files. Truncates at _MAX_TOTAL_BYTES.
    """
    root = pathlib.Path(os.path.expanduser(repo_path)).resolve()
    if not root.exists():
        raise FileNotFoundError(f"--repo path does not exist: {repo_path}")
    if root.is_file():
        return _read_one(root, label=str(root.name))

    chunks: List[str] = []
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        # in-place prune
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            if any(fname.endswith(s) for s in _SKIP_SUFFIXES):
                continue
            fp = pathlib.Path(dirpath) / fname
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if size > _MAX_FILE_BYTES:
                continue
            rel = str(fp.relative_to(root))
            chunk = _read_one(fp, label=rel)
            if total + len(chunk) > _MAX_TOTAL_BYTES:
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        if truncated:
            break
    if truncated:
        chunks.append("\n===== [TRUNCATED: repo dump hit byte budget] =====\n")
    return "".join(chunks)


def _read_one(fp: pathlib.Path, label: str) -> str:
    try:
        text = fp.read_text(errors="ignore")
    except OSError:
        return ""
    return f"\n===== file: {label} =====\n{text}\n"


def read_stdin_paste() -> str:
    """Read all of stdin. Returns empty string if stdin is a tty."""
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def build_context_preamble(repo: Optional[str], paste: bool) -> str:
    parts: List[str] = []
    if repo:
        parts.append(f"<repo path=\"{repo}\">\n{pack_repo(repo)}\n</repo>\n")
    if paste:
        pasted = read_stdin_paste()
        if pasted.strip():
            parts.append(f"<paste>\n{pasted}\n</paste>\n")
    return "".join(parts)


def wrap_with_context(prompt: str, preamble: str) -> str:
    if not preamble:
        return prompt
    return preamble + "\n\n<task>\n" + prompt + "\n</task>\n"


# ---------------------------------------------------------------------------
# Subcommand: claude
# ---------------------------------------------------------------------------


def cmd_claude(args) -> int:
    n = args.n
    if n < 1 or n > 10:
        print(f"[fanout] N must be 1-10 (got {n})", file=sys.stderr)
        return 1
    prompts = list(args.prompts)
    if len(prompts) == 1:
        prompts = prompts * n
    elif len(prompts) != n:
        print(
            f"[fanout] got {len(prompts)} prompts but N={n}; "
            f"either supply 1 prompt (replicated) or exactly N prompts.",
            file=sys.stderr,
        )
        return 1

    try:
        preamble = build_context_preamble(args.repo, args.paste)
    except FileNotFoundError as e:
        print(f"[fanout] {e}", file=sys.stderr)
        return 1
    final_prompts = [wrap_with_context(p, preamble) for p in prompts]
    return asyncio.run(_dispatch(final_prompts, args))


# ---------------------------------------------------------------------------
# Subcommand: plan
# ---------------------------------------------------------------------------


def cmd_plan(args) -> int:
    try:
        preamble = build_context_preamble(args.repo, args.paste)
    except FileNotFoundError as e:
        print(f"[fanout] {e}", file=sys.stderr)
        return 1

    orchestrator_input = json.dumps(
        {
            "task": args.task,
            "context": preamble if preamble else None,
            "n_hint": args.n_hint,
        },
        indent=2,
    )
    print("[fanout plan] asking orchestrator to decompose...", file=sys.stderr)
    try:
        raw = asyncio.run(
            call_claude(orchestrator_input, system=ORCHESTRATOR_SYSTEM, timeout=args.timeout)
        )
    except (BackendError, BackendTimeout) as e:
        print(f"[fanout plan] orchestrator failed: {e}", file=sys.stderr)
        return 3
    inner = parse_claude_envelope(raw)
    try:
        obj = _extract_json(inner)
    except ValueError as e:
        print(f"[fanout plan] orchestrator output unparseable: {e}", file=sys.stderr)
        print(inner[:800], file=sys.stderr)
        return 3

    if not isinstance(obj, dict) or "n" not in obj or "tasks" not in obj:
        print(f"[fanout plan] bad orchestrator shape: {obj!r}", file=sys.stderr)
        return 3
    n = int(obj["n"])
    tasks = list(obj["tasks"])
    rationale = obj.get("rationale", "(no rationale)")
    if len(tasks) != n:
        print(
            f"[fanout plan] mismatch: orchestrator declared n={n} but produced {len(tasks)} tasks",
            file=sys.stderr,
        )
        return 3

    print(f"[fanout plan] N={n}  rationale: {rationale}", file=sys.stderr)
    for i, t in enumerate(tasks, start=1):
        excerpt = t[:120].replace("\n", " ")
        print(f"  W{i}: {excerpt}{'...' if len(t) > 120 else ''}", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(obj, indent=2))
        return 0

    final_prompts = [wrap_with_context(t, preamble) for t in tasks]
    return asyncio.run(_dispatch(final_prompts, args))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _dispatch(prompts: List[str], args) -> int:
    use_tmux = (not getattr(args, "no_tmux", False)) and tmux_available()
    print(f"[fanout] launching {len(prompts)} panes ({'tmux' if use_tmux else 'headless'})...", file=sys.stderr)

    if use_tmux:
        info = dispatch_tmux(prompts)
        session = info["session"]
        print(f"[fanout] tmux session: {session}", file=sys.stderr)
        print(f"  attach: tmux attach -t {session}", file=sys.stderr)
        print(f"  logs:   {info['run_dir']}/W*.out", file=sys.stderr)
        results = await wait_for_done(info["pane_files"], timeout=getattr(args, "timeout", 600.0) * 2)
        for r in results:
            print(f"\n=== W{r['id']} ===\n{r['output']}")
        if not getattr(args, "keep_tmux", False):
            kill_tmux_session(session)
            print(f"[fanout] killed tmux session {session}", file=sys.stderr)
        else:
            print(f"[fanout] keeping tmux session {session}", file=sys.stderr)
        return 0

    # Headless
    coros = [call_claude(p, timeout=getattr(args, "timeout", 600.0)) for p in prompts]
    outs = await asyncio.gather(*coros, return_exceptions=True)
    failed = 0
    for i, o in enumerate(outs, start=1):
        if isinstance(o, Exception):
            failed += 1
            print(f"\n=== W{i} ===\n[ERROR] {type(o).__name__}: {o}")
        else:
            print(f"\n=== W{i} ===\n{o}")
    return 0 if failed == 0 else 3


# ---------------------------------------------------------------------------
# JSON extractor (orchestrator output, tolerant of fences/preamble)
# ---------------------------------------------------------------------------


def _extract_json(raw: str):
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty orchestrator output")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for i, c in enumerate(s):
        if c not in "{[":
            continue
        sub = _balanced(s, i)
        if sub is None:
            continue
        try:
            return json.loads(sub)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON")


def _balanced(s: str, start: int) -> Optional[str]:
    o = s[start]
    c = "}" if o == "{" else "]"
    depth, in_str, esc = 0, False, False
    for j in range(start, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == o:
                depth += 1
            elif ch == c:
                depth -= 1
                if depth == 0:
                    return s[start : j + 1]
    return None


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


HANDLERS = {
    "claude": cmd_claude,
    "plan": cmd_plan,
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
