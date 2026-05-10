# NOTICE

## Attribution

The macOS tool inventory in `manifests/default.yml` is derived from
[`LeuAlmeida/workstation`](https://github.com/LeuAlmeida/workstation) (MIT license).
That project's `scripts/apple/macosx.sh` was the source list of brew, cask, and npm
packages we port into the declarative manifest format.

## What is original to fanout

- The declarative manifest format (`workstation.yml`).
- The `plan` / `apply` / `state` / `verify` / `rollback` engine.
- Per-package-manager adapter architecture (`adapters/brew.py`, `adapters/npm.py`, etc).
- Idempotence + drift detection via the state file.
- tmux pane dispatch of installer tasks (reused from fanout's earlier multi-agent
  pipeline; see `workers.py`).
- AI manifest generation via Claude (`fanout ai "describe your workstation"`).

## License

The portions derived from `LeuAlmeida/workstation` carry the MIT license terms of
that project. fanout itself is also released under MIT. See `LICENSE`.
