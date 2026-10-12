"""Sample history for demos: 60 days for Mochi, every event marked source="seed".

Story: Mochi vomits about once every two weeks, the food changes 9 days ago,
after which she vomits several times, mostly soon after dry food, and loses a
little weight. Detail keys follow cat_health_signals.fields.DETAIL_FIELDS; update
them here if that table changes.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from cat_health_signals import CatProfile, Event

from .store import Store, new_id

MOCHI = CatProfile(cat_id="mochi", name="Mochi", sex="female", neutered=True, birth_year=2019,
                   current_food="Brand Y dry", meals_per_day=2, med_schedule="one pill every morning")


def demo_events(now: datetime | None = None, cat_id: str = "mochi") -> list[Event]:
    now = now or datetime.now(UTC)
    rng = random.Random(42)
    events: list[Event] = []

    def add(days_ago: float, type_: str, reporter: str, **details: object) -> None:
        ts = now - timedelta(days=days_ago)
        events.append(Event(event_id=new_id(ts), cat_id=cat_id, type=type_, ts=ts, details=details, reporter=reporter, source="seed"))

    family = ["Alex", "Sam"]
    for d in range(60, 0, -1):
        add(d - 0.30, "meal", rng.choice(family), food="Brand X dry" if d > 9 else "Brand Y dry")
        add(d - 0.75, "meal", rng.choice(family), food="Brand X dry" if d > 9 else "Brand Y dry")
        add(d - 0.31, "medication", rng.choice(family), name="pill")
    for d in (55, 41, 30, 16):  # baseline: about every two weeks
        add(d - 0.28, "vomit", rng.choice(family), content="hairball", blood=False)
    add(9, "food_change", "Alex", **{"from": "Brand X dry", "to": "Brand Y dry"})
    for d in (7, 5, 4, 2):  # after the switch, mostly right after breakfast
        add(d - 0.29, "vomit", rng.choice(family), content="food", blood=False, minutes_after_meal=rng.choice([10, 15, 20]))
    add(25, "weight", "Sam", kg=4.2)
    add(12, "weight", "Sam", kg=4.15)
    add(1, "weight", "Sam", kg=4.0)
    return events


def seed_store(store: Store, household: str, now: datetime | None = None) -> int:
    store.put_cat(household, MOCHI)
    events = demo_events(now)
    for e in events:
        store.put_event(household, e)
    return len(events)
