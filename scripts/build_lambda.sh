#!/usr/bin/env bash
# Package the MCP server for Lambda (python3.13, x86_64) into .build/lambda.
# Works on macOS/Linux without Docker: dependencies are downloaded as Linux wheels.
#
# cat-health-signals comes from the sibling folder ../cat-health-signals if it exists,
# otherwise from SIGNALS_SRC (e.g. git+https://github.com/<org>/cat-health-signals.git@main).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/.build/lambda"
SIGNALS_SRC="${SIGNALS_SRC:-$ROOT/../cat-health-signals}"

rm -rf "$OUT"
mkdir -p "$OUT"

python3 -m pip install --quiet --target "$OUT" \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 --only-binary=:all: \
  -r "$ROOT/server/requirements.txt"

# Pure-Python package; its only dependency (pydantic) is already installed above.
python3 -m pip install --quiet --target "$OUT" --no-deps "$SIGNALS_SRC"

cp -R "$ROOT/server/cat_nanny" "$OUT/"
find "$OUT" -name "__pycache__" -type d -prune -exec rm -rf {} +

echo "Built $OUT ($(du -sh "$OUT" | cut -f1))"
