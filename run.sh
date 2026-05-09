#!/usr/bin/env bash
set -euo pipefail
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  TARGET="$(readlink "$SOURCE")"
  if [[ "$TARGET" = /* ]]; then SOURCE="$TARGET"; else SOURCE="$(dirname "$SOURCE")/$TARGET"; fi
done
DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
exec /usr/bin/python3 "$DIR/fanout.py" "$@"
