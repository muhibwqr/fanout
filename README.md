# Operation Fanout (v0)

Local multi-agent task decomposition: a single command becomes N independent subtasks, dispatched to N parallel `claude -p` workers, then reduced.

Spec: `../multi_agent_fanout.html`. NLA grounding: https://www.anthropic.com/research/natural-language-autoencoders.

## Install

Stdlib-only at runtime. Tests need pytest.

```sh
chmod +x run.sh smoke.sh
python3 -m pip install --user pytest
```

## Default flow (interactive)

```sh
fanout "audit auth for security"
```

This triggers, in order:
1. **Intent Q&A**: a small LLM agent reads your command, prints 2-3 clarifying questions, you answer, the agent refines `command` / `mode` / `n` / `files` / `refs`.
2. **Planner**: produces the JSON plan.
3. **Plan gate**: shows the plan and prompts `[a]ccept / [e]dit / [r]egen / [q]uit`.  `e` opens `$EDITOR` on the plan JSON; `r` re-runs the planner.
4. **tmux dispatch**: spawns N visible panes in a detached tmux session. Each pane runs `claude -p` for one worker, writes output to `/tmp/fanout_<id>/W<n>.out`, touches a `.done` sentinel. Orchestrator polls.
5. **Reducer**: synthesises results per `merge_plan`.

Attach the tmux session in another terminal to watch workers live:

```sh
tmux attach -t fanout_<id>
```

(The orchestrator prints the exact attach command.)

## Quickstart

Three modes; pick whichever fits the work.

### Extend — work on an existing repo

```sh
./run.sh "audit auth layer for security, perf, DX" \
  --mode extend \
  --repo ~/work/api \
  --files "src/auth/**/*.py" "tests/auth/**/*.py" \
  -n 6
```

### Scratch — build new from references

```sh
./run.sh "build a CLI for tailing structured logs with jq-like filters" \
  --mode scratch \
  --refs https://github.com/aurora/lnav https://stedolan.github.io/jq \
  -n 4
```

### Greenfield — pure ideation

```sh
./run.sh "design a novel approach to interpretable agent memory" \
  --mode greenfield -n 8
```

## See the plan before workers run

```sh
./run.sh "..." --dry-run --out-plan /tmp/plan.json
# inspect /tmp/plan.json, edit if needed, then:
./run.sh "..." --plan /tmp/plan.json
```

`--dry-run` stops after the planner. `--plan <path>` skips the planner and runs workers from a hand-edited JSON file.

## CLI flags

| Flag | Meaning |
|------|---------|
| `-n {2,4,6,8,10}` | Number of workers. |
| `--mode {scratch,extend,greenfield}` | Task shape. |
| `--repo <path>` | Repo root (extend only). |
| `--files <glob...>` | Repo-relative glob patterns (extend/scratch). |
| `--refs <url-or-path...>` | Reference material (extend/scratch). |
| `--dry-run` | Build plan, validate, print, exit. |
| `--plan <path>` | Use a pre-written plan; skip planner. |
| `--out-plan <path>` | Write the validated plan to disk. |
| `--no-reducer` | Print raw worker outputs instead of synthesising. |
| `--timeout <sec>` | Per-call timeout (default 600s). |
| `--auto` | Skip intent Q&A and plan gate. For scripted use. |
| `--no-intent` | Skip the intent Q&A only; plan gate still shown. |
| `--no-tmux` | Force headless `asyncio.gather` dispatch. |
| `--keep-tmux` | Don't kill the tmux session after workers finish — useful for inspection. |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success. |
| 1 | User-input error (bad flags, missing repo, etc). |
| 2 | Plan failed validation. |
| 3 | Backend (claude) error or partial worker failure. |
| 130 | Ctrl-C. |

## Tests

```sh
python3 -m pytest tests/ -v
```

47 unit tests: bundle builder, repo-map fallback, file digests, robust JSON extraction, plan validation rules.

```sh
./smoke.sh
```

Three live `--dry-run` invocations against `claude -p` (one planner call each) covering all three modes. Output saved to `/tmp/fanout_smoke_*.json`.

## Validation rules (`validate_plan`)

- Schema: required keys, types, enum membership for `mode` / `strategy` / `merge_plan`.
- N: `plan.n == requested n` and `len(subtasks) == n`. Duplicate titles surface as a "downscale" hint.
- Mode contract: `extend` subtasks must have non-empty `read_files`; `scratch` / `greenfield` must have empty `read_files`.
- Bundle membership: every `read_files` entry in extend mode must appear in the planner's bundle.
- Overlap (`by_file` only): no pair of subtasks may share more than one file. `by_dimension` legitimately shares files; rule is strategy-aware.
- Subtask ids 1..N, no duplicates.
- Self-contained: instructions may not reference other workers' output (independence invariant).

All errors are aggregated and reported together, not first-failure-only.

## Design notes — NLA grounding

Per Anthropic's Natural Language Autoencoders work, LLM verbalisations are *thematically* faithful but drift on specifics. Translated:

- Trust planner's *strategy* and *titles* (themes).
- Don't trust planner's *file paths* — `_check_files_in_bundle` rejects hallucinated paths.
- The planner is invoked with `--json-schema`, locking thematic fields to enums while leaving free-form `instructions` open.

## Limits (v0)

- `claude -p` is invoked with `--tools ""` — workers cannot edit files. They return markdown only.
- No URL fetching for `--refs`. Refs are passed to the model as text.
- No retry on a failed worker; partial results are reduced and reported.
- N=10 against a small file set in `by_file` mode will fail validation (overlap rule). Use `by_dimension` or smaller N.
- No caching, no live tmux pane streaming, no Ollama backend (see roadmap in spec sec XI).

## Layout

```
fanout/
  fanout.py        orchestrator, planner call, validator, extract_json
  context.py       bundle builder, repo_map, file_digest
  prompts.py       PLANNER_SYSTEM, REDUCER_SYSTEM, PLANNER_SCHEMA
  workers.py       call_claude wrapper, envelope parser
  run.sh           entrypoint
  smoke.sh         3-mode dry-run smoke
  tests/
    test_context.py
    test_validate.py
    test_extract_json.py
    fixtures/sample_repo/
    sample_plans/*.json
```
