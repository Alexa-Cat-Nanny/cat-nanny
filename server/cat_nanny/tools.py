"""The MCP tools Alexa+ (or the web simulator) can call.

In MCP mode the voice assistant's own language model understands what the
person said and fills these tools' arguments; the server validates, applies the
deterministic questionnaire and rules from `cat_health_signals`, and stores the
result. No language understanding happens here.

Every result carries `assistant_guidance`: one plain-English instruction telling
the assistant what to say first and what to ask next, so the behaviour does not
depend on the assistant inferring it from raw fields.

The behaviours (one observation per event, follow-up answers update the same
event, unknown answers are not asked again, corrections move the event and its
photos to another cat with an audit record) are ported from CatNannyFramework's
conversation_service and language_action_service.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Annotated, Any, Literal

from cat_health_signals import SUPPORTED_EVENT_TYPES, CatProfile, Event, FollowUp, RedFlag, assess, followup, schemas
from cat_health_signals.summary import PeriodComparison, build_summary
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .photos import PhotoStorage
from .store import (
    Caregiver,
    ObservationRecord,
    Photo,
    Reassignment,
    Store,
    ensure_caregiver,
    find_cat_by_name,
    new_id,
    slug,
)

log = logging.getLogger("cat_nanny.tools")

VET_SUMMARY_URI = "ui://cat-nanny/vet-summary-v2.html"
EventTypeArg = Literal["vomit", "appetite"]

CatName = Annotated[str, Field(description="The cat's name as the person said it, e.g. 'Mochi'. If they say 'she' or 'the cat' and only one cat is set up, use that cat's name.")]
Reporter = Annotated[str | None, Field(description="Name of the family member speaking, if known. Omit rather than guess.")]
OccurredAt = Annotated[str | None, Field(description="When it happened, ISO 8601 with UTC offset (e.g. 2026-10-12T08:40:00-07:00). Convert relative times like 'ten minutes ago' yourself. Omit if it just happened.")]


# --- result shapes ---------------------------------------------------------------


class PhotoView(BaseModel):
    photo_id: str
    url: str
    uploaded_at: str


class ObservationView(BaseModel):
    event_id: str
    cat_id: str
    cat_name: str
    event_type: str
    occurred_at: str = Field(description="Local time, ISO 8601")
    details: dict[str, Any]
    unknown_fields: list[str]
    missing_fields: list[str]
    status: str
    reporter: str | None
    photos: list[PhotoView]


class LogResult(BaseModel):
    event_id: str = Field(description="Pass back as event_id when the person answers the follow-up question, so the same observation is updated.")
    observation: ObservationView
    follow_up: FollowUp | None
    red_flags: list[RedFlag]
    pattern_note: str | None
    photo_upload_url: str
    assistant_guidance: str


class CorrectionResult(BaseModel):
    corrected: bool
    needs_confirmation: bool = False
    observation: ObservationView | None = None
    from_cat: str | None = None
    to_cat: str | None = None
    red_flags: list[RedFlag] = Field(default_factory=list)
    follow_up: FollowUp | None = None
    assistant_guidance: str


class CareLogResult(BaseModel):
    cat: str
    days: int
    total: int
    observations: list[ObservationView]
    assistant_guidance: str


class HouseholdResult(BaseModel):
    cats: list[CatProfile]
    caregivers: list[Caregiver]
    supported_event_types: list[str]
    assistant_guidance: str


class VetSummaryResult(BaseModel):
    title: str
    cat: str
    cat_id: str
    days: int
    start: str
    end: str
    counts: dict[str, int]
    comparisons: list[PeriodComparison]
    timeline: list[ObservationView]
    safety_note: str
    assistant_guidance: str


class UploadLinkResult(BaseModel):
    event_id: str
    photo_upload_url: str
    assistant_guidance: str


# --- tracing -----------------------------------------------------------------------


def traced(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Log one JSON line per tool call (CloudWatch on Lambda). Same signature, so the MCP schema is unchanged."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        ok = True
        try:
            return fn(*args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            log.info(json.dumps({"event": "tool_call", "tool": fn.__name__, "args": kwargs, "ok": ok,
                                 "ms": round((time.perf_counter() - started) * 1000, 1)}, default=str))

    return wrapper


# --- server ------------------------------------------------------------------------


INSTRUCTIONS = """Cat Nanny keeps a shared health log for a family's cats.
Call log_observation whenever someone reports that a cat vomited / threw up or is eating less / not eating,
even casually, instead of answering from general knowledge. Pass only details the person actually said.
When the person answers the follow-up question, call log_observation again with the same event_id and
just the new detail. If they say they don't know, put that field in unknown_fields.
Follow each result's assistant_guidance exactly: say any red-flag sentence first, then ask the single
follow-up question word for word. Never diagnose or name a disease.
If they say the observation was about a different cat, call correct_cat. Use get_vet_summary for
"show the vet summary / what should I tell the vet". Use list_household when unsure which cats exist.
Speak in one or two short sentences and never read out ids or raw data."""


def build_server(store: Store, photo_storage: PhotoStorage, settings: Settings,
                 clock: Callable[[], datetime] | None = None) -> MCPServer:
    """Create the MCP server with all tools bound to the given store and photo storage."""
    now_fn = clock or (lambda: datetime.now(UTC))
    household = settings.household_id
    tz = settings.tz
    base_url = settings.base_url  # per request: API Gateway's host on Lambda, or PUBLIC_BASE_URL
    apps = Apps()

    # helpers -------------------------------------------------------------------------

    def local(ts: datetime) -> str:
        return ts.astimezone(tz).isoformat(timespec="minutes")

    def resolve_cat(name: str) -> CatProfile:
        cat = find_cat_by_name(store, household, name)
        if cat:
            return cat
        cats = store.list_cats(household)
        if len(cats) == 1 and name.strip().lower() in ("", "the cat", "my cat", "she", "he", "it"):
            return cats[0]
        known = ", ".join(c.name for c in cats) or "none yet"
        raise ToolError(f"No cat named {name!r} is set up. Known cats: {known}. Ask which cat, or use add_cat.")

    def parse_time(value: str | None) -> datetime:
        now = now_fn()
        if not value:
            return now
        try:
            ts = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ToolError(f"occurred_at must be ISO 8601, got {value!r}") from exc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        return min(ts, now)

    def upload_url(event_id: str) -> str:
        return f"{base_url()}/photos/upload?event_id={event_id}"

    def photo_views(event_id: str) -> list[PhotoView]:
        return [PhotoView(photo_id=p.photo_id, url=photo_storage.view_url(p.storage_key, base_url()), uploaded_at=local(p.uploaded_at))
                for p in store.list_photos(household, event_id)]

    def view(record: ObservationRecord, cat: CatProfile | None = None) -> ObservationView:
        e = record.event
        cat = cat or store.get_cat(household, e.cat_id)
        return ObservationView(
            event_id=e.event_id, cat_id=e.cat_id, cat_name=cat.name if cat else e.cat_id, event_type=e.type,
            occurred_at=local(e.ts), details=e.details, unknown_fields=record.unknown_fields,
            missing_fields=followup.missing_fields(e.type, e.details, record.unknown_fields),
            status=record.status, reporter=e.reporter, photos=photo_views(e.event_id))

    def history_for(cat_id: str, around: datetime, days: int = 60) -> list[Event]:
        return [r.event for r in store.query_observations(household, cat_id, around - timedelta(days=days), around + timedelta(days=1))]

    def guidance_for(assessment_flags: list[RedFlag], follow_up: FollowUp | None, cat: CatProfile, pattern_note: str | None) -> str:
        parts = []
        if assessment_flags:
            parts.append(f'Say first: "{assessment_flags[0].say_first}"')
        else:
            parts.append(f"Confirm it is logged for {cat.name}" + (f" and mention: {pattern_note}." if pattern_note else "."))
        if follow_up:
            parts.append(f'Then ask exactly: "{follow_up.question}"')
        else:
            parts.append("All details are recorded; offer the photo upload link only if they ask for it.")
        return " ".join(parts)

    # tool registry ----------------------------------------------------------------------

    tool_defs: list[tuple[Callable[..., Any], dict[str, Any]]] = []

    def tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_defs.append((traced(fn), kwargs))
            return fn

        return deco

    # tools ----------------------------------------------------------------------------

    @tool(
        title="List cats and caregivers",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description="The cats and family members Cat Nanny knows about in this household, and which observation "
        "types are supported. Use when unsure which cat the person means or before adding a cat.",
    )
    def list_household() -> HouseholdResult:
        cats = store.list_cats(household)
        return HouseholdResult(cats=cats, caregivers=store.list_caregivers(household),
                               supported_event_types=list(SUPPORTED_EVENT_TYPES),
                               assistant_guidance="Name the cats in one short sentence." if cats else "Say no cat is set up yet and offer to add one.")

    @tool(
        title="Add a cat",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        description="Create a profile for a cat the first time it is mentioned ('add a cat named Jeremy', 'my cat is Mochi'). "
        "Returns the saved profile. Names must be distinct; an existing name is returned unchanged.",
    )
    def add_cat(
        name: Annotated[str, Field(min_length=1, max_length=80, description="The cat's name")],
        sex: Annotated[Literal["female", "male", "unknown"], Field(description="female, male, or unknown")] = "unknown",
        birth_year: Annotated[int | None, Field(ge=1980, le=2100, description="Year of birth, if known")] = None,
    ) -> CatProfile:
        existing = find_cat_by_name(store, household, name)
        if existing:
            return existing
        cat = CatProfile(cat_id=f"cat-{slug(name)}", name=name.strip(), sex=sex, birth_year=birth_year)
        store.put_cat(household, cat)
        return cat

    @tool(
        title="Log a health observation",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        description="Record one observation about a cat: vomiting / throwing up / puking / hairball (event_type vomit), "
        "or eating less / not eating (event_type appetite). Use whenever the person reports such a thing, even casually. "
        "Pass only the details they actually said. When they answer a follow-up question, call again with the same "
        "event_id and only the new detail; if they say they don't know, list the field in unknown_fields. "
        "Returns the follow-up question to ask next, any red flags that need a vet, and assistant_guidance.\n"
        "Detail keys for vomit:\n" + schemas.describe_fields("vomit") + "\nDetail keys for appetite:\n" + schemas.describe_fields("appetite"),
    )
    def log_observation(
        cat: CatName,
        event_type: Annotated[EventTypeArg, Field(description="vomit or appetite")],
        details: Annotated[dict[str, Any] | None, Field(description="Only the detail keys the person stated (see the list above). Never invent values.")] = None,
        event_id: Annotated[str | None, Field(description="Only when adding details to an observation logged earlier in this conversation.")] = None,
        unknown_fields: Annotated[list[str] | None, Field(description="Detail keys the person said they do not know, so they are not asked again.")] = None,
        occurred_at: OccurredAt = None,
        reporter: Reporter = None,
    ) -> LogResult:
        profile = resolve_cat(cat)
        person = ensure_caregiver(store, household, reporter)
        new_details = {k: v for k, v in (details or {}).items() if v is not None}
        now = now_fn()

        if event_id:
            record = store.get_observation(household, event_id)
            if record is None:
                raise ToolError(f"No observation {event_id!r}; log it as a new observation instead.")
            if record.event.cat_id != profile.cat_id:
                raise ToolError(f"Observation {event_id} belongs to {view(record).cat_name}, not {profile.name}. Use correct_cat to move it.")
            if record.event.type != event_type:
                raise ToolError("A different kind of observation needs a new event; omit event_id.")
            merged = {**record.event.details, **new_details}
            ts = parse_time(occurred_at) if occurred_at else record.event.ts
            reporter_name = record.event.reporter or (person.name if person else None)
            unknown = set(record.unknown_fields)
        else:
            merged = new_details
            ts = parse_time(occurred_at)
            event_id = new_id(ts)
            reporter_name = person.name if person else None
            unknown = set()

        try:
            validated = schemas.validate_details(event_type, merged)
        except ValidationError as exc:
            raise ToolError(f"Invalid details for {event_type}: {exc.errors()[0]['msg']} at {exc.errors()[0]['loc']}") from exc
        for key in unknown_fields or []:
            if key in validated and validated[key] is None:
                unknown.add(key)
        unknown -= {k for k, v in validated.items() if v is not None}

        event = Event(event_id=event_id, cat_id=profile.cat_id, type=event_type, ts=ts, details=validated,
                      reporter=reporter_name, source="voice")
        assessment = assess(event, history_for(profile.cat_id, ts), profile, now, unknown_fields=unknown)
        record = ObservationRecord(event=event, status="awaiting_answer" if assessment.follow_up else "completed",
                                   unknown_fields=sorted(unknown), updated_at=now)
        store.put_observation(household, record)

        return LogResult(event_id=event_id, observation=view(record, profile), follow_up=assessment.follow_up,
                         red_flags=assessment.red_flags, pattern_note=assessment.pattern_note,
                         photo_upload_url=upload_url(event_id),
                         assistant_guidance=guidance_for(assessment.red_flags, assessment.follow_up, profile, assessment.pattern_note))

    @tool(
        title="Move an observation to another cat",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        description="Use when the person says the current observation was about a different cat ('no, actually it was "
        "Jeremy', 'I meant Tofu'). Moves only that observation and its photos; details, time and other history stay. "
        "If the target cat has no profile, the result asks you to confirm with the person before calling again with "
        "create_if_missing=true.",
    )
    def correct_cat(
        event_id: Annotated[str, Field(description="The observation to move (from the earlier log_observation result)")],
        to_cat: Annotated[str, Field(description="The cat it should belong to")],
        create_if_missing: Annotated[bool, Field(description="true only after the person confirmed creating a new profile for to_cat")] = False,
        reporter: Reporter = None,
        reason: Annotated[str | None, Field(description="What the person said, for the audit record")] = None,
    ) -> CorrectionResult:
        record = store.get_observation(household, event_id)
        if record is None:
            raise ToolError(f"No observation {event_id!r} to correct.")
        from_cat = store.get_cat(household, record.event.cat_id)
        from_name = from_cat.name if from_cat else record.event.cat_id
        target = find_cat_by_name(store, household, to_cat)
        if target is None and not create_if_missing:
            return CorrectionResult(
                corrected=False, needs_confirmation=True, from_cat=from_name, to_cat=to_cat.strip(),
                assistant_guidance=f'Ask exactly: "I do not have a profile for {to_cat.strip()}. Should I create it and move only this '
                f'observation, including its photos, from {from_name} to {to_cat.strip()}?" If they say yes, call correct_cat again '
                "with create_if_missing=true; if no, say nothing was changed.")
        if target is None:
            target = add_cat(name=to_cat)
        person = ensure_caregiver(store, household, reporter)
        now = now_fn()
        if target.cat_id != record.event.cat_id:
            record = store.move_observation(household, event_id, target.cat_id)
            store.put_reassignment(household, Reassignment(
                reassignment_id=new_id(now), event_id=event_id, from_cat_id=from_cat.cat_id if from_cat else record.event.cat_id,
                to_cat_id=target.cat_id, reporter=person.name if person else None, reason=reason, corrected_at=now))
        # Rules are re-run against the new cat's history (e.g. the 24-hour count belongs to that cat).
        assessment = assess(record.event, history_for(target.cat_id, record.event.ts), target, now, unknown_fields=record.unknown_fields)
        parts = [f"Say: \"Corrected. This same observation is now recorded for {target.name}, not {from_name}. Its details, time and photos were kept.\""]
        if assessment.red_flags:
            parts.append(f'Then say: "{assessment.red_flags[0].say_first}"')
        if assessment.follow_up:
            parts.append(f'Then ask exactly: "{assessment.follow_up.question}"')
        return CorrectionResult(corrected=True, observation=view(record, target), from_cat=from_name, to_cat=target.name,
                                red_flags=assessment.red_flags, follow_up=assessment.follow_up, assistant_guidance=" ".join(parts))

    @tool(
        title="Care log",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description="A cat's recent observations, newest first, with who logged each and when. Use for 'how has Mochi "
        "been?', 'did anyone log anything today?', 'what happened this week?'.",
    )
    def get_care_log(cat: CatName, days: Annotated[int, Field(ge=1, le=365, description="How many days back")] = 7,
                     limit: Annotated[int, Field(ge=1, le=50)] = 20) -> CareLogResult:
        profile = resolve_cat(cat)
        now = now_fn()
        rows = store.query_observations(household, profile.cat_id, now - timedelta(days=days), now)
        rows = sorted(rows, key=lambda r: r.event.ts, reverse=True)
        views = [view(r, profile) for r in rows[:limit]]
        guidance = (f"Summarise the {len(rows)} observation(s) in one or two sentences, naming who logged them and when."
                    if rows else f"Say nothing has been logged for {profile.name} in the last {days} days.")
        return CareLogResult(cat=profile.name, days=days, total=len(rows), observations=views, assistant_guidance=guidance)

    @tool(
        title="Photo upload link",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description="A link the person opens on their phone to attach a photo to an observation ('send me the photo "
        "link'). The link is shown on screen; nothing is texted or emailed.",
    )
    def get_photo_upload_link(event_id: Annotated[str, Field(description="The observation to attach the photo to")]) -> UploadLinkResult:
        if store.get_observation(household, event_id) is None:
            raise ToolError(f"No observation {event_id!r}. Log the observation first so the photo can be attached to it.")
        return UploadLinkResult(event_id=event_id, photo_upload_url=upload_url(event_id),
                                assistant_guidance="Say the photo link is on screen and that nothing was texted or emailed.")

    # UI tool -------------------------------------------------------------------------

    def get_vet_summary(cat: CatName, days: Annotated[int, Field(ge=1, le=90, description="How many days to cover")] = 7
                        ) -> Annotated[CallToolResult, VetSummaryResult]:
        profile = resolve_cat(cat)
        now = now_fn()
        start = now - timedelta(days=days)
        rows = store.query_observations(household, profile.cat_id, start, now)
        history = [r.event for r in store.query_observations(household, profile.cat_id, now - timedelta(days=2 * days), now)]
        comparisons = build_summary(history, profile, now, days=days, tz=tz).comparisons
        counts = {t: sum(1 for r in rows if r.event.type == t) for t in SUPPORTED_EVENT_TYPES}
        counts["total_events"] = len(rows)
        result = VetSummaryResult(
            title="Cat Nanny Vet Summary", cat=profile.name, cat_id=profile.cat_id, days=days,
            start=local(start), end=local(now), counts=counts, comparisons=comparisons,
            timeline=[view(r, profile) for r in sorted(rows, key=lambda r: r.event.ts, reverse=True)],
            safety_note="Cat Nanny describes observations and does not diagnose. Contact a veterinarian for medical advice.",
            assistant_guidance="Say the summary is on screen and give the counts in one sentence (e.g. how many vomiting "
                               "episodes versus the previous period). Do not read the timeline aloud.")
        vomit = next((c for c in comparisons if c.event_type == "vomit"), None)
        text = f"{profile.name}, last {days} days: {counts['total_events']} observation(s)" + (
            f"; {vomit.current} vomiting vs {vomit.previous} in the previous {days} days." if vomit else ".")
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=result.model_dump(mode="json"),
            # Alexa+ docs read resourceUri from the tool *result*; the MCP Apps spec reads it from the tool
            # definition (set by apps.tool below). Sending both works with either host.
            _meta={"ui": {"resourceUri": VET_SUMMARY_URI, "invoking": "Preparing the vet summary...", "invoked": "Vet summary ready"}},
        )

    apps.tool(
        resource_uri=VET_SUMMARY_URI,
        name="get_vet_summary",
        title="Vet summary",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description="A summary of a cat's observations to show a vet: counts for the period versus the previous one, "
        "and a timeline with details and photos, shown on screen. Use for 'show Mochi's summary for the vet', "
        "'what should I tell the vet?', 'show me the care report'.",
    )(traced(get_vet_summary))

    apps.add_html_resource(
        VET_SUMMARY_URI,
        files("cat_nanny.ui").joinpath("vet_summary.html").read_text(encoding="utf-8"),
        name="Vet summary",
        description="Counts and timeline for a vet visit",
        csp=ResourceCsp(connect_domains=[], resource_domains=["*"]),
        prefers_border=False,
    )

    mcp = MCPServer("cat-nanny", title="Cat Nanny", version="0.2.0", instructions=INSTRUCTIONS, extensions=[apps])
    for fn, kwargs in tool_defs:
        mcp.add_tool(fn, name=fn.__name__, **kwargs)
    return mcp


# --- photo registration (used by the HTTP upload routes in app.py) --------------------


def register_photo(store: Store, household: str, event_id: str, storage_key: str, content_type: str, now: datetime) -> Photo:
    record = store.get_observation(household, event_id)
    if record is None:
        raise KeyError(event_id)
    photo = Photo(photo_id=storage_key.rsplit("/", 1)[-1].split(".")[0], event_id=event_id, cat_id=record.event.cat_id,
                  storage_key=storage_key, content_type=content_type, uploaded_at=now)
    store.put_photo(household, photo)
    return photo
