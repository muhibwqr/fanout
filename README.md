# fanout

> One YAML file. Five commands. Your dev machine, declarative, idempotent, rollback-able, with HTML reports.

```sh
fanout init                                  # write ~/.fanout/workstation.yml
fanout plan "set up a Python ML rig"         # ask Claude for a setup plan (HTML)
fanout plan                                  # diff manifest vs your machine (HTML)
fanout apply --tmux                          # install in parallel tmux panes
fanout rollback                              # undo last apply
```

Every command opens an HTML report in your browser. You stay in the loop. You can `Cmd+P` and find any past run in `~/.fanout/reports/`.

Forked from [LeuAlmeida/workstation](https://github.com/LeuAlmeida/workstation) (MIT). Same tool inventory. Different fundamentals.

---

## Why it exists

Workstation, `dotbot`, every "set up my mac" repo on GitHub — they're shell scripts. They install 50 tools top-to-bottom. They have these problems:

1. **Not idempotent.** Re-running re-installs. Output undefined.
2. **No preview.** You can't see what they'll do before they do it.
3. **No rollback.** Step 43 fails → wrecked machine.
4. **No state.** Three months later, you can't tell what the script installed vs what you did.
5. **No drift detection.** You install something off-script → next re-run clobbers your state.
6. **No selection.** All-or-nothing. Want only AWS + Node? Fork and edit.
7. **No verify.** Did `docker ps` actually work? You find out when you try to use it.

`fanout` fixes all of that. The Terraform pattern (declarative spec → plan/apply/state) applied to your laptop.

---

## Install (30 seconds)

```sh
git clone https://github.com/muhibwqr/fanout
cd fanout
chmod +x run.sh
ln -sf "$(pwd)/run.sh" ~/.local/bin/fanout
python3 -m pip install --user pyyaml
```

That's it. `python3` + `pyyaml` are the only runtime deps. `tmux` is optional but recommended.

---

## How to use it

### Day one — declare what you want

```sh
fanout init
```

Writes `~/.fanout/workstation.yml` from a starter template. Open it:

```sh
fanout edit
```

Edit it. Add the tools you want. The format is dead-simple YAML. A typical entry:

```yaml
version: 1

profiles:
  default: [base, web, cloud]

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

settings:
  apply:
    snapshot_before: true
  verify:
    checks:
      - cmd: "git --version"
      - cmd: "node --version"
```

Three things to know:
- **Profiles** compose **modules**. Pick a profile with `--profile X`.
- **Modules** group tools by **bucket** (which package manager handles them).
- **Buckets** today: `brew`, `cask`, `npm_global`, `pip`, `curl`. Each is one adapter file.

### Day one — see what would happen

```sh
fanout plan
```

Opens an HTML report in your browser. `+green` = will install. `-red` = will uninstall (it's in your installed set but not in the manifest). `=muted` = unchanged.

**This is the moment workstation taught us was missing.** You read the plan before any side effect happens. You change your mind. You edit the manifest. You re-plan. Then apply.

### Day one — converge

```sh
fanout apply --tmux
```

Snapshot taken before apply. Then one tmux pane per adapter batch: `brew install` in one pane, `npm install -g` in another, `pip install --user` in a third. You watch progress side by side. Pane stays open after; `ctrl-b d` to detach.

If you don't want tmux:

```sh
fanout apply           # sequential, all in your current terminal
fanout apply --dry-run # print the commands; don't run them
```

When it's done, HTML report opens. Snapshot id is in the report. You can `fanout rollback` from any future moment.

### Day two — drift

You install something off-manifest:

```sh
brew install httpie
```

Then:

```sh
fanout state diff
```

Exit code 2. HTML report opens listing the off-manifest install. Two choices:
- Add `httpie` to a module in your manifest (then `fanout apply` is a no-op; state catches up).
- Leave it off; `fanout apply` would uninstall it on the next run.

### Day two — undo

```sh
fanout rollback
```

Restores the snapshot taken before the most recent apply. HTML report shows what was re-installed and what was removed.

### Day N — AI mode

You forgot what to add to your manifest. Ask Claude:

```sh
fanout plan "set up a Python ML rig with PyTorch and Jupyter"
```

Claude writes a concise HTML plan titled **Project Fanout: Launching your Developer Setup Effectively**. It tells you:
- The goal restated in its own words
- The strategy (which package managers, how many items)
- 7-or-fewer numbered steps
- The exact YAML to add to your manifest
- Verification commands to run after `fanout apply`
- Risks and callouts before you act

Or generate the whole manifest:

```sh
fanout ai "set up a Python ML rig with PyTorch and Jupyter"
```

Claude writes the full `workstation.yml`. `[a]ccept / [e]dit / [r]egen / [q]uit`. On accept it writes the file. Then `fanout apply --tmux`.

---

## Every command produces HTML

Inspired by Thariq's [Unreasonable Effectiveness of HTML](https://thariqs.github.io/html-effectiveness/).

| Command | HTML report shows |
|---------|-------------------|
| `fanout plan` (no task) | Summary cards (+install / −remove / =keep), per-adapter buckets, color-coded items |
| `fanout plan "task"` | Claude-generated setup plan with goal, strategy, steps, manifest additions, verification |
| `fanout apply` | Snapshot id, installed/removed/failed sections, full log per bucket |
| `fanout state` | Per-bucket ownership counts, item lists, snapshot history table |
| `fanout state diff` | Drift cards, off-manifest installs, missing items with rationale |
| `fanout verify` | Pass/fail per check, stderr capture on failures |
| `fanout rollback` | Re-installed/removed sections, log capture |
| `fanout ai "..."` | Generated manifest with accept status |
| `fanout claude "..." "..."` | Aggregated worker output |

Reports go to `~/.fanout/reports/<timestamp>-<cmd>.html`. Auto-open in browser. Add `--no-open` to suppress the browser, `--no-html` to skip the report entirely.

---

## Why it's undeniable

| | shell script | fanout |
|---|---|---|
| Source format | 40 KB of bash | ~70 lines of YAML |
| Re-run safe? | No | Yes |
| Preview before apply? | No | `fanout plan` |
| Rollback after fail? | No | `fanout rollback` |
| Tracks what it installed? | No | `~/.fanout/state.json` |
| Detects drift? | No | `fanout state diff` |
| Picks subset? | Fork & edit | `--profile X` |
| Verifies install works? | No | `fanout verify` |
| AI assistance? | No | `fanout plan "..."` and `fanout ai "..."` |
| Visible parallel installs? | No | `fanout apply --tmux` |
| HTML reports you can share? | No | Every command |
| Adds a new package manager? | Edit the bash | One ~60 LOC `adapters/X.py` |
| Test suite? | None | 67 unit tests |

Every right-column cell answers a real failure of the left column.

---

## Subcommand reference

```
fanout init [--from-workstation] [--force]      # create manifest from template
fanout edit                                      # $EDITOR on the manifest
fanout plan ["<task>"] [--profile X]             # diff (no task) or Claude plan (with task)
fanout apply [--profile X] [--tmux] [--dry-run]  # converge
fanout state                                     # show owned items
fanout state diff                                # detect drift (exit 2 on drift)
fanout verify                                    # run sanity checks
fanout rollback [--dry-run]                      # restore last snapshot
fanout ai "<prose description>"                  # Claude writes the manifest
fanout claude "task a" "task b"                  # N parallel claude -p in tmux panes
```

Universal flags (every command):
- `--no-open` — write the HTML report but don't open the browser
- `--no-html` — skip the HTML report entirely

---

## Files this tool touches

- `~/.fanout/workstation.yml` — your manifest. Edit freely.
- `~/.fanout/state.json` — what fanout owns. Don't edit directly.
- `~/.fanout/snapshots/<ts>.json` — rollback snapshots. Don't edit.
- `~/.fanout/reports/<ts>-<cmd>.html` — HTML reports. Browse freely, share freely.

Nothing else outside `~/.fanout/`.

---

## Tests

```sh
python3 -m pytest -v
```

67 unit tests across manifest, state, adapters, reports. Subprocess fully mocked. <1s.

---

## Attribution

Tool inventory in `manifests/workstation-port.yml` derived from
[LeuAlmeida/workstation](https://github.com/LeuAlmeida/workstation) (MIT).
See `NOTICE.md`.

HTML-first communication inspired by Thariq's
[Unreasonable Effectiveness of HTML](https://thariqs.github.io/html-effectiveness/).

## License

MIT.
