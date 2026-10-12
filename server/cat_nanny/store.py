"""Storage: cats, events and family alerts.

`Store` is the interface the tools use. Two implementations:
- `MemoryStore` for local development and tests (no AWS needed),
- `DynamoStore` for Lambda (one DynamoDB table).

DynamoDB single-table layout (pk / sk are strings):

| item         | pk                        | sk                     |
|--------------|---------------------------|------------------------|
| cat profile  | H#<household>             | CAT#<cat_id>           |
| event        | H#<household>#C#<cat_id>  | EV#<event_id>          |
| alert        | H#<household>#C#<cat_id>  | AL#<alert_id>          |

Event and alert ids start with a UTC timestamp (20261012T084000Z-1a2b3c), so
sorting by sk is sorting by time, and a time range is a `between` query.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from cat_health_signals import CatProfile, Event
from pydantic import BaseModel, Field

ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"


def new_id(ts: datetime) -> str:
    """Time-ordered id: UTC timestamp + random suffix."""
    return f"{ts.astimezone(UTC).strftime(ID_TIME_FORMAT)}-{secrets.token_hex(3)}"


def id_floor(ts: datetime) -> str:
    """The smallest possible id at or after `ts` (for range queries)."""
    return ts.astimezone(UTC).strftime(ID_TIME_FORMAT)


def cat_id_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "cat"


class Alert(BaseModel):
    """A red flag that the rest of the family should hear about."""

    alert_id: str
    cat_id: str
    level: Literal["emergency", "vet_today"]
    message: str
    created_at: datetime
    source_event_id: str | None = None
    rule_id: str | None = None
    reporter: str | None = None
    acknowledged: bool = False
    acknowledged_by: str | None = None
    note: str | None = None


class Store(Protocol):
    def put_cat(self, household: str, cat: CatProfile) -> None: ...
    def get_cat(self, household: str, cat_id: str) -> CatProfile | None: ...
    def list_cats(self, household: str) -> list[CatProfile]: ...

    def put_event(self, household: str, event: Event) -> None: ...
    def get_event(self, household: str, cat_id: str, event_id: str) -> Event | None: ...
    def query_events(self, household: str, cat_id: str, since: datetime, until: datetime | None = None) -> list[Event]:
        """Events with since <= ts <= until (until defaults to now), oldest first."""
        ...

    def put_alert(self, household: str, alert: Alert) -> None: ...
    def list_alerts(self, household: str, cat_id: str, since: datetime, open_only: bool = True) -> list[Alert]:
        """Alerts created at or after `since` (only unacknowledged ones if open_only), newest first."""
        ...


# --- in-memory ----------------------------------------------------------------


class MemoryStore:
    def __init__(self) -> None:
        self._cats: dict[tuple[str, str], CatProfile] = {}
        self._events: dict[tuple[str, str], dict[str, Event]] = {}
        self._alerts: dict[tuple[str, str], dict[str, Alert]] = {}

    def put_cat(self, household: str, cat: CatProfile) -> None:
        self._cats[(household, cat.cat_id)] = cat.model_copy(deep=True)

    def get_cat(self, household: str, cat_id: str) -> CatProfile | None:
        cat = self._cats.get((household, cat_id))
        return cat.model_copy(deep=True) if cat else None

    def list_cats(self, household: str) -> list[CatProfile]:
        return [c.model_copy(deep=True) for (h, _), c in sorted(self._cats.items()) if h == household]

    def put_event(self, household: str, event: Event) -> None:
        self._events.setdefault((household, event.cat_id), {})[event.event_id] = event.model_copy(deep=True)

    def get_event(self, household: str, cat_id: str, event_id: str) -> Event | None:
        e = self._events.get((household, cat_id), {}).get(event_id)
        return e.model_copy(deep=True) if e else None

    def query_events(self, household: str, cat_id: str, since: datetime, until: datetime | None = None) -> list[Event]:
        until = until or datetime.now(UTC)
        events = self._events.get((household, cat_id), {}).values()
        return sorted((e.model_copy(deep=True) for e in events if since <= e.ts <= until), key=lambda e: e.ts)

    def put_alert(self, household: str, alert: Alert) -> None:
        self._alerts.setdefault((household, alert.cat_id), {})[alert.alert_id] = alert.model_copy(deep=True)

    def list_alerts(self, household: str, cat_id: str, since: datetime, open_only: bool = True) -> list[Alert]:
        alerts = self._alerts.get((household, cat_id), {}).values()
        return sorted(
            (a.model_copy(deep=True) for a in alerts if not (open_only and a.acknowledged) and a.created_at >= since),
            key=lambda a: a.created_at,
            reverse=True,
        )


# --- DynamoDB -----------------------------------------------------------------


def _to_dynamo(value: Any) -> Any:
    """JSON-safe Python -> DynamoDB types (floats become Decimal)."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        # Decimal('4') came from an int, Decimal('4.0') from a float: keep the original type.
        return int(value) if value.as_tuple().exponent >= 0 else float(value)
    return value


class DynamoStore:
    def __init__(self, table_name: str, table: Any = None) -> None:
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self._t = table

    @staticmethod
    def _cat_pk(household: str, cat_id: str) -> str:
        return f"H#{household}#C#{cat_id}"

    # cats
    def put_cat(self, household: str, cat: CatProfile) -> None:
        item = {"pk": f"H#{household}", "sk": f"CAT#{cat.cat_id}", "kind": "cat", "data": _to_dynamo(cat.model_dump(mode="json"))}
        self._t.put_item(Item=item)

    def get_cat(self, household: str, cat_id: str) -> CatProfile | None:
        item = self._t.get_item(Key={"pk": f"H#{household}", "sk": f"CAT#{cat_id}"}).get("Item")
        return CatProfile.model_validate(_from_dynamo(item["data"])) if item else None

    def list_cats(self, household: str) -> list[CatProfile]:
        from boto3.dynamodb.conditions import Key

        items = self._query_all(KeyConditionExpression=Key("pk").eq(f"H#{household}") & Key("sk").begins_with("CAT#"))
        return [CatProfile.model_validate(_from_dynamo(i["data"])) for i in items]

    # events
    def put_event(self, household: str, event: Event) -> None:
        item = {
            "pk": self._cat_pk(household, event.cat_id),
            "sk": f"EV#{event.event_id}",
            "kind": "event",
            "type": event.type,
            "ts": event.ts.astimezone(UTC).isoformat(),
            "data": _to_dynamo(event.model_dump(mode="json")),
        }
        self._t.put_item(Item=item)

    def get_event(self, household: str, cat_id: str, event_id: str) -> Event | None:
        item = self._t.get_item(Key={"pk": self._cat_pk(household, cat_id), "sk": f"EV#{event_id}"}).get("Item")
        return Event.model_validate(_from_dynamo(item["data"])) if item else None

    def query_events(self, household: str, cat_id: str, since: datetime, until: datetime | None = None) -> list[Event]:
        from boto3.dynamodb.conditions import Key

        until = until or datetime.now(UTC)
        key = Key("pk").eq(self._cat_pk(household, cat_id)) & Key("sk").between(
            f"EV#{id_floor(since)}", f"EV#{id_floor(until)}~"
        )
        events = [Event.model_validate(_from_dynamo(i["data"])) for i in self._query_all(KeyConditionExpression=key)]
        return sorted((e for e in events if since <= e.ts <= until), key=lambda e: e.ts)

    # alerts
    def put_alert(self, household: str, alert: Alert) -> None:
        item = {
            "pk": self._cat_pk(household, alert.cat_id),
            "sk": f"AL#{alert.alert_id}",
            "kind": "alert",
            "acknowledged": alert.acknowledged,
            "data": _to_dynamo(alert.model_dump(mode="json")),
        }
        self._t.put_item(Item=item)

    def list_alerts(self, household: str, cat_id: str, since: datetime, open_only: bool = True) -> list[Alert]:
        from boto3.dynamodb.conditions import Attr, Key

        key = Key("pk").eq(self._cat_pk(household, cat_id)) & Key("sk").between(f"AL#{id_floor(since)}", "AL#~")
        kwargs: dict[str, Any] = {"KeyConditionExpression": key}
        if open_only:
            kwargs["FilterExpression"] = Attr("acknowledged").eq(False)
        items = self._query_all(**kwargs)
        alerts = [Alert.model_validate(_from_dynamo(i["data"])) for i in items]
        return sorted((a for a in alerts if a.created_at >= since), key=lambda a: a.created_at, reverse=True)

    def _query_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            resp = self._t.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def make_store(kind: str, table_name: str = "") -> Store:
    if kind == "dynamodb":
        if not table_name:
            raise ValueError("STORE=dynamodb needs TABLE_NAME")
        return DynamoStore(table_name)
    if kind == "memory":
        return MemoryStore()
    raise ValueError(f"Unknown STORE {kind!r} (use memory or dynamodb)")


class EventDraft(BaseModel):
    """Helper for building a new Event with a fresh id."""

    cat_id: str
    type: str
    ts: datetime
    details: dict[str, Any] = Field(default_factory=dict)
    reporter: str | None = None
    source: str = "voice"

    def to_event(self) -> Event:
        return Event(event_id=new_id(self.ts), **self.model_dump())
