#!/usr/bin/env bash
# Smoke — pytest + help text. No model spend.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== pytest ==="
( cd "$DIR" && python3 -m pytest -q )

echo
echo "=== fanout --help ==="
"$DIR/run.sh" --help

echo
echo "=== fanout claude --help ==="
"$DIR/run.sh" claude --help

echo
echo "=== fanout plan --help ==="
"$DIR/run.sh" plan --help

echo
echo "all smokes passed."
