# fanout

> Declarative workstation bootstrap. Manifest in. Plan/apply/state/verify/rollback out — every command emits an HTML report and opens it in your browser.

Forked the tool inventory from [LeuAlmeida/workstation](https://github.com/LeuAlmeida/workstation) (MIT). Replaced the architecture.

The workstation repo is a 40 KB one-shot shell script: `sh startup.sh` and pray. Re-run = re-install collisions. Step 43 fails = half-configured machine. No idempotence, no plan/apply, no state, no rollback, no drift detection, no profiles, no verification.

fanout fixes all of that. The tool list survives. The architecture is Terraform-for-laptop.

## Install

```sh
git clone https://github.com/muhibwqr/fanout
cd fanout
chmod +x run.sh
ln -sf "$(pwd)/run.sh" ~/.local/bin/fanout    # or anywhere on PATH
python3 -m pip install --user pyyaml
```

Tests: `python3 -m pytest -v`.

## HTML-first communication

Inspired by Thariq's [Unreasonable Effectiveness of HTML](https://thariqs.github.io/html-effectiveness/). Every fanout command writes a self-contained HTML artifact to `~/.fanout/reports/<timestamp>-<cmd>.html` and (on macOS) auto-opens it in the default browser.

- **Information density**: tables, color, summary cards, per-bucket grouping that text cannot do legibly.
- **Easy to share**: upload to S3 or pass the file directly. No "read this 400-line bash log".
- **Stay in the loop**: you actually read the plan before applying. Drift is visible at a glance.

Default: HTML on, browser auto-opens. Opt out per-command:
- `--no-open` — write the report, don't open.
- `--no-html` — terminal output only, no report.

## First run

```sh
fanout init                          # writes ~/.fanout/workstation.yml (the default manifest)
fanout init --from-workstation       # alternative: ports LeuAlmeida/workstation's full inventory
fanout plan                          # shows + (install) / - (remove) / = (keep) per bucket
fanout apply                         # converges your machine to the manifest
fanout state                         # shows what fanout owns
fanout verify                        # runs the manifest's `verify.checks` commands
```

## Day after

```sh
fanout state diff                    # detect drift since last apply
brew install fzf                     # you install something off-manifest
fanout state diff                    # → exits 2, reports + fzf as drift
fanout edit                          # opens $EDITOR on the manifest; add fzf
fanout apply                         # idempotent; nothing to install (already there); state updated
fanout rollback                      # undoes last apply via the snapshot taken before it
```

## AI mode

```sh
fanout ai "set up a Python ML rig with PyTorch, Jupyter, and Docker"
```

Claude generates the manifest. You see it. `[a]ccept / [e]dit / [r]egen / [q]uit`. On accept it writes to `~/.fanout/workstation.yml`. Then `fanout apply` like usual.

Requires the `claude` CLI on PATH (used as a subprocess; no API key handling).

## Subcommands

| Command | What it does |
|---------|--------------|
| `fanout init [--from-workstation] [--force]` | Write `~/.fanout/workstation.yml` from a template. |
| `fanout edit` | `$EDITOR` on the manifest. |
| `fanout plan [--profile X] [--manifest PATH]` | Diff: manifest vs installed. Pure read; no side effects. |
| `fanout apply [--profile X] [--dry-run] [--no-snapshot] [--yes]` | Converge to manifest. Snapshot first by default. |
| `fanout state` | Dump owned items as JSON. |
| `fanout state diff` | Detect drift between owned set and reality. Exit 2 on drift. |
| `fanout verify` | Run sanity-check commands from manifest's `verify.checks`. |
| `fanout rollback [--dry-run]` | Restore the most recent snapshot. |
| `fanout ai "<description>"` | Claude generates the manifest from prose. |
| `fanout claude "task" "task" ...` | Bonus: dispatch N tasks to N parallel `claude -p` sessions in tmux panes. |

## Manifest format

```yaml
version: 1

profiles:
  default: [base, web, cloud]
  python-ml: [base, python, ml]

modules:
  base:
    brew: [git, jq, ripgrep, fzf]
  web:
    brew: [node]
    cask: [visual-studio-code]
    npm_global: [typescript, yarn]
  cloud:
    brew: [awscli, kubectl]
    cask: [docker]
  python:
    brew: [python]
    pip: [poetry, ruff]
    curl:
      - name: nvm
        marker: "~/.nvm/nvm.sh"
        install: "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash"

settings:
  apply:
    parallelism: 4
    timeout: 900
    snapshot_before: true
  verify:
    checks:
      - cmd: "git --version"
      - cmd: "node --version"
```

**Buckets**: `brew`, `cask`, `npm_global`, `pip`, `curl`. Each maps to an adapter. Adding a new backend = one ~60 LOC file in `adapters/`.

**Curl items**: for tools that aren't in a package manager (NVM, Oh-My-Zsh, Docker Compose). Idempotent via a `marker` file/dir.

## Adapters

Five built in:

- `brew` — `brew install/uninstall`, formulas
- `cask` — `brew install --cask`, GUI apps
- `npm_global` — `npm install -g`
- `pip` — `pip install --user`
- `curl` — marker-file-gated shell installer for third-party scripts

Each implements `list_installed()`, `install(items)`, `uninstall(items)`. Easy to add more (`apt`, `cargo`, `gem`, `mise`, etc).

## Tests

```sh
python3 -m pytest -v
```

Currently 42 unit tests across manifest, state, adapters. Subprocess fully mocked so tests stay offline.

## Why this is 10x better than `workstation`

| | workstation | fanout |
|---|---|---|
| Source format | 40 KB bash script | ~70 line YAML manifest |
| Idempotence | No | Yes |
| Plan before apply | No | `fanout plan` |
| Profiles | No (fork the script) | `--profile python-ml` |
| State tracking | None | `~/.fanout/state.json` |
| Drift detection | None | `fanout state diff` |
| Rollback | None | `fanout rollback` |
| Verify install actually works | None | `fanout verify` |
| AI manifest generation | None | `fanout ai "..."` |
| Visible parallel installs | None | tmux pane dispatch (planned for `apply --tmux`) |
| New adapter cost | Edit shell script | One ~60 LOC `adapters/X.py` |

## Layout

```
fanout/
  fanout.py               # subcommand router
  manifest.py             # YAML parse + validate + profile resolve + diff
  state.py                # state file IO + snapshots + drift detection
  engine.py               # plan / apply / verify / rollback orchestrator
  workers.py              # tmux dispatch (used by `fanout claude` + `apply --tmux`)
  prompts.py              # AI-mode system prompt for `fanout ai`
  adapters/
    base.py               # Adapter base class + InstallResult
    brew.py               # brew formula + cask
    npm.py                # npm -g
    pip.py                # pip --user
    curl_script.py        # marker-file-gated curl installs
  manifests/
    default.yml           # starter manifest
    workstation-port.yml  # LeuAlmeida/workstation's inventory, ported
  tests/
    test_manifest.py
    test_state.py
    test_adapters.py
  NOTICE.md               # attribution to upstream
  LICENSE                 # MIT
```

## Attribution

Tool inventory in `manifests/workstation-port.yml` derived from
[LeuAlmeida/workstation](https://github.com/LeuAlmeida/workstation) (MIT).
See `NOTICE.md`.

## License

MIT.
