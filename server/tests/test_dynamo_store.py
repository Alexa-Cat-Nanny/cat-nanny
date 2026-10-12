"""DynamoStore against a mocked DynamoDB table (moto), same table shape as infra/template.yaml."""

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from cat_nanny.demo_data import MOCHI, demo_events
from cat_nanny.store import Alert, DynamoStore, new_id

NOW = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)


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


def test_cats_events_alerts_roundtrip(store):
    store.put_cat("demo", MOCHI)
    assert store.get_cat("demo", "mochi") == MOCHI
    assert [c.name for c in store.list_cats("demo")] == ["Mochi"]

    events = demo_events(NOW)
    for e in events:
        store.put_event("demo", e)
    last_week = store.query_events("demo", "mochi", NOW - timedelta(days=7), NOW)
    assert last_week and all(NOW - timedelta(days=7) <= e.ts <= NOW for e in last_week)
    assert last_week == sorted(last_week, key=lambda e: e.ts)
    weight = [e for e in last_week if e.type == "weight"][0]
    assert weight.details["kg"] == 4.0 and isinstance(weight.details["kg"], float)

    got = store.get_event("demo", "mochi", last_week[0].event_id)
    assert got == last_week[0]

    a = Alert(alert_id=new_id(NOW), cat_id="mochi", level="vet_today", message="m", created_at=NOW, rule_id="r")
    store.put_alert("demo", a)
    assert [x.alert_id for x in store.list_alerts("demo", "mochi", NOW - timedelta(hours=1))] == [a.alert_id]
    store.put_alert("demo", a.model_copy(update={"acknowledged": True}))
    assert store.list_alerts("demo", "mochi", NOW - timedelta(hours=1)) == []
    assert len(store.list_alerts("demo", "mochi", NOW - timedelta(hours=1), open_only=False)) == 1
