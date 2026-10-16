"""DynamoStore against a mocked DynamoDB table (moto), same table shape as infra/template.yaml."""

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from cat_nanny.demo_data import JEREMY, MOCHI, demo_records, seed_store
from cat_nanny.store import Caregiver, DynamoStore, Photo, Reassignment, find_cat_by_name, new_id

NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="cat-nanny-test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoStore("cat-nanny-test", table=table)


def test_cats_and_caregivers(store):
    seed_store(store, "demo", now=NOW)
    assert [c.name for c in store.list_cats("demo")] == ["Jeremy", "Mochi"]
    assert store.get_cat("demo", "cat-mochi") == MOCHI
    assert find_cat_by_name(store, "demo", "MOCHI") == MOCHI
    assert [p.name for p in store.list_caregivers("demo")] == ["Alex", "Sam"]
    store.put_caregiver("demo", Caregiver(caregiver_id="p-x", name="Jordan"))
    assert len(store.list_caregivers("demo")) == 3


def test_observations_roundtrip_and_time_range(store):
    seed_store(store, "demo", now=NOW)
    records = demo_records(NOW)
    week = store.query_observations("demo", "cat-mochi", NOW - timedelta(days=7), NOW)
    assert [r.event.ts for r in week] == [r.event.ts for r in records if r.event.cat_id == "cat-mochi"]
    assert week[1].status == "awaiting_answer"
    assert week[1].event.details["minutes_after_meal"] == 15 and isinstance(week[1].event.details["minutes_after_meal"], int)
    assert store.query_observations("demo", "cat-mochi", NOW - timedelta(days=3), NOW)[0].event.event_id == week[1].event.event_id
    got = store.get_observation("demo", week[0].event.event_id)
    assert got == week[0]
    assert store.get_observation("demo", "missing") is None


def test_move_observation_carries_photos_and_keeps_id(store):
    seed_store(store, "demo", now=NOW)
    event_id = store.query_observations("demo", "cat-mochi", NOW - timedelta(days=7))[0].event.event_id
    store.put_photo("demo", Photo(photo_id="p1", event_id=event_id, cat_id="cat-mochi", storage_key="k/p1.jpg",
                                  content_type="image/jpeg", uploaded_at=NOW))
    moved = store.move_observation("demo", event_id, JEREMY.cat_id)
    assert moved.event.event_id == event_id and moved.event.cat_id == "cat-jeremy"
    assert store.get_observation("demo", event_id).event.cat_id == "cat-jeremy"
    assert [r.event.event_id for r in store.query_observations("demo", "cat-mochi", NOW - timedelta(days=7))] != [event_id]
    assert any(r.event.event_id == event_id for r in store.query_observations("demo", "cat-jeremy", NOW - timedelta(days=7)))
    assert store.list_photos("demo", event_id)[0].cat_id == "cat-jeremy"
    store.put_reassignment("demo", Reassignment(reassignment_id=new_id(NOW), event_id=event_id, from_cat_id="cat-mochi",
                                                to_cat_id="cat-jeremy", reporter="Alex", reason="misspoke", corrected_at=NOW))
    assert store.list_reassignments("demo", event_id)[0].to_cat_id == "cat-jeremy"
