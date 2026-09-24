"""Minimal stdio MCP server (newline-delimited JSON-RPC 2.0, stdlib only).

Any MCP client (Claude Code, Codex, Cursor, Gemini CLI, Windsurf, Zed, Cline, ...) can
call fanout. The client's name from `initialize` is used as the host, so the caller is
never handed its own work unless it's the best fit.
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Callable, Dict, Optional

import boost as boostmod
import fanout
from agents import normalize_client

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "fanout", "version": "5.0.0"}

_TASK_PROPS = {
    "task": {"type": "string", "description": "What to get done. fanout splits it into subtasks."},
    "subtasks": {
        "type": "array",
        "description": "Optional: your own split. Each item {title, prompt, depends_on:[ids]}. "
                       "Skips fanout's planner; Jev still picks the agent for each.",
        "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "prompt": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "integer"}}},
            "required": ["prompt"]},
    },
    "n": {"type": "integer", "description": "Force the number of subtasks."},
    "agents": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these agents."},
    "pin": {"type": "string", "description": "Send every subtask to this agent."},
    "cwd": {"type": "string", "description": "Working directory for the agents (default: server cwd)."},
    "repo": {"type": "string", "description": "Pack this repo/file into every prompt."},
    "context": {"type": "string", "description": "Extra context prepended to every prompt."},
    "yolo": {"type": "boolean", "description": "Skip agent permission prompts."},
    "boost": {"type": "object", "description": "Turn boosts off, e.g. {\"headroom\": false}.",
              "properties": {"caveman": {"type": "boolean"}, "rtk": {"type": "boolean"},
                             "headroom": {"type": "boolean"}}},
}

TOOLS = [
    {"name": "fanout_agents", "description": "List coding agents installed on this machine, their strengths, "
     "and which host is calling.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fanout_plan", "description": "Dry run: split the task and show which agent Jev assigns to each "
     "subtask, with wave order and caveman/rtk/headroom mix. Runs nothing.",
     "inputSchema": {"type": "object", "properties": _TASK_PROPS}},
    {"name": "fanout_run", "description": "Split the task, route each subtask to the best installed agent, and "
     "run them in parallel. Returns a run_id immediately unless wait=true; poll fanout_status.",
     "inputSchema": {"type": "object", "properties": {**_TASK_PROPS,
                     "wait": {"type": "boolean", "description": "Block until all workers finish."},
                     "tmux": {"type": "boolean", "description": "Show workers in tmux panes."}}}},
    {"name": "fanout_status", "description": "State of a run (default: latest).",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}}},
    {"name": "fanout_results", "description": "Worker outputs of a run (default: latest).",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"},
                                                      "tail": {"type": "integer", "description": "last N chars each"}}}},
    {"name": "fanout_doctor", "description": "Check agents, Jev, planner, boosts, tmux.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fanout_boost", "description": "Install/wire caveman, rtk, headroom into every detected agent.",
     "inputSchema": {"type": "object", "properties": {"dry_run": {"type": "boolean"}}}},
]


class Server:
    def __init__(self) -> None:
        self.host: Optional[str] = None
        self.handlers: Dict[str, Callable[[dict], dict]] = {
            "fanout_agents": lambda a: fanout.agents_report(self.host),
            "fanout_plan": lambda a: fanout.handle_request({**a, "dry_run": True, "host": self.host}),
            "fanout_run": lambda a: fanout.handle_request({**a, "host": self.host}),
            "fanout_status": lambda a: fanout.results(a.get("run_id"), 0)["status"],
            "fanout_results": lambda a: fanout.results(a.get("run_id"), a.get("tail")),
            "fanout_doctor": lambda a: fanout.doctor_report(),
            "fanout_boost": lambda a: {"steps": boostmod.enable(fanout.detect(), dry_run=bool(a.get("dry_run")),
                                                                log=fanout.eprint)},
        }

    def handle(self, msg: dict) -> Optional[dict]:
        method, mid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if mid is None:
            return None
        if method == "initialize":
            self.host = normalize_client((params.get("clientInfo") or {}).get("name"))
            return self._ok(mid, {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            return self._ok(mid, self.call(params.get("name"), params.get("arguments") or {}))
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}

    def call(self, name: str, args: dict) -> dict:
        fn = self.handlers.get(name)
        if fn is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        try:
            out = fn(args)
            return {"content": [{"type": "text", "text": json.dumps(out, indent=2, default=str)}],
                    "isError": False}
        except SystemExit as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # noqa: BLE001 - surface any failure to the client
            fanout.eprint(traceback.format_exc())
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}

    @staticmethod
    def _ok(mid, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> int:
    srv = Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = srv.handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0
