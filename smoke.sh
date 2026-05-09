#!/usr/bin/env bash
# Smoke test — three dry-runs across all three modes. Costs 3 planner calls.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$DIR/run.sh"

echo "=== smoke 1: extend / N=4 ==="
"$RUN" "audit auth layer for security, perf, DX, and accessibility" \
  --mode extend \
  --repo "$DIR/tests/fixtures/sample_repo" \
  --files "src/auth/*.py" "src/db/*.py" "src/api/*.py" "tests/auth/*.py" \
  -n 4 --dry-run >/tmp/fanout_smoke_extend.json
echo "ok — plan written to /tmp/fanout_smoke_extend.json"

echo "=== smoke 2: greenfield / N=8 ==="
"$RUN" "design a novel approach to interpretable agent memory" \
  --mode greenfield \
  -n 8 --dry-run >/tmp/fanout_smoke_greenfield.json
echo "ok — plan written to /tmp/fanout_smoke_greenfield.json"

echo "=== smoke 3: scratch / N=4 ==="
"$RUN" "build a CLI for tailing structured logs with jq-like filters" \
  --mode scratch \
  --refs "https://github.com/aurora/lnav" "https://stedolan.github.io/jq" \
  -n 4 --dry-run >/tmp/fanout_smoke_scratch.json
echo "ok — plan written to /tmp/fanout_smoke_scratch.json"

echo
echo "all smokes passed."
