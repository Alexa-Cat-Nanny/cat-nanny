"""Package the MCP server for Lambda (python3.13, x86_64) into .build/lambda. Works on Windows, macOS and Linux.

Dependencies are downloaded as Linux wheels from the pinned list in server/requirements-lambda.txt, so no
Docker is needed. The list is pinned and installed with --no-deps because pip evaluates dependency markers
(e.g. mcp's `pywin32; sys_platform == "win32"`) against the build machine, not the target platform.

cat-health-signals comes from the sibling folder ../cat-health-signals if it exists, otherwise from
SIGNALS_SRC (e.g. git+https://github.com/Alexa-Cat-Nanny/cat-health-signals.git@main).

Usage:  python scripts/build_lambda.py           build .build/lambda
        python scripts/build_lambda.py --lock    (Linux only) re-resolve server/requirements.txt and rewrite the pinned list
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".build" / "lambda"
LOCK = ROOT / "server" / "requirements-lambda.txt"
SIGNALS_SRC = os.environ.get("SIGNALS_SRC") or str(ROOT.parent / "cat-health-signals")
TARGET = ["--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.13", "--only-binary=:all:"]


def pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(OUT), *args], check=True)


def write_lock() -> None:
    names = sorted(p.name for p in OUT.iterdir() if p.name.endswith(".dist-info") and not p.name.startswith("cat_health_signals"))
    pins = [re.sub(r"-([^-]+)\.dist-info$", r"==\1", n) for n in names]
    LOCK.write_text(
        "# Exact Linux x86_64 / Python 3.13 package set for the Lambda bundle, installed with --no-deps by scripts/build_lambda.py.\n"
        "# Pinned so the build is the same on Windows, macOS and Linux (pip evaluates dependency markers such as\n"
        "# 'pywin32; sys_platform == \"win32\"' against the build machine, which breaks cross-platform resolution).\n"
        "# Regenerate after changing server/requirements.txt: python scripts/build_lambda.py --lock (on Linux).\n"
        + "\n".join(pins) + "\n")
    print(f"Wrote {LOCK} ({len(pins)} packages)")


shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True)

if "--lock" in sys.argv:
    if sys.platform != "linux":
        sys.exit("--lock resolves markers for Linux; run it on Linux (or WSL).")
    pip(*TARGET, "-r", str(ROOT / "server" / "requirements.txt"))
    write_lock()
else:
    pip(*TARGET, "--no-deps", "-r", str(LOCK))
# Pure-Python package; its only dependency (pydantic) is already installed above.
pip("--no-deps", SIGNALS_SRC)

shutil.copytree(ROOT / "server" / "cat_nanny", OUT / "cat_nanny", ignore=shutil.ignore_patterns("__pycache__"))
for cache in OUT.rglob("__pycache__"):
    shutil.rmtree(cache, ignore_errors=True)

size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
print(f"Built {OUT} ({size:.0f} MB)")
