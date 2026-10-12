"""Load 60 days of sample history (source="seed") for Mochi into the deployed DynamoDB table.

Usage:  TABLE_NAME=<from stack outputs> python scripts/seed.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from cat_nanny.demo_data import seed_store  # noqa: E402
from cat_nanny.store import DynamoStore  # noqa: E402

table = os.environ.get("TABLE_NAME")
if not table:
    sys.exit("Set TABLE_NAME (see the stack outputs).")
count = seed_store(DynamoStore(table), os.environ.get("HOUSEHOLD_ID", "demo"))
print(f"Seeded Mochi with {count} sample events into {table}.")
