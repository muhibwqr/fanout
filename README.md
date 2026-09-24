# fanout v5

Split a coding task, let [Jev](https://openrouter.ai/typesafe/jev-1.13) pick the best **installed** coding agent for each piece, and run them in parallel, with [caveman](https://github.com/JuliusBrussee/caveman), [rtk](https://github.com/rtk-ai/rtk) and [headroom](https://github.com/headroomlabs-ai/headroom) cutting tokens on every worker.

You can call it from Claude Code, Codex, Cursor, Gemini CLI, Windsurf, Zed, a plain terminal, or any tool that speaks MCP or can run a shell command.

```
fanout plan "add OAuth login, tests for it, and update the docs"

[fanout] host=cursor  agents=claude,codex,gemini  jev=on
[fanout] width=3 (jev)  planner=openrouter  auth, tests, and docs split cleanly
  W1 wave1 claude  conf=0.81 size=medium  boost=caveman:full,headroom  [jev]  OAuth login flow
  W2 wave2 codex   conf=0.74 size=small   boost=caveman:ultra,rtk      [jev] after 1  tests for OAuth
  W3 wave1 gemini  conf=0.69 size=small   boost=caveman:lite           [jev]  update docs
```

## How it works

1. **Discover.** `agents.py` checks PATH for 12 agent CLIs (claude, codex, gemini, cursor-agent, opencode, aider, goose, qwen, amp, copilot, droid, crush), plus anything you add in `~/.config/fanout/agents.json`. It also detects which tool is calling fanout, from the MCP `clientInfo` or from env markers.
2. **Width.** Jev scores how many parallel agents the task deserves (`score` question).
3. **Plan.** A planner writes self-contained subtask prompts. It tries, in order: the calling host (when it passes `subtasks` over MCP), an OpenRouter chat model, a local agent CLI in read-only mode, and finally a naive split.
4. **Route.** For each subtask, Jev answers typed questions:
   - `agent` (`choice` over the installed agents, described by their strengths)
   - `independent` (`noul`)
   - `size` (`score`, which sets the timeout)
   - `caveman` level (`score`: lite, full, or ultra)
   - `shell_heavy` (`noul`, turns on rtk)
   - `context_heavy` (`noul`, turns on headroom)

   Close calls get balanced across agents. Dependent subtasks run in later waves and receive their upstream output.
5. **Run.** Each worker runs its agent headlessly (`claude -p`, `codex exec`, `gemini -p`, `cursor-agent -p`, ...), either in tmux panes (one window per wave) or detached/headless. State is kept in `~/.cache/fanout/runs/<id>/`.

Without an OpenRouter key, every Jev step falls back to deterministic heuristics, and the plan shows `[heuristic]` instead of `[jev]`.

## Install

```bash
git clone https://github.com/muhibwqr/fanout && cd fanout
./run.sh install          # link ~/.local/bin/fanout, register MCP in every detected client, run `fanout boost`
export OPENROUTER_API_KEY=sk-or-...   # or: echo sk-or-... > ~/.config/fanout/openrouter_key
fanout doctor
```

`fanout install` registers the stdio MCP server `fanout mcp` like this:

| client | how |
|---|---|
| Claude Code | `claude mcp add --scope user fanout -- run.sh mcp` |
| Codex | `codex mcp add fanout -- run.sh mcp` |
| Gemini CLI | `gemini mcp add --scope user fanout run.sh mcp` |
| Cursor | `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| anything else | point its MCP config at `run.sh mcp`, or shell out to `fanout exec` / `fanout ... --json` |

It also copies `skills/fanout/SKILL.md` into `~/.claude/skills` and `~/.agents/skills`.

## Boosts

`fanout boost` installs and wires all three tools. It is idempotent, and `--dry-run` shows what it would do.

- **caveman**: installed for every detected agent via `npx github:JuliusBrussee/caveman`. fanout also appends a caveman directive to each worker prompt at the level Jev picked, so it works even without the skill installed.
- **rtk**: `brew install rtk` (or cargo/curl), then `rtk init -g [--codex|--gemini|--agent cursor|...]` for each agent. Shell-heavy workers are told to prefix commands with `rtk`.
- **headroom**: `uv tool install "headroom-ai[all]"` (or pipx/pip). For context-heavy workers, fanout starts one shared `headroom proxy` on port 8787 and points `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at it.

You can turn any of them off per run with `--no-caveman`, `--no-rtk`, or `--no-headroom`.

## Commands

```
fanout plan "task" [-n N] [--max 6] [--planner auto|openrouter|naive|<agent>]
fanout run "p1" "p2" ...          # you split the task; Jev routes each piece
fanout <agent> N "prompt" ...     # N copies of one agent: claude, codex, gemini, cursor, ...
fanout agents | doctor | boost [--dry-run] [--status]
fanout status [RUN] | logs [RUN] [--tail N]
fanout exec < request.json        # {"task" | "prompts" | "subtasks", "n", "agents", "pin", "dry_run", "wait", ...}
fanout mcp                        # tools: fanout_agents, fanout_plan, fanout_run, fanout_status, fanout_results, fanout_doctor, fanout_boost
fanout install [--clients claude,cursor] [--dry-run]
```

Flags that work on every run command:

- `--repo PATH`, `--paste`: add shared context to every prompt
- `--agents a,b`, `--pin agent`: restrict agents or force one
- `--no-tmux`, `--keep-tmux`, `--detach`, `--json`
- `--timeout S`, `--cwd DIR`, `--yolo`, `--no-jev`

`--yolo` passes each agent's own skip-permissions flag. Without it, fanout uses the agent's safe edit mode, such as Claude's `acceptEdits` or Gemini's `auto_edit`.

## Custom agents

```json
// ~/.config/fanout/agents.json
{
  "myagent": {"bins": ["myagent"], "cmd": ["{bin}", "--print", "{prompt}"], "strengths": "what it's good at", "api": "openai"},
  "aider": null
}
```

Setting an agent to `null` removes it. Any field of a built-in agent can be overridden.

## Env

| var | default |
|---|---|
| `OPENROUTER_API_KEY` / `FANOUT_OPENROUTER_API_KEY` | none (heuristic routing) |
| `FANOUT_JEV_MODEL` | `typesafe/jev-1.13` |
| `FANOUT_PLANNER_MODEL` | `openai/gpt-4o-mini` |
| `FANOUT_HOST` | auto-detected |
| `FANOUT_CONFIG_DIR` | `~/.config/fanout` |
| `FANOUT_RUNS_DIR` | `~/.cache/fanout/runs` |
| `FANOUT_HEADROOM_PORT` | `8787` |

## Dev

Requires Python 3.9+ and uses only the stdlib. The tests need pytest.

```bash
python3 -m pytest -q   # no network, uses fake agents
./smoke.sh             # tests, every --help, plus an end-to-end run and an MCP handshake with fake agents
```
