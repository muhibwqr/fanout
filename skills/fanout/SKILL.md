---
name: fanout
description: Split a coding task across every coding agent installed on this machine (Claude Code, Codex, Gemini, Cursor, OpenCode, Aider, ...) and run them in parallel. Jev decides which agent gets each subtask and whether it runs in parallel or after another. Caveman, RTK, and Headroom are applied per subtask to save tokens. Use when a task has several independent parts, needs a broad audit/review, or would benefit from a second agent's opinion.
---

# fanout

Prefer the MCP tools if they are connected (`fanout_plan`, `fanout_run`, `fanout_status`,
`fanout_results`, `fanout_agents`). Otherwise use the CLI; every command below also takes `--json`.

## Workflow

1. See what's available: `fanout agents`
2. Preview the split and routing: `fanout plan "<task>" --dry-run`
3. Run it: `fanout plan "<task>" --detach --json` and keep the returned `run_id`
4. Poll: `fanout status <run_id> --json`, then read outputs: `fanout logs <run_id> --tail 4000`
5. Merge the results yourself and check that workers did not touch the same files.

If you already know the split, pass it yourself so fanout only routes it:
`fanout run "<subtask 1>" "<subtask 2>" ...` or MCP `fanout_run` with `subtasks`.

## Flags

- `--agents codex,gemini` limits which agents may be used; `--pin claude` sends everything to one agent
- `-n 4` forces the number of subtasks
- `--repo .` / `--paste` add shared context to every prompt
- `--no-tmux` runs headless (the default for MCP and `--json`); `--yolo` skips agent permission prompts
- `--no-caveman`, `--no-rtk`, `--no-headroom` turn individual boosts off

## Notes

- Workers run in the given `--cwd`. Plan subtasks with separate file ownership.
- Subtasks that depend on others run in a later wave and get their dependencies' output appended to their prompt.
- Without `OPENROUTER_API_KEY`, routing falls back to heuristics. `fanout doctor` shows which mode is active.
