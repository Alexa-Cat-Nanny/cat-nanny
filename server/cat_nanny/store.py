"""Storage: cats, caregivers, observations, photos and reassignment audit records.

`Store` is the interface the MCP tools use. Two implementations:
- `MemoryStore` for local development and tests (no AWS needed),
- `DynamoStore` for Lambda (one DynamoDB table).

The entities and their semantics come from CatNannyFramework (SQLite tables
cats, caregivers, events, event_progress, photos, event_reassignments),
re-modelled for a single DynamoDB table:

| item          | pk                        | sk                 |
|---------------|---------------------------|--------------------|
| cat           | H#<household>             | CAT#<cat_id>       |
| caregiver     | H#<household>             | CG#<caregiver_id>  |
| event         | H#<household>#C#<cat_id>  | EV#<event_id>      |
| photo         | H#<household>#E#<event_id>| PH#<photo_id>      |
| reassignment  | H#<household>#E#<event_id>| RA#<reassign_id>   |

Event ids start with a UTC timestamp (20261012T084000Z-1a2b3c), so sorting
by sk is sorting by observation time and a time range is a `between` query.
Moving an event to another cat (a correction) changes its pk, so it is a
delete + put; photos live under the event, so they move with it for free.
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


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "x"


# --- entities ------------------------------------------------------------------


class Caregiver(BaseModel):
    caregiver_id: str
    name: str


EventStatus = Literal["awaiting_answer", "completed"]


class ObservationRecord(BaseModel):
    """An event plus its conversation progress (what is still unanswered)."""

    event: Event
    status: EventStatus = "awaiting_answer"
    unknown_fields: list[str] = Field(default_factory=list)
    updated_at: datetime


class Photo(BaseModel):
    photo_id: str
    event_id: str
    cat_id: str
    storage_key: str
    content_type: str
    uploaded_at: datetime


class Reassignment(BaseModel):
    reassignment_id: str
    event_id: str
    from_cat_id: str
    to_cat_id: str
    reporter: str | None
    reason: str | None
    corrected_at: datetime


class Store(Protocol):
    # cats
    def put_cat(self, household: str, cat: CatProfile) -> None: ...
    def get_cat(self, household: str, cat_id: str) -> CatProfile | None: ...
    def list_cats(self, household: str) -> list[CatProfile]: ...

    # caregivers
    def put_caregiver(self, household: str, caregiver: Caregiver) -> None: ...
    def list_caregivers(self, household: str) -> list[Caregiver]: ...

    # observations
    def put_observation(self, household: str, record: ObservationRecord) -> None: ...
    def get_observation(self, household: str, event_id: str) -> ObservationRecord | None:
        """Find by id across all cats (ids are unique per household)."""
        ...
    def query_observations(self, household: str, cat_id: str, since: datetime, until: datetime | None = None) -> list[ObservationRecord]:
        """since <= ts <= until (until defaults to now), oldest first."""
        ...
    def move_observation(self, household: str, event_id: str, to_cat_id: str) -> ObservationRecord:
        """Re-home an observation (and its photos) under another cat. Returns the updated record."""
        ...

    # photos and audit
    def put_photo(self, household: str, photo: Photo) -> None: ...
    def list_photos(self, household: str, event_id: str) -> list[Photo]: ...
    def put_reassignment(self, household: str, item: Reassignment) -> None: ...
    def list_reassignments(self, household: str, event_id: str) -> list[Reassignment]: ...


# --- helpers shared by both stores ---------------------------------------------


def find_cat_by_name(store: Store, household: str, name: str) -> CatProfile | None:
    """Case-insensitive match on the cat's name (the voice assistant passes names, not ids)."""
    wanted = name.strip().casefold()
    for cat in store.list_cats(household):
        if cat.name.casefold() == wanted or cat.cat_id == name:
            return cat
    return None


def ensure_caregiver(store: Store, household: str, name: str | None) -> Caregiver | None:
    """Caregivers are self-declared attribution, not authentication: create on first use."""
    if not name or not name.strip():
        return None
    wanted = name.strip()
    for person in store.list_caregivers(household):
        if person.name.casefold() == wanted.casefold():
            return person
    person = Caregiver(caregiver_id=f"person-{slug(wanted)}-{secrets.token_hex(2)}", name=wanted)
    store.put_caregiver(household, person)
    return person


# --- in-memory ------------------------------------------------------------------


class MemoryStore:
    def __init__(self) -> None:
        self._cats: dict[tuple[str, str], CatProfile] = {}
        self._caregivers: dict[tuple[str, str], Caregiver] = {}
        self._obs: dict[tuple[str, str], ObservationRecord] = {}  # (household, event_id)
        self._photos: dict[tuple[str, str], list[Photo]] = {}
        self._moves: dict[tuple[str, str], list[Reassignment]] = {}

    def put_cat(self, household, cat):
        self._cats[(household, cat.cat_id)] = cat.model_copy(deep=True)

    def get_cat(self, household, cat_id):
        c = self._cats.get((household, cat_id))
        return c.model_copy(deep=True) if c else None

    def list_cats(self, household):
        return sorted((c.model_copy(deep=True) for (h, _), c in self._cats.items() if h == household), key=lambda c: c.name.casefold())

    def put_caregiver(self, household, caregiver):
        self._caregivers[(household, caregiver.caregiver_id)] = caregiver.model_copy()

    def list_caregivers(self, household):
        return sorted((c.model_copy() for (h, _), c in self._caregivers.items() if h == household), key=lambda c: c.name.casefold())

    def put_observation(self, household, record):
        self._obs[(household, record.event.event_id)] = record.model_copy(deep=True)

    def get_observation(self, household, event_id):
        r = self._obs.get((household, event_id))
        return r.model_copy(deep=True) if r else None

    def query_observations(self, household, cat_id, since, until=None):
        until = until or datetime.now(UTC)
        rows = [r for (h, _), r in self._obs.items() if h == household and r.event.cat_id == cat_id and since <= r.event.ts <= until]
        return sorted((r.model_copy(deep=True) for r in rows), key=lambda r: r.event.ts)

    def move_observation(self, household, event_id, to_cat_id):
        record = self._obs[(household, event_id)]
        record = record.model_copy(update={"event": record.event.model_copy(update={"cat_id": to_cat_id})})
        self._obs[(household, event_id)] = record
        self._photos[(household, event_id)] = [p.model_copy(update={"cat_id": to_cat_id}) for p in self._photos.get((household, event_id), [])]
        return record.model_copy(deep=True)

    def put_photo(self, household, photo):
        self._photos.setdefault((household, photo.event_id), []).append(photo.model_copy())

    def list_photos(self, household, event_id):
        return sorted((p.model_copy() for p in self._photos.get((household, event_id), [])), key=lambda p: p.uploaded_at)

    def put_reassignment(self, household, item):
        self._moves.setdefault((household, item.event_id), []).append(item.model_copy())

    def list_reassignments(self, household, event_id):
        return sorted((m.model_copy() for m in self._moves.get((household, event_id), [])), key=lambda m: m.corrected_at)


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
    def _hpk(h: str) -> str:
        return f"H#{h}"

    @staticmethod
    def _cpk(h: str, cat_id: str) -> str:
        return f"H#{h}#C#{cat_id}"

    @staticmethod
    def _epk(h: str, event_id: str) -> str:
        return f"H#{h}#E#{event_id}"

    def _query_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            resp = self._t.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # cats
    def put_cat(self, household, cat):
        self._t.put_item(Item={"pk": self._hpk(household), "sk": f"CAT#{cat.cat_id}", "kind": "cat",
                               "data": _to_dynamo(cat.model_dump(mode="json"))})

    def get_cat(self, household, cat_id):
        item = self._t.get_item(Key={"pk": self._hpk(household), "sk": f"CAT#{cat_id}"}).get("Item")
        return CatProfile.model_validate(_from_dynamo(item["data"])) if item else None

    def list_cats(self, household):
        from boto3.dynamodb.conditions import Key

        items = self._query_all(KeyConditionExpression=Key("pk").eq(self._hpk(household)) & Key("sk").begins_with("CAT#"))
        return sorted((CatProfile.model_validate(_from_dynamo(i["data"])) for i in items), key=lambda c: c.name.casefold())

    # caregivers
    def put_caregiver(self, household, caregiver):
        self._t.put_item(Item={"pk": self._hpk(household), "sk": f"CG#{caregiver.caregiver_id}", "kind": "caregiver",
                               "data": _to_dynamo(caregiver.model_dump(mode="json"))})

    def list_caregivers(self, household):
        from boto3.dynamodb.conditions import Key

        items = self._query_all(KeyConditionExpression=Key("pk").eq(self._hpk(household)) & Key("sk").begins_with("CG#"))
        return sorted((Caregiver.model_validate(_from_dynamo(i["data"])) for i in items), key=lambda c: c.name.casefold())

    # observations
    def _obs_item(self, household: str, record: ObservationRecord) -> dict[str, Any]:
        e = record.event
        return {"pk": self._cpk(household, e.cat_id), "sk": f"EV#{e.event_id}", "kind": "event",
                "event_id": e.event_id, "type": e.type, "ts": e.ts.astimezone(UTC).isoformat(),
                "data": _to_dynamo(record.model_dump(mode="json"))}

    def put_observation(self, household, record):
        item = self._obs_item(household, record)
        # Index item so an event can be found by id without knowing its cat.
        self._t.put_item(Item=item)
        self._t.put_item(Item={"pk": self._epk(household, record.event.event_id), "sk": "META", "kind": "event_index",
                               "cat_id": record.event.cat_id})

    def get_observation(self, household, event_id):
        meta = self._t.get_item(Key={"pk": self._epk(household, event_id), "sk": "META"}).get("Item")
        if not meta:
            return None
        item = self._t.get_item(Key={"pk": self._cpk(household, meta["cat_id"]), "sk": f"EV#{event_id}"}).get("Item")
        return ObservationRecord.model_validate(_from_dynamo(item["data"])) if item else None

    def query_observations(self, household, cat_id, since, until=None):
        from boto3.dynamodb.conditions import Key

        until = until or datetime.now(UTC)
        key = Key("pk").eq(self._cpk(household, cat_id)) & Key("sk").between(f"EV#{id_floor(since)}", f"EV#{id_floor(until)}~")
        rows = [ObservationRecord.model_validate(_from_dynamo(i["data"])) for i in self._query_all(KeyConditionExpression=key)]
        return sorted((r for r in rows if since <= r.event.ts <= until), key=lambda r: r.event.ts)

    def move_observation(self, household, event_id, to_cat_id):
        record = self.get_observation(household, event_id)
        if record is None:
            raise KeyError(event_id)
        old = record.event.cat_id
        record = record.model_copy(update={"event": record.event.model_copy(update={"cat_id": to_cat_id})})
        self._t.put_item(Item=self._obs_item(household, record))
        self._t.put_item(Item={"pk": self._epk(household, event_id), "sk": "META", "kind": "event_index", "cat_id": to_cat_id})
        if old != to_cat_id:
            self._t.delete_item(Key={"pk": self._cpk(household, old), "sk": f"EV#{event_id}"})
        for photo in self.list_photos(household, event_id):
            self.put_photo(household, photo.model_copy(update={"cat_id": to_cat_id}))
        return record

    # photos and audit
    def put_photo(self, household, photo):
        self._t.put_item(Item={"pk": self._epk(household, photo.event_id), "sk": f"PH#{photo.photo_id}", "kind": "photo",
                               "data": _to_dynamo(photo.model_dump(mode="json"))})

    def list_photos(self, household, event_id):
        from boto3.dynamodb.conditions import Key

        items = self._query_all(KeyConditionExpression=Key("pk").eq(self._epk(household, event_id)) & Key("sk").begins_with("PH#"))
        return sorted((Photo.model_validate(_from_dynamo(i["data"])) for i in items), key=lambda p: p.uploaded_at)

    def put_reassignment(self, household, item):
        self._t.put_item(Item={"pk": self._epk(household, item.event_id), "sk": f"RA#{item.reassignment_id}", "kind": "reassignment",
                               "data": _to_dynamo(item.model_dump(mode="json"))})

    def list_reassignments(self, household, event_id):
        from boto3.dynamodb.conditions import Key

        items = self._query_all(KeyConditionExpression=Key("pk").eq(self._epk(household, event_id)) & Key("sk").begins_with("RA#"))
        return sorted((Reassignment.model_validate(_from_dynamo(i["data"])) for i in items), key=lambda m: m.corrected_at)


def make_store(kind: str, table_name: str = "") -> Store:
    if kind == "dynamodb":
        if not table_name:
            raise ValueError("STORE=dynamodb needs TABLE_NAME")
        return DynamoStore(table_name)
    if kind == "memory":
        return MemoryStore()
    raise ValueError(f"Unknown STORE {kind!r} (use memory or dynamodb)")
