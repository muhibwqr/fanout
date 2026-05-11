# fanout

> Launch N parallel Claude agents in tmux panes. One bash command. No install.

```sh
git clone https://github.com/muhibwqr/fanout
cd fanout
chmod +x run.sh
ln -sf "$(pwd)/run.sh" ~/.local/bin/fanout
```

That's it. `~/.local/bin` is on most PATHs. If not, run `./run.sh` directly.

Requires: `python3`, `claude` CLI on PATH, `tmux` (recommended; falls back to single-terminal headless if missing). No pip installs. No system packages. No `~/.fanout/` directory. Nothing to clean up.

---

## Two commands

### `fanout claude N "prompt"`

Launch N Claude sessions in N tmux panes side by side. Each one runs the same prompt:

```sh
fanout claude 2 "build a backend for a typing test app that will take tests and submit them"
```

Two panes spawn. Each runs `claude -p` with that exact prompt. You watch them work in parallel.

**Different prompts per pane** — pass exactly N prompts:

```sh
fanout claude 3 "design the schema" "write the API" "write the frontend"
```

### `fanout plan "task description"`

Don't know how to split the work? Let Claude decide. The orchestrator reads your task, decides how many agents, writes one prompt per agent, then launches them:

```sh
fanout plan "build a typing-test app with a Go backend, Postgres, and a React frontend"
```

Output:
```
[fanout plan] asking orchestrator to decompose...
[fanout plan] N=4  rationale: split by stack layer for clean parallelism
  W1: Design the Postgres schema for users, tests, submissions...
  W2: Write a Go backend that serves the schema from W1...
  W3: Write a React frontend that talks to the W2 API...
  W4: Write deployment + docker-compose tying it together...
[fanout] launching 4 panes (tmux)...
[fanout] tmux session: fanout_a3b8c1
  attach: tmux attach -t fanout_a3b8c1
```

Add `--dry-run` to see the orchestrator's plan without launching.

---

## Paste a repo or context

Include repo contents as preamble in every prompt:

```sh
fanout claude 4 "audit each module for security issues" --repo ~/work/my-api
fanout plan "refactor this codebase to add caching" --repo .
```

Or pipe arbitrary context via stdin:

```sh
git diff HEAD~5 | fanout plan "review this diff for regressions" --paste
cat README.md | fanout claude 2 "rewrite this in two different voices" --paste
```

Repo packing skips `.git`, `node_modules`, binaries, oversized files. Caps the dump at ~400KB so it fits in the model's context cleanly.

---

## Flags

Both `claude` and `plan` accept:

| Flag | Effect |
|------|--------|
| `--repo PATH` | Pack repo contents as preamble in every prompt. |
| `--paste` | Read stdin and prepend to every prompt. |
| `--no-tmux` | Run sequentially in this terminal (asyncio.gather). |
| `--keep-tmux` | Don't kill the tmux session after panes finish. |
| `--timeout SECONDS` | Per-pane wall-clock timeout. Default 600. |

`plan` also has:
- `-n N` — hint the orchestrator about desired count (still its decision).
- `--dry-run` — print the plan; don't launch.

---

## How it works

```
              ┌────────────────────────────┐
              │ fanout claude N "prompt"   │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │ build N prompts            │
              │ (optionally wrap in repo/  │
              │  paste preamble)           │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │ tmux new-session + N-1     │
              │ split-windows; one pane    │
              │ per claude -p invocation   │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │ poll W*.done sentinels;    │
              │ collect W*.out; print      │
              │ aggregated output;         │
              │ kill session (or keep).    │
              └────────────────────────────┘
```

`fanout plan` adds one extra step at the front: a single `claude -p` call with an orchestrator system prompt that returns JSON `{n, rationale, tasks: [N prompts]}`. Then the dispatch is identical.

---

## Tests

```sh
python3 -m pytest -v
```

25 tests across argparse, repo packing, JSON extraction. Subprocess-free; <0.2s.

---

## Files

```
fanout/
  fanout.py          CLI: claude + plan subcommands, repo packing, dispatch
  workers.py         tmux session/pane spawning, async claude wrapper
  prompts.py         ORCHESTRATOR_SYSTEM (for `fanout plan`)
  run.sh             entrypoint (resolves symlink, exec python)
  tests/
    test_fanout.py
```

Five source files. No config. No state. No reports directory.

---

## License

MIT.

Inspired by the multi-agent dispatch pattern in
[LeuAlmeida/workstation](https://github.com/LeuAlmeida/workstation) and
the HTML communication style in
[Thariq's html-effectiveness](https://thariqs.github.io/html-effectiveness/).
