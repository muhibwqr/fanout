"""Repo map + file digest + bundle builder for the fanout planner."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
from typing import Iterable, List, Optional

SYMBOL_RE = re.compile(
    r"^\s*(?:async\s+)?(?:def|class|func|fn|export\s+(?:function|class|const))\s+[A-Za-z_][\w]*",
    re.M,
)

PRUNE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)

MAX_FILE_BYTES = 200_000


def repo_map(repo: str, depth: int = 3) -> str:
    """Tree view of repo, depth-bounded. Falls back to pure-python walker if `tree` missing."""
    try:
        out = subprocess.run(
            [
                "tree",
                "-L",
                str(depth),
                "-I",
                "node_modules|.git|dist|build|__pycache__",
                repo,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except FileNotFoundError:
        pass
    return _fallback_tree(repo, depth)


def _fallback_tree(root: str, depth: int = 3, max_entries: int = 400) -> str:
    """os.walk-based tree with in-place dir pruning and entry cap."""
    root_p = pathlib.Path(root)
    if not root_p.exists():
        return ""
    lines: List[str] = []
    root_str = str(root_p)
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel = os.path.relpath(dirpath, root_str)
        rel_depth = 0 if rel == "." else rel.count(os.sep) + 1
        # Prune in place — must mutate dirnames slice.
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS)
        if rel_depth >= depth:
            dirnames[:] = []
        indent = "  " * rel_depth
        if rel != ".":
            lines.append(f"{indent}{os.path.basename(dirpath)}/")
            if len(lines) >= max_entries:
                truncated = True
                break
        for f in sorted(filenames):
            lines.append(f"{'  ' * (rel_depth + (0 if rel == '.' else 1))}{f}")
            if len(lines) >= max_entries:
                truncated = True
                break
        if truncated:
            break
    if truncated:
        lines.append(f"... [truncated at {max_entries} entries] ...")
    return "\n".join(lines)


def file_digest(
    path: str,
    head: int = 200,
    tail: int = 100,
    repo: Optional[str] = None,
) -> dict:
    """Read file, return {path (repo-relative), abs_path, symbols, excerpt}."""
    abs_p = pathlib.Path(path)
    if repo and not abs_p.is_absolute():
        abs_p = pathlib.Path(repo) / path
    rel_path = path
    if repo:
        try:
            rel_path = str(abs_p.relative_to(pathlib.Path(repo)))
        except ValueError:
            rel_path = path

    if not abs_p.exists():
        return {
            "path": rel_path,
            "abs_path": str(abs_p),
            "symbols": [],
            "excerpt": "[FILE NOT FOUND]",
        }

    size = abs_p.stat().st_size
    if size > MAX_FILE_BYTES:
        return {
            "path": rel_path,
            "abs_path": str(abs_p),
            "symbols": [],
            "excerpt": f"[FILE TOO LARGE: {size} bytes — skipped]",
        }

    text = abs_p.read_text(errors="ignore")
    lines = text.splitlines()
    if len(lines) <= head + tail:
        body = lines
    else:
        body = lines[:head] + ["... [TRUNCATED] ..."] + lines[-tail:]
    symbols = [m.group(0).strip() for m in SYMBOL_RE.finditer(text)]
    return {
        "path": rel_path,
        "abs_path": str(abs_p),
        "symbols": symbols[:60],
        "excerpt": "\n".join(body),
    }


def _expand_globs(patterns: Iterable[str], repo: Optional[str]) -> List[str]:
    """Expand globs against `repo` if given, else cwd. Returns repo-relative or cwd-relative paths."""
    seen: List[str] = []
    if repo:
        base = pathlib.Path(repo)
        for pat in patterns:
            for p in sorted(base.glob(pat)):
                if p.is_file():
                    rel = str(p.relative_to(base))
                    if rel not in seen:
                        seen.append(rel)
    else:
        import glob as _glob

        for pat in patterns:
            for p in sorted(_glob.glob(pat, recursive=True)):
                if pathlib.Path(p).is_file() and p not in seen:
                    seen.append(p)
    return seen


def build_bundle(
    repo: Optional[str],
    files: Iterable[str],
    refs: Iterable[str],
    budget: int = 32_000,
) -> dict:
    """Build the planner's context bundle.

    Returns dict with keys: repo_map, files (list of digests), refs.
    Truncates excerpts if total bytes/4 (rough token estimate) exceeds budget.
    """
    bundle: dict = {
        "repo_map": repo_map(repo) if repo else "",
        "files": [],
        "refs": list(refs or []),
    }
    expanded = _expand_globs(list(files or []), repo)
    bundle["files"] = [file_digest(f, repo=repo) for f in expanded]
    total_chars = sum(len(d["excerpt"]) for d in bundle["files"])
    if total_chars // 4 > budget:
        for d in bundle["files"]:
            d["excerpt"] = d["excerpt"][:2000] + "\n... [BUDGET TRUNCATED] ..."
    return bundle
