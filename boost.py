"""Token savers: caveman (what agents say), rtk (shell output they read),
headroom (everything else they read). fanout turns all three on by default.

`status()`  what is installed
`enable()`  install + wire each tool into every detected agent (idempotent)
`runtime()` per-worker env + prompt directives for a run
"""
from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from agents import Agent

HEADROOM_PORT = int(os.environ.get("FANOUT_HEADROOM_PORT", "8787"))
CAVEMAN_REF = os.environ.get("FANOUT_CAVEMAN_REF", "v2.5.0")
HOME = pathlib.Path.home()

CAVEMAN_RULES = {
    "lite": "Style: concise. No filler, pleasantries, hedging, or restating the task. "
            "Full sentences allowed.",
    "full": "Style: caveman mode. Drop articles and filler, use short fragments. "
            "Code, commands, paths, identifiers, and error text stay exact.",
    "ultra": "Style: ultra caveman. Minimum words, telegraphic. Prefer showing the diff/command "
             "over describing it. Code and errors stay exact.",
}
RTK_RULE = ("Shell: rtk is installed. Prefix noisy commands with `rtk` for compact output, e.g. "
            "`rtk git status`, `rtk git diff`, `rtk pytest`, `rtk npm test`, `rtk grep`, `rtk read FILE`.")

_CAVEMAN_DIRS = [
    HOME / ".claude" / "plugins",
    HOME / ".claude" / "skills",
    HOME / ".agents" / "skills",
    HOME / ".cursor" / "skills",
    HOME / ".codex" / "skills",
    HOME / ".gemini" / "extensions",
    HOME / ".config" / "opencode",
]


def _caveman_installed() -> Optional[str]:
    if shutil.which("caveman"):
        return shutil.which("caveman")
    for d in _CAVEMAN_DIRS:
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("caveman*"):
                return str(p)
        except OSError:
            continue
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def status() -> Dict[str, dict]:
    return {
        "caveman": {"installed": _caveman_installed() is not None, "where": _caveman_installed(),
                    "note": "prompt directive always applied, even if the skill is missing"},
        "rtk": {"installed": shutil.which("rtk") is not None, "where": shutil.which("rtk")},
        "headroom": {"installed": shutil.which("headroom") is not None, "where": shutil.which("headroom"),
                     "proxy_running": port_open(HEADROOM_PORT), "port": HEADROOM_PORT},
    }


# ---------------------------------------------------------------------------
# enable: install + wire into agents
# ---------------------------------------------------------------------------


Step = Tuple[str, List[str]]


def plan_enable(agents: Dict[str, Agent], only: Optional[List[str]] = None) -> List[Step]:
    """Commands that install/wire the boosts. Pure: no side effects."""
    want = set(only or ["caveman", "rtk", "headroom"])
    st = status()
    steps: List[Step] = []

    if "rtk" in want:
        if not st["rtk"]["installed"]:
            if shutil.which("brew"):
                steps.append(("rtk: install", ["brew", "install", "rtk"]))
            elif shutil.which("cargo"):
                steps.append(("rtk: install", ["cargo", "install", "--git", "https://github.com/rtk-ai/rtk"]))
            else:
                steps.append(("rtk: install", ["sh", "-c",
                              "curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh -o /tmp/rtk-install.sh && sh /tmp/rtk-install.sh"]))
        for name, a in agents.items():
            if a.rtk_flag is not None:
                steps.append((f"rtk: hook {name}", ["rtk", "init", "-g", *a.rtk_flag]))

    if "caveman" in want and shutil.which("npx"):
        steps.append(("caveman: install for every detected agent",
                      ["npx", "-y", f"github:JuliusBrussee/caveman#{CAVEMAN_REF}", "--", "--non-interactive"]))

    if "headroom" in want and not st["headroom"]["installed"]:
        pkg = "headroom-ai[all]"
        if shutil.which("uv"):
            steps.append(("headroom: install", ["uv", "tool", "install", "--python", "3.13", pkg]))
        elif shutil.which("pipx"):
            steps.append(("headroom: install", ["pipx", "install", pkg]))
        else:
            steps.append(("headroom: install", [sys.executable, "-m", "pip", "install", "--user", pkg]))
    return steps


def enable(agents: Dict[str, Agent], *, only: Optional[List[str]] = None, dry_run: bool = False,
           log=print) -> List[dict]:
    results = []
    for label, argv in plan_enable(agents, only):
        if dry_run:
            log(f"[boost] would run: {label}: {' '.join(argv)}")
            results.append({"step": label, "argv": argv, "ran": False})
            continue
        log(f"[boost] {label}: {' '.join(argv)}")
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
            ok = proc.returncode == 0
            tail = (proc.stdout + proc.stderr)[-400:]
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, tail = False, str(e)
        log(f"[boost]   -> {'ok' if ok else 'FAILED'}")
        results.append({"step": label, "argv": argv, "ran": True, "ok": ok, "tail": tail})
    return results


# ---------------------------------------------------------------------------
# runtime: per-run env + prompt directives
# ---------------------------------------------------------------------------


def ensure_headroom_proxy(log_dir: pathlib.Path, *, port: int = HEADROOM_PORT) -> bool:
    """Start one shared `headroom proxy` if headroom is installed and nothing is on the port."""
    if port_open(port):
        return True
    exe = shutil.which("headroom")
    if not exe:
        return False
    env = dict(os.environ)
    env.setdefault("HEADROOM_OUTPUT_SHAPER", "1")
    log = open(log_dir / "headroom.log", "ab")
    subprocess.Popen([exe, "proxy", "--port", str(port)], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    for _ in range(40):
        if port_open(port):
            return True
        time.sleep(0.25)
    return port_open(port)


def headroom_env(api: str, port: int = HEADROOM_PORT) -> Dict[str, str]:
    base = f"http://127.0.0.1:{port}"
    if api == "anthropic":
        return {"ANTHROPIC_BASE_URL": base}
    if api == "openai":
        return {"OPENAI_BASE_URL": f"{base}/v1"}
    return {}


def directives(boost: dict, *, rtk_available: bool) -> List[str]:
    lines: List[str] = []
    level = boost.get("caveman")
    if level in CAVEMAN_RULES:
        lines.append(CAVEMAN_RULES[level])
    if boost.get("rtk") and rtk_available:
        lines.append(RTK_RULE)
    return lines


def decorate(prompt: str, boost: dict, *, rtk_available: bool) -> str:
    lines = directives(boost, rtk_available=rtk_available)
    if not lines:
        return prompt
    return prompt.rstrip() + "\n\n<fanout-boost>\n" + "\n".join(lines) + "\n</fanout-boost>\n"
