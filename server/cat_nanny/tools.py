"""The MCP tools Alexa+ (or the web simulator) can call.

Each tool is thin: validate input -> read/write the store -> ask
cat_health_signals what it concludes -> shape the result. No health logic lives
here. The only decision made here is orchestration: when a red flag is
`emergency` or `vet_today`, email the family and keep an alert open until
someone acknowledges it.

Every result carries `assistant_guidance`: one plain-English instruction telling
the assistant what to say first and what to ask next, so the behaviour does not
depend on the assistant inferring it from raw fields.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from cat_health_signals import CatProfile, Event, FollowUp, Pattern, RedFlag, assess
from cat_health_signals import fields as signal_fields
from cat_health_signals import patterns as signal_patterns
from cat_health_signals.summary import PeriodComparison, VetSummaryData, build_summary
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .config import Settings
from .notify import Notifier
from .store import Alert, Store, cat_id_from_name, new_id

log = logging.getLogger("cat_nanny.tools")

HEALTH_TYPES = ("vomit", "appetite", "litter", "energy")
CARE_TYPES = ("meal", "medication", "weight", "food_change", "vet_visit")
HISTORY_DAYS = 60
VET_SUMMARY_URI = "ui://cat-nanny/vet-summary-v1.html"

CatName = Annotated[str, Field(description="The cat's name as the user said it, e.g. 'Mochi'. If the user says 'she' or 'the cat' and only one cat is set up, use that cat's name.")]
Reporter = Annotated[str | None, Field(description="Name of the family member reporting, if known. Omit rather than guess.")]
OccurredAt = Annotated[
    str | None,
    Field(description="When it happened, ISO 8601 with UTC offset (e.g. 2026-10-12T08:40:00-07:00). Convert relative times like 'ten minutes ago' yourself. Omit if it just happened."),
]


# --- result shapes ---------------------------------------------------------------


class EventView(BaseModel):
    event_id: str
    type: str
    at: str = Field(description="Local time, ISO 8601")
    details: dict[str, Any]
    reporter: str | None = None


class AlertView(BaseModel):
    alert_id: str
    level: str
    message: str
    created_at: str
    reporter: str | None = None


class LogHealthResult(BaseModel):
    event_id: str = Field(description="Pass back as event_id to add details the user gives in their next answer.")
    logged: EventView
    follow_up: FollowUp | None
    red_flags: list[RedFlag]
    pattern_note: str | None
    family_notified: bool
    pending_alerts: list[AlertView]
    assistant_guidance: str


class TodayItem(BaseModel):
    at: str
    reporter: str | None
    details: dict[str, Any]


class TodayResult(BaseModel):
    cat: str
    date: str
    meals: list[TodayItem]
    meals_expected: int | None
    medications: list[TodayItem]
    medication_schedule: str | None
    symptoms: list[EventView]
    not_done_yet: list[str]
    pending_alerts: list[AlertView]
    assistant_guidance: str


class LogCareResult(BaseModel):
    event_id: str
    logged: EventView
    today: TodayResult
    pending_alerts: list[AlertView]
    assistant_guidance: str


class PatternsResult(BaseModel):
    cat: str
    days: int
    patterns: list[Pattern]
    comparisons: list[PeriodComparison]
    pending_alerts: list[AlertView]
    assistant_guidance: str


class VetSummaryResult(BaseModel):
    summary: VetSummaryData
    alerts_in_period: list[AlertView]
    pending_alerts: list[AlertView]
    assistant_guidance: str


class AckResult(BaseModel):
    acknowledged: int
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


INSTRUCTIONS = """Cat Nanny keeps a shared health log for the family's cat(s).
Call log_health_event whenever someone reports something about how the cat is (vomiting, not eating,
litter box changes, low energy), even casually. Call log_care_event when someone fed, medicated or
weighed the cat, changed food, or went to the vet. Follow each result's assistant_guidance exactly:
say any red-flag sentence first, then ask the single follow-up question if there is one.
Never diagnose or name a disease; describe observations and recommend the vet when told to.
Speak in one or two short sentences and never read out ids or raw data."""


def build_server(store: Store, notifier: Notifier, settings: Settings, clock: Callable[[], datetime] | None = None) -> MCPServer:
    """Create the MCP server with all tools bound to the given store and notifier."""
    now_fn = clock or (lambda: datetime.now(UTC))
    household = settings.household_id
    tz = settings.tz
    apps = Apps()

    # helpers -------------------------------------------------------------------------

    def local(ts: datetime) -> str:
        return ts.astimezone(tz).isoformat(timespec="minutes")

    def resolve_cat(name: str) -> CatProfile:
        cat = store.get_cat(household, cat_id_from_name(name))
        if cat:
            return cat
        cats = store.list_cats(household)
        if len(cats) == 1:
            return cats[0]
        known = ", ".join(c.name for c in cats) or "none yet"
        raise ToolError(f"No cat named {name!r} is set up. Known cats: {known}. Use add_cat first.")

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

    def event_view(e: Event) -> EventView:
        return EventView(event_id=e.event_id, type=e.type, at=local(e.ts), details=e.details, reporter=e.reporter)

    def alert_view(a: Alert) -> AlertView:
        return AlertView(alert_id=a.alert_id, level=a.level, message=a.message, created_at=local(a.created_at), reporter=a.reporter)

    def open_alerts(cat: CatProfile, exclude_event: str | None = None) -> list[AlertView]:
        since = now_fn() - timedelta(hours=settings.alert_window_hours)
        alerts = store.list_alerts(household, cat.cat_id, since, open_only=True)
        return [alert_view(a) for a in alerts if a.source_event_id != exclude_event][:3]

    def alerts_guidance(pending: list[AlertView]) -> str:
        if not pending:
            return ""
        return (f" Also tell them: {pending[0].message} Ask if the vet has been contacted; if they say yes or that they"
                " know, call acknowledge_alerts.")

    def raise_alerts(cat: CatProfile, event: Event, flags: list[RedFlag]) -> bool:
        """Store and email alerts for emergency / vet_today flags. Returns True if anything new was sent."""
        urgent = [f for f in flags if f.level in ("emergency", "vet_today")]
        if not urgent:
            return False
        since = now_fn() - timedelta(hours=settings.alert_window_hours)
        existing = {(a.source_event_id, a.rule_id) for a in store.list_alerts(household, cat.cat_id, since, open_only=False)}
        sent = False
        for flag in urgent:
            if (event.event_id, flag.rule_id) in existing:
                continue
            who = event.reporter or "Someone"
            message = f"{who} reported at {event.ts.astimezone(tz):%-I:%M %p}: {flag.reason} {flag.say_first}"
            store.put_alert(household, Alert(alert_id=new_id(now_fn()), cat_id=cat.cat_id, level=flag.level, message=message,
                                             created_at=now_fn(), source_event_id=event.event_id, rule_id=flag.rule_id,
                                             reporter=event.reporter))
            notifier.notify_family(f"Cat Nanny: {cat.name} needs attention", message)
            sent = True
        return sent

    def history(cat: CatProfile, days: int = HISTORY_DAYS) -> list[Event]:
        now = now_fn()
        return store.query_events(household, cat.cat_id, now - timedelta(days=days), now)

    # server -------------------------------------------------------------------------

    mcp: MCPServer[Any]  # assigned below; tools close over it only through decorators

    tool_defs: list[tuple[Callable[..., Any], dict[str, Any]]] = []

    def tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_defs.append((traced(fn), kwargs))
            return fn

        return deco

    # tools ----------------------------------------------------------------------------

    @tool(
        title="Add a cat",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        description="Set up a cat's profile the first time, or add another cat to the household. Use when the user "
        "introduces their cat ('my cat is Mochi, she's a 2019 female'). Returns the saved profile.",
    )
    def add_cat(
        name: Annotated[str, Field(description="The cat's name")],
        sex: Annotated[Literal["female", "male", "unknown"], Field(description="female, male, or unknown")] = "unknown",
        neutered: Annotated[bool | None, Field(description="Spayed/neutered, if known")] = None,
        birth_year: Annotated[int | None, Field(description="Year of birth, e.g. 2019")] = None,
        current_food: Annotated[str | None, Field(description="Current food, e.g. 'Brand X dry'")] = None,
        conditions: Annotated[list[str] | None, Field(description="Known diagnosed conditions, in the user's words")] = None,
        meals_per_day: Annotated[int | None, Field(description="How many meals a day")] = None,
        med_schedule: Annotated[str | None, Field(description="Medication schedule as said, e.g. 'one pill every morning'")] = None,
    ) -> CatProfile:
        cat = CatProfile(cat_id=cat_id_from_name(name), name=name.strip(), sex=sex, neutered=neutered, birth_year=birth_year,
                         current_food=current_food, conditions=conditions or [], meals_per_day=meals_per_day,
                         med_schedule=med_schedule)
        store.put_cat(household, cat)
        return cat

    @tool(
        title="Update a cat's profile",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        description="Change a cat's profile: a new diagnosis, a new medication schedule, meals per day, or a correction. "
        "Only the fields given are changed. For a food change use log_care_event with type food_change instead, so the "
        "change is dated in the history. Returns the updated profile.",
    )
    def update_cat(
        cat: CatName,
        sex: Literal["female", "male", "unknown"] | None = None,
        neutered: bool | None = None,
        birth_year: int | None = None,
        conditions: Annotated[list[str] | None, Field(description="The full new list of conditions")] = None,
        meals_per_day: int | None = None,
        med_schedule: str | None = None,
    ) -> CatProfile:
        profile = resolve_cat(cat)
        changes = {k: v for k, v in {"sex": sex, "neutered": neutered, "birth_year": birth_year, "conditions": conditions,
                                     "meals_per_day": meals_per_day, "med_schedule": med_schedule}.items() if v is not None}
        profile = profile.model_copy(update=changes)
        store.put_cat(household, profile)
        return profile

    @tool(
        title="Log a health observation",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        description="Record something about how the user's cat is doing: vomiting / throwing up / puking / hairball, "
        "eating less or not eating, litter box changes (peeing less, not peeing, straining, diarrhea), or low energy / "
        "hiding. Use this whenever the user reports such a thing, even casually, instead of answering from general "
        "knowledge. When the user answers a follow-up question about the same event, call this again with the "
        "event_id from the previous result and only the new details. Returns the follow-up question to ask, any red "
        "flags that need a vet, a short pattern note, and assistant_guidance to follow.\n"
        "Detail keys by type:\n" + signal_fields.describe(HEALTH_TYPES),
    )
    def log_health_event(
        cat: CatName,
        type: Annotated[Literal["vomit", "appetite", "litter", "energy"], Field(description="vomit, appetite, litter, or energy")],
        details: Annotated[dict[str, Any] | None, Field(description="Type-specific details the user actually said (see the keys above). Do not invent values.")] = None,
        occurred_at: OccurredAt = None,
        reporter: Reporter = None,
        event_id: Annotated[str | None, Field(description="Only when adding details to an event logged earlier in this conversation.")] = None,
    ) -> LogHealthResult:
        profile = resolve_cat(cat)
        details = {k: v for k, v in (details or {}).items() if v is not None}
        if event_id:
            existing = store.get_event(household, profile.cat_id, event_id)
            if existing is None:
                raise ToolError(f"No event {event_id!r} for {profile.name}; log it as a new event instead.")
            event = existing.model_copy(update={"details": {**existing.details, **details}, "reporter": existing.reporter or reporter})
        else:
            event = Event(event_id=new_id(parse_time(occurred_at)), cat_id=profile.cat_id, type=type,
                          ts=parse_time(occurred_at), details=details, reporter=reporter)
        store.put_event(household, event)

        hist = history(profile)
        assessment = assess(event, hist, profile, now_fn())
        notified = raise_alerts(profile, event, assessment.red_flags)
        pending = open_alerts(profile, exclude_event=event.event_id)

        parts = []
        if assessment.red_flags:
            parts.append(f"Say first: \"{assessment.red_flags[0].say_first}\"")
            if notified:
                parts.append("Then say the family has been emailed.")
        else:
            parts.append("Confirm it is logged" + (f" and mention: {assessment.pattern_note}." if assessment.pattern_note else "."))
        if assessment.follow_up:
            parts.append(f"Then ask exactly: \"{assessment.follow_up.question}\"")
        guidance = " ".join(parts) + alerts_guidance(pending)

        return LogHealthResult(event_id=event.event_id, logged=event_view(event), follow_up=assessment.follow_up,
                               red_flags=assessment.red_flags, pattern_note=assessment.pattern_note,
                               family_notified=notified, pending_alerts=pending, assistant_guidance=guidance)

    def today_result(profile: CatProfile) -> TodayResult:
        now = now_fn()
        start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        events = store.query_events(household, profile.cat_id, start, now)
        item = lambda e: TodayItem(at=local(e.ts), reporter=e.reporter, details=e.details)  # noqa: E731
        meals = [item(e) for e in events if e.type == "meal"]
        meds = [item(e) for e in events if e.type == "medication"]
        symptoms = [event_view(e) for e in events if e.type in HEALTH_TYPES]
        not_done: list[str] = []
        if profile.meals_per_day and len(meals) < profile.meals_per_day:
            not_done.append(f"{profile.meals_per_day - len(meals)} meal(s) not logged yet")
        if profile.med_schedule and not meds:
            not_done.append("medication not logged yet")
        pending = open_alerts(profile)
        guidance = "Answer the user's question from these entries, naming who did it and when."
        if not_done:
            guidance += f" Mention what is not done yet: {'; '.join(not_done)}."
        return TodayResult(cat=profile.name, date=start.date().isoformat(), meals=meals, meals_expected=profile.meals_per_day,
                           medications=meds, medication_schedule=profile.med_schedule, symptoms=symptoms,
                           not_done_yet=not_done, pending_alerts=pending,
                           assistant_guidance=guidance + alerts_guidance(pending))

    @tool(
        title="Log care given",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        description="Record care someone gave the cat: fed a meal, gave medication, weighed the cat, switched to a new "
        "food, or went to the vet. Use when the user says they did one of these ('I just fed Mochi', 'gave her the "
        "pill', 'she weighs 4 kilos', 'we switched to Brand Y'). Returns what is still not done today.\n"
        "Detail keys by type:\n" + signal_fields.describe(CARE_TYPES),
    )
    def log_care_event(
        cat: CatName,
        type: Annotated[Literal["meal", "medication", "weight", "food_change", "vet_visit"], Field(description="meal, medication, weight, food_change, or vet_visit")],
        details: Annotated[dict[str, Any] | None, Field(description="Type-specific details the user actually said (see the keys above).")] = None,
        occurred_at: OccurredAt = None,
        reporter: Reporter = None,
    ) -> LogCareResult:
        profile = resolve_cat(cat)
        details = {k: v for k, v in (details or {}).items() if v is not None}
        ts = parse_time(occurred_at)
        event = Event(event_id=new_id(ts), cat_id=profile.cat_id, type=type, ts=ts, details=details, reporter=reporter)
        store.put_event(household, event)
        if type == "food_change" and details.get("to"):
            store.put_cat(household, profile.model_copy(update={"current_food": str(details["to"])}))
        today = today_result(profile)
        return LogCareResult(event_id=event.event_id, logged=event_view(event), today=today, pending_alerts=today.pending_alerts,
                             assistant_guidance="Confirm it is logged in a few words." + alerts_guidance(today.pending_alerts))

    @tool(
        title="What happened today",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description="Today's meals, medication and symptoms for a cat, with who logged each and when. Use for questions "
        "like 'did anyone feed Mochi?', 'did she get her medicine today?', 'how has she been today?'.",
    )
    def get_today(cat: CatName) -> TodayResult:
        return today_result(resolve_cat(cat))

    @tool(
        title="Patterns in the cat's health log",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description="Patterns found in the cat's logged history, such as more vomiting than the previous period, "
        "vomiting soon after meals, changes after a food switch, or weight loss. Use for 'why does she keep throwing up?', "
        "'how has Mochi been lately?', 'is it getting worse?'. Returns observations only, never causes.",
    )
    def get_patterns(cat: CatName, days: Annotated[int, Field(ge=7, le=90, description="How many days to look back")] = 30) -> PatternsResult:
        profile = resolve_cat(cat)
        now = now_fn()
        hist = history(profile, days=2 * days)
        found = signal_patterns.find_patterns(hist, profile, now, days=days)
        comparisons = build_summary(hist, profile, now, days=days, tz=tz).comparisons
        pending = open_alerts(profile)
        guidance = ("Start with: \"I can't say why, but here's what the log shows.\" Then summarise the patterns as observations "
                    "and suggest mentioning them to the vet." if found else
                    "Say no clear pattern stands out yet and give the counts compared with the previous period.")
        return PatternsResult(cat=profile.name, days=days, patterns=found, comparisons=comparisons, pending_alerts=pending,
                              assistant_guidance=guidance + alerts_guidance(pending))

    @tool(
        title="Acknowledge family alerts",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        description="Mark the cat's open alerts as handled, after the user confirms they know about them or have "
        "contacted the vet. Stops the alert from being repeated to other family members.",
    )
    def acknowledge_alerts(cat: CatName, reporter: Reporter = None,
                           note: Annotated[str | None, Field(description="What was done, e.g. 'called the vet'")] = None) -> AckResult:
        profile = resolve_cat(cat)
        since = now_fn() - timedelta(hours=settings.alert_window_hours)
        alerts = store.list_alerts(household, profile.cat_id, since, open_only=True)
        for a in alerts:
            store.put_alert(household, a.model_copy(update={"acknowledged": True, "acknowledged_by": reporter, "note": note}))
        return AckResult(acknowledged=len(alerts), assistant_guidance="Briefly confirm the family alert is cleared.")

    # UI tool -------------------------------------------------------------------------

    def vet_summary(cat: CatName, days: Annotated[int, Field(ge=7, le=90, description="How many days to cover")] = 30
                    ) -> Annotated[CallToolResult, VetSummaryResult]:
        profile = resolve_cat(cat)
        now = now_fn()
        hist = history(profile, days=2 * days)
        data = build_summary(hist, profile, now, days=days, tz=tz)
        in_period = [alert_view(a) for a in store.list_alerts(household, profile.cat_id, now - timedelta(days=days), open_only=False)]
        pending = open_alerts(profile)
        result = VetSummaryResult(summary=data, alerts_in_period=in_period, pending_alerts=pending,
                                  assistant_guidance="Say the summary is on screen and give two or three key numbers in one "
                                  "sentence (counts versus the previous period, food change date, weight change)."
                                  + alerts_guidance(pending))
        vomit = next((c for c in data.comparisons if c.event_type == "vomit"), None)
        text = f"{profile.name}, last {days} days" + (f": {vomit.current} vomiting episodes vs {vomit.previous} before." if vomit else ".")
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
        description="A summary of the cat's health log to show a vet: a day-by-day timeline, counts compared with the "
        "previous period, food changes and weight change, shown on screen. Use for 'show Mochi's summary for the vet', "
        "'what should I tell the vet?', 'prepare for the vet visit'.",
    )(traced(vet_summary))

    from importlib.resources import files

    apps.add_html_resource(
        VET_SUMMARY_URI,
        files("cat_nanny.ui").joinpath("vet_summary.html").read_text(encoding="utf-8"),
        name="Vet summary",
        description="Timeline and totals for a vet visit",
        csp=ResourceCsp(connect_domains=[], resource_domains=[]),
        prefers_border=False,
    )

    mcp = MCPServer("cat-nanny", title="Cat Nanny", version="0.1.0", instructions=INSTRUCTIONS, extensions=[apps])
    for fn, kwargs in tool_defs:
        mcp.add_tool(fn, name=fn.__name__, **kwargs)
    return mcp
