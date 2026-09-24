"""Agent registry + detection.

Every coding-agent CLI fanout knows how to drive headlessly. `detect()` returns
the ones installed on this machine; `detect_host()` guesses which tool is
calling fanout right now (Claude Code, Cursor, Codex, ...).

Users can add or override agents in ~/.config/fanout/agents.json:

    {"myagent": {"bins": ["myagent"], "cmd": ["myagent", "--print", "{prompt}"],
                 "strengths": "what it is good at"}}
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

CONFIG_DIR = pathlib.Path(os.environ.get("FANOUT_CONFIG_DIR", "~/.config/fanout")).expanduser()


@dataclass
class Agent:
    name: str
    bins: List[str]
    cmd: List[str]
    strengths: str
    yolo: List[str] = field(default_factory=list)
    safe: List[str] = field(default_factory=list)
    api: str = "anthropic"
    rtk_flag: Optional[List[str]] = None
    caveman_id: Optional[str] = None
    binary: Optional[str] = None

    def argv(self, prompt: str, *, yolo: bool = False) -> List[str]:
        exe = self.binary or self.bins[0]
        out = [exe if a == "{bin}" else a for a in self.cmd]
        out = [prompt if a == "{prompt}" else a for a in out]
        return out + (self.yolo if yolo else self.safe)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cmd")
        d.pop("yolo")
        d.pop("safe")
        return d


# Headless invocation for each known CLI. `{bin}` is the resolved binary,
# `{prompt}` the task. `yolo` flags skip permission prompts (opt-in); `safe`
# flags are used otherwise.
BUILTIN: Dict[str, dict] = {
    "claude": {
        "bins": ["claude"],
        "cmd": ["{bin}", "-p", "{prompt}"],
        "safe": ["--permission-mode", "acceptEdits"],
        "yolo": ["--dangerously-skip-permissions"],
        "strengths": "Claude Code. Best all-round multi-file coding, refactors, "
                     "architecture, careful edits across a repo, writing tests.",
        "api": "anthropic",
        "rtk_flag": [],
        "caveman_id": "claude-code",
    },
    "codex": {
        "bins": ["codex"],
        "cmd": ["{bin}", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"],
        "yolo": ["--dangerously-bypass-approvals-and-sandbox"],
        "strengths": "OpenAI Codex CLI. Strong at backend logic, algorithms, scripts, "
                     "shell/devops work, debugging failing tests, precise single-module changes.",
        "api": "openai",
        "rtk_flag": ["--codex"],
        "caveman_id": "codex",
    },
    "gemini": {
        "bins": ["gemini"],
        "cmd": ["{bin}", "-p", "{prompt}"],
        "safe": ["--approval-mode", "auto_edit"],
        "yolo": ["--yolo"],
        "strengths": "Gemini CLI. Huge context window: reading and summarising large "
                     "codebases, docs, research, audits, UI/frontend and multimodal work.",
        "api": "google",
        "rtk_flag": ["--gemini"],
        "caveman_id": "gemini",
    },
    "cursor": {
        "bins": ["cursor-agent", "agent"],
        "cmd": ["{bin}", "-p", "{prompt}", "--output-format", "text"],
        "yolo": ["--force"],
        "strengths": "Cursor Agent CLI. Fast frontend/UI iteration, React/TypeScript "
                     "components, quick targeted edits.",
        "api": "openai",
        "rtk_flag": ["--agent", "cursor"],
        "caveman_id": "cursor",
    },
    "opencode": {
        "bins": ["opencode"],
        "cmd": ["{bin}", "run", "{prompt}"],
        "strengths": "opencode. Provider-agnostic coding agent; general coding, "
                     "cheap/open models for boilerplate and routine tasks.",
        "api": "openai",
        "caveman_id": "opencode",
    },
    "aider": {
        "bins": ["aider"],
        "cmd": ["{bin}", "--yes-always", "--no-auto-commits", "--message", "{prompt}"],
        "strengths": "aider. Surgical diff-based edits to a few known files, "
                     "small refactors, git-aware pair programming.",
        "api": "openai",
    },
    "goose": {
        "bins": ["goose"],
        "cmd": ["{bin}", "run", "-t", "{prompt}"],
        "strengths": "Block Goose. Automation, tool/MCP-heavy workflows, "
                     "environment setup and scripting.",
        "api": "openai",
        "caveman_id": "goose",
    },
    "qwen": {
        "bins": ["qwen"],
        "cmd": ["{bin}", "-p", "{prompt}"],
        "yolo": ["--yolo"],
        "strengths": "Qwen Code. Cheap high-throughput coding, boilerplate, "
                     "translations, bulk mechanical edits.",
        "api": "openai",
        "caveman_id": "qwen-code",
    },
    "amp": {
        "bins": ["amp"],
        "cmd": ["{bin}", "-x", "{prompt}"],
        "yolo": ["--dangerously-allow-all"],
        "strengths": "Sourcegraph Amp. Deep code search across big repos, "
                     "cross-cutting changes, code review.",
        "api": "anthropic",
        "caveman_id": "amp",
    },
    "copilot": {
        "bins": ["copilot"],
        "cmd": ["{bin}", "-p", "{prompt}"],
        "yolo": ["--allow-all-tools"],
        "strengths": "GitHub Copilot CLI. GitHub-centric work: PRs, issues, "
                     "Actions workflows, repo chores.",
        "api": "anthropic",
    },
    "droid": {
        "bins": ["droid"],
        "cmd": ["{bin}", "exec", "{prompt}"],
        "strengths": "Factory Droid. End-to-end feature work, migrations, "
                     "long autonomous tasks.",
        "api": "anthropic",
        "rtk_flag": ["--agent", "droid"],
        "caveman_id": "droid",
    },
    "crush": {
        "bins": ["crush"],
        "cmd": ["{bin}", "run", "{prompt}"],
        "strengths": "Charm Crush. Terminal-native general coding with any model.",
        "api": "openai",
        "caveman_id": "crush",
    },
}


def _load_overrides() -> Dict[str, dict]:
    p = CONFIG_DIR / "agents.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def registry() -> Dict[str, Agent]:
    specs = {k: dict(v) for k, v in BUILTIN.items()}
    for name, spec in _load_overrides().items():
        if spec is None:
            specs.pop(name, None)
            continue
        base = specs.get(name, {"bins": [name], "cmd": ["{bin}", "{prompt}"], "strengths": name})
        base.update(spec)
        specs[name] = base
    return {name: Agent(name=name, **spec) for name, spec in specs.items()}


def detect(only: Optional[List[str]] = None) -> Dict[str, Agent]:
    """Agents whose binary is on PATH (optionally restricted to `only`)."""
    found: Dict[str, Agent] = {}
    for name, agent in registry().items():
        if only and name not in only:
            continue
        for b in agent.bins:
            path = shutil.which(b)
            if path and _is_really(b, path):
                agent.binary = path
                found[name] = agent
                break
    return found


# Bin names too generic to trust without checking where they resolve.
_AMBIGUOUS_BINS = {"agent": "cursor"}


def _is_really(bin_name: str, path: str) -> bool:
    marker = _AMBIGUOUS_BINS.get(bin_name)
    return marker is None or marker in os.path.realpath(path).lower()


# Env markers set by each host when it runs a shell / MCP server.
_HOST_MARKERS = [
    ("claude", ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")),
    ("codex", ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_MANAGED_BY_NPM", "CODEX_HOME")),
    ("cursor", ("CURSOR_TRACE_ID", "CURSOR_AGENT")),
    ("gemini", ("GEMINI_CLI",)),
    ("opencode", ("OPENCODE",)),
    ("amp", ("AMP_THREAD_ID",)),
]

_CLIENT_ALIASES = {
    "claude-code": "claude", "claude": "claude", "codex": "codex", "codex-mcp-client": "codex",
    "cursor": "cursor", "cursor-vscode": "cursor", "gemini": "gemini", "gemini-cli-mcp-client": "gemini",
    "opencode": "opencode", "windsurf": "windsurf", "cline": "cline", "vscode": "vscode",
    "visual studio code": "vscode", "goose": "goose", "amp": "amp", "zed": "zed",
}


def normalize_client(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    key = name.strip().lower()
    for alias, canon in _CLIENT_ALIASES.items():
        if key == alias or key.startswith(alias):
            return canon
    return key


def detect_host(env: Optional[dict] = None, mcp_client: Optional[str] = None) -> Optional[str]:
    """Best-effort guess of the tool that invoked fanout."""
    if mcp_client:
        return normalize_client(mcp_client)
    env = os.environ if env is None else env
    if env.get("FANOUT_HOST"):
        return env["FANOUT_HOST"]
    for host, keys in _HOST_MARKERS:
        if any(env.get(k) for k in keys):
            return host
    tp = (env.get("TERM_PROGRAM") or "").lower()
    if "cursor" in tp:
        return "cursor"
    if tp == "vscode":
        return "vscode"
    return None
