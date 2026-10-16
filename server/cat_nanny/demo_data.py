"""Sample household for demos: Mochi and Jeremy, two caregivers, a week of observations (source="seed").

Story: Mochi vomited twice this week (one with the follow-up still open),
Jeremy ate less yesterday. Enough for the care log, the vet summary and the
correction flow without typing a history by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cat_health_signals import CatProfile, Event

from .store import Caregiver, ObservationRecord, Store, new_id

MOCHI = CatProfile(cat_id="cat-mochi", name="Mochi", sex="female", neutered=True, birth_year=2019, current_food="Brand Y dry")
JEREMY = CatProfile(cat_id="cat-jeremy", name="Jeremy", sex="male", neutered=True, birth_year=2021)
ALEX = Caregiver(caregiver_id="person-alex", name="Alex")
SAM = Caregiver(caregiver_id="person-sam", name="Sam")


def demo_records(now: datetime | None = None) -> list[ObservationRecord]:
    now = now or datetime.now(UTC)

    def rec(days_ago: float, cat: CatProfile, type_: str, details: dict, reporter: str, status: str = "completed", unknown=()) -> ObservationRecord:
        ts = now - timedelta(days=days_ago)
        return ObservationRecord(
            event=Event(event_id=new_id(ts), cat_id=cat.cat_id, type=type_, ts=ts, details=details, reporter=reporter, source="seed"),
            status=status, unknown_fields=list(unknown), updated_at=ts)

    return [
        rec(5.3, MOCHI, "vomit", {"content": "hairball", "has_blood": False, "episode_count": 1, "low_energy": False, "minutes_after_meal": None}, "Sam"),
        rec(2.2, MOCHI, "vomit", {"content": "food", "has_blood": False, "episode_count": 2, "low_energy": None, "minutes_after_meal": 15}, "Alex",
            status="awaiting_answer"),
        rec(1.1, JEREMY, "appetite", {"status": "eating_less", "hours_since_food": 6}, "Alex"),
    ]


def seed_store(store: Store, household: str, now: datetime | None = None) -> int:
    for cat in (MOCHI, JEREMY):
        store.put_cat(household, cat)
    for person in (ALEX, SAM):
        store.put_caregiver(household, person)
    records = demo_records(now)
    for r in records:
        store.put_observation(household, r)
    return len(records)
