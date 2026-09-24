"""`fanout install`: make fanout callable from every tool on this machine.

- symlinks `fanout` into ~/.local/bin
- registers the stdio MCP server (`fanout mcp`) with Claude Code, Codex, Gemini CLI,
  Cursor, Windsurf (whichever are present)
- drops the fanout skill into ~/.claude/skills and ~/.agents/skills
- runs `fanout boost` so caveman + rtk + headroom are ready
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

import boost as boostmod
from agents import detect

HERE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path.home()
ENTRY = str(HERE / "run.sh")
BIN_DIR = HOME / ".local" / "bin"

# client -> (detector, cli argv)  for clients that manage MCP config themselves
CLI_CLIENTS: Dict[str, Tuple[str, List[str]]] = {
    "claude": ("claude", ["claude", "mcp", "add", "--scope", "user", "fanout", "--", ENTRY, "mcp"]),
    "codex": ("codex", ["codex", "mcp", "add", "fanout", "--", ENTRY, "mcp"]),
    "gemini": ("gemini", ["gemini", "mcp", "add", "--scope", "user", "fanout", ENTRY, "mcp"]),
}
# client -> mcp.json-style config file ({"mcpServers": {...}})
JSON_CLIENTS: Dict[str, pathlib.Path] = {
    "cursor": HOME / ".cursor" / "mcp.json",
    "windsurf": HOME / ".codeium" / "windsurf" / "mcp_config.json",
}
SKILL_DIRS = [HOME / ".claude" / "skills" / "fanout", HOME / ".agents" / "skills" / "fanout"]


def merge_mcp_json(path: pathlib.Path, entry: Optional[dict] = None) -> dict:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
    data.setdefault("mcpServers", {})["fanout"] = entry or {"command": ENTRY, "args": ["mcp"]}
    return data


def plan_install(clients: Optional[List[str]] = None) -> List[Tuple[str, Callable[[], str]]]:
    want = set(clients) if clients else None
    steps: List[Tuple[str, Callable[[], str]]] = []

    def link() -> str:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        dst = BIN_DIR / "fanout"
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(ENTRY)
        return str(dst)
    steps.append((f"link {BIN_DIR / 'fanout'} -> {ENTRY}", link))

    for name, (exe, argv) in CLI_CLIENTS.items():
        if (want and name not in want) or not shutil.which(exe):
            continue

        def reg(argv=argv) -> str:
            proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            out = (proc.stdout + proc.stderr).strip()
            if proc.returncode != 0 and "already exists" not in out:
                raise RuntimeError(out[-300:])
            return out[-200:] or "ok"
        steps.append((f"mcp {name}: {' '.join(argv)}", reg))

    for name, path in JSON_CLIENTS.items():
        if (want and name not in want) or not (path.parent.exists() or (want and name in want)):
            continue

        def write(path=path) -> str:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(merge_mcp_json(path), indent=2) + "\n")
            return str(path)
        steps.append((f"mcp {name}: {path}", write))

    for d in SKILL_DIRS:
        if not d.parent.parent.exists():
            continue

        def skill(d=d) -> str:
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / "skills" / "fanout" / "SKILL.md", d / "SKILL.md")
            return str(d / "SKILL.md")
        steps.append((f"skill {d}", skill))
    return steps


def run(clients: Optional[str] = None, dry_run: bool = False, boost: bool = True) -> int:
    names = [c.strip() for c in clients.split(",")] if clients else None
    ok = True
    for label, fn in plan_install(names):
        if dry_run:
            print(f"[install] would: {label}")
            continue
        try:
            print(f"[install] {label}: {fn()}")
        except (OSError, RuntimeError) as e:
            ok = False
            print(f"[install] {label}: FAILED {e}")
    if boost:
        res = boostmod.enable(detect(), dry_run=dry_run, log=lambda m: print(m))
        ok = ok and all(r.get("ok", True) for r in res)
    if not dry_run and str(BIN_DIR) not in os.environ.get("PATH", ""):
        print(f"[install] add {BIN_DIR} to PATH to call `fanout` directly")
    return 0 if ok else 1
