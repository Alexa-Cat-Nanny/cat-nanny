"""Package the MCP server for Lambda (python3.13, x86_64) into .build/lambda. Works on Windows, macOS and Linux.

Dependencies are downloaded as Linux wheels, so no Docker is needed. cat-health-signals comes from the
sibling folder ../cat-health-signals if it exists, otherwise from SIGNALS_SRC
(e.g. git+https://github.com/Alexa-Cat-Nanny/cat-health-signals.git@main).

Usage:  python scripts/build_lambda.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".build" / "lambda"
SIGNALS_SRC = os.environ.get("SIGNALS_SRC") or str(ROOT.parent / "cat-health-signals")

shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True)

pip = [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(OUT)]
subprocess.run([*pip, "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.13",
                "--only-binary=:all:", "-r", str(ROOT / "server" / "requirements.txt")], check=True)
# Pure-Python package; its only dependency (pydantic) is already installed above.
subprocess.run([*pip, "--no-deps", SIGNALS_SRC], check=True)

shutil.copytree(ROOT / "server" / "cat_nanny", OUT / "cat_nanny", ignore=shutil.ignore_patterns("__pycache__"))
for cache in OUT.rglob("__pycache__"):
    shutil.rmtree(cache, ignore_errors=True)

size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
print(f"Built {OUT} ({size:.0f} MB)")
