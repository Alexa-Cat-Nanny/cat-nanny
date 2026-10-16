#!/usr/bin/env bash
# Package the MCP server for Lambda into .build/lambda (see build_lambda.py for details).
set -euo pipefail
exec python3 "$(dirname "$0")/build_lambda.py"
