"""Repo packing + stdin paste, shared by the CLI and the MCP server."""
from __future__ import annotations

import os
import pathlib
import sys
from typing import List, Optional

_SKIP_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", ".next", ".cache", "target",
})
_SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".class", ".o", ".a", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".tar",
    ".gz", ".bz2", ".xz", ".7z", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".DS_Store", ".lock",
)
_MAX_FILE_BYTES = 100_000
_MAX_TOTAL_BYTES = 400_000


def pack_repo(repo_path: str) -> str:
    root = pathlib.Path(os.path.expanduser(repo_path)).resolve()
    if not root.exists():
        raise FileNotFoundError(f"--repo path does not exist: {repo_path}")
    if root.is_file():
        return _read_one(root, label=root.name)
    chunks: List[str] = []
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            if fname.startswith(".") or fname.endswith(_SKIP_SUFFIXES):
                continue
            fp = pathlib.Path(dirpath) / fname
            try:
                if fp.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            chunk = _read_one(fp, label=str(fp.relative_to(root)))
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
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def build_context_preamble(repo: Optional[str], paste: bool, text: str = "") -> str:
    parts: List[str] = []
    if repo:
        parts.append(f"<repo path=\"{repo}\">\n{pack_repo(repo)}\n</repo>\n")
    if paste:
        pasted = read_stdin_paste()
        if pasted.strip():
            parts.append(f"<paste>\n{pasted}\n</paste>\n")
    if text.strip():
        parts.append(f"<context>\n{text}\n</context>\n")
    return "".join(parts)


def wrap_with_context(prompt: str, preamble: str) -> str:
    if not preamble:
        return prompt
    return preamble + "\n\n<task>\n" + prompt + "\n</task>\n"
