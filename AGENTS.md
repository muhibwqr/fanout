# fanout: notes for coding agents

- Python 3.9+, stdlib only. No runtime pip dependencies. `pytest` is the only dev dependency.
- Tests: `python3 -m pytest -q`. Smoke test: `./smoke.sh`.
- Layout:
  - `fanout.py`: CLI plus the core API (`make_plan`, `launch`, `results`, `handle_request`)
  - `agents.py`: agent registry, PATH detection, host detection
  - `jev.py`: OpenRouter Jev Decisions API client
  - `router.py`: width, agent choice, size, and boost mix per subtask (Jev with a heuristic fallback), plus waves
  - `planner.py`: writes subtask prompts (OpenRouter chat, then a local agent CLI, then a naive split)
  - `dispatch.py`: runs waves in tmux panes or headless and tracks state in `~/.cache/fanout/runs/<id>/`
  - `boost.py`: caveman, rtk, and headroom (install, status, per-worker env and prompt directives)
  - `mcp_server.py`: stdio MCP server
  - `install.py`: registers fanout with MCP clients
- Tests must not hit the network or need real agents. Point `FANOUT_CONFIG_DIR` at a tmp dir holding an `agents.json` that defines fake agents.
