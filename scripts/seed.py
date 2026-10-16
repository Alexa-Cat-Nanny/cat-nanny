"""Load the demo household (Mochi, Jeremy, two caregivers, a few observations) into the deployed DynamoDB table.

Usage:  TABLE_NAME=<from stack outputs> AWS_REGION=us-east-1 python scripts/seed.py
Running it twice is safe: the same cats and caregivers are overwritten, observations get new ids.
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
household = os.environ.get("HOUSEHOLD_ID", "demo")
count = seed_store(DynamoStore(table), household)
print(f"Seeded household {household!r} with Mochi, Jeremy and {count} observations into {table}.")
