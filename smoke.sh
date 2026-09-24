#!/usr/bin/env bash
# Smoke test: unit tests, every command's --help, then an end-to-end run with fake agents.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

python3 -m pytest -q
for c in "" plan run claude codex agents doctor boost status logs exec mcp install; do
  "$DIR/run.sh" $c --help >/dev/null
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/cfg"
for a in fakeclaude fakecodex; do
  printf '#!/usr/bin/env bash\necho "%s ok"\n' "$a" > "$TMP/bin/$a"
  chmod +x "$TMP/bin/$a"
done
echo '{"claude": {"bins": ["fakeclaude"]}, "codex": {"bins": ["fakecodex"]}}' > "$TMP/cfg/agents.json"
export PATH="$TMP/bin:/usr/bin:/bin" FANOUT_CONFIG_DIR="$TMP/cfg" FANOUT_RUNS_DIR="$TMP/runs"
unset OPENROUTER_API_KEY FANOUT_OPENROUTER_API_KEY

"$DIR/run.sh" agents | grep -q fakecodex
"$DIR/run.sh" plan $'- fix the failing shell script\n- refactor the architecture' --planner naive --no-tmux --cwd "$TMP" \
  | grep -q "fakecodex ok"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"cursor"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | "$DIR/run.sh" mcp | grep -q fanout_run
echo "smoke ok"
