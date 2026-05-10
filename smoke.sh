#!/usr/bin/env bash
# Smoke — verify v3 subcommands work end-to-end without spending model tokens
# (skips `fanout ai` which would call Claude).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$DIR/run.sh"

echo "=== smoke 1: pytest ==="
( cd "$DIR" && python3 -m pytest -q )

echo
echo "=== smoke 2: fanout --help ==="
"$RUN" --help >/dev/null
echo "ok"

echo
echo "=== smoke 3: fanout plan --manifest manifests/default.yml --profile minimal ==="
"$RUN" plan --manifest "$DIR/manifests/default.yml" --profile minimal | head -10
echo "..."
echo "ok"

echo
echo "=== smoke 4: fanout plan --manifest manifests/workstation-port.yml ==="
"$RUN" plan --manifest "$DIR/manifests/workstation-port.yml" | head -10
echo "..."
echo "ok"

echo
echo "all smokes passed."
