"""In-process tests of the MCP tools (no HTTP, no AWS)."""

from datetime import UTC, datetime

import pytest
from mcp import Client

from cat_nanny.config import Settings
from cat_nanny.demo_data import seed_store
from cat_nanny.notify import LogNotifier
from cat_nanny.store import MemoryStore
from cat_nanny.tools import VET_SUMMARY_URI, build_server

NOW = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env():
    store, notifier = MemoryStore(), LogNotifier()
    seed_store(store, "demo", now=NOW)
    server = build_server(store, notifier, Settings(), clock=lambda: NOW)
    return server, store, notifier


async def call(server, name, args):
    async with Client(server) as c:
        r = await c.call_tool(name, args)
        assert not r.is_error, r.content
        return r


async def test_lists_all_tools_with_output_schemas(env):
    server, *_ = env
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"add_cat", "update_cat", "log_health_event", "log_care_event", "get_today",
                          "get_patterns", "get_vet_summary", "acknowledge_alerts"}
    assert tools["get_vet_summary"].meta["ui"]["resourceUri"] == VET_SUMMARY_URI
    assert tools["get_today"].annotations.read_only_hint is True
    assert all(t.output_schema for t in tools.values())
    assert "content (what came up" in tools["log_health_event"].description


async def test_follow_up_then_complete_same_event(env):
    server, store, _ = env
    r1 = await call(server, "log_health_event", {"cat": "Mochi", "type": "vomit", "reporter": "Alex"})
    first = r1.structured_content
    assert first["follow_up"]["field"] == "content"
    assert "Was it food" in first["assistant_guidance"]

    r2 = await call(server, "log_health_event", {"cat": "mochi", "type": "vomit", "event_id": first["event_id"], "details": {"content": "food"}})
    second = r2.structured_content
    assert second["event_id"] == first["event_id"]
    assert second["follow_up"] is None
    assert second["logged"]["details"] == {"content": "food"}


async def test_red_flag_emails_family_once_and_repeats_until_acknowledged(env):
    server, _, notifier = env
    r = await call(server, "log_health_event", {"cat": "Mochi", "type": "vomit", "details": {"content": "food", "blood": True}, "reporter": "Alex"})
    sc = r.structured_content
    assert sc["red_flags"][0]["level"] == "vet_today" and sc["family_notified"]
    assert sc["assistant_guidance"].startswith('Say first: "Please call your vet today."')
    assert len(notifier.sent) == 1

    # Same event updated again: no duplicate email.
    await call(server, "log_health_event", {"cat": "Mochi", "type": "vomit", "event_id": sc["event_id"], "details": {"minutes_after_meal": 5}})
    assert len(notifier.sent) == 1

    # Another family member asks something else: the alert is repeated.
    today = (await call(server, "get_today", {"cat": "Mochi"})).structured_content
    assert today["pending_alerts"] and "Alex reported" in today["pending_alerts"][0]["message"]

    ack = (await call(server, "acknowledge_alerts", {"cat": "Mochi", "reporter": "Sam", "note": "called the vet"})).structured_content
    assert ack["acknowledged"] == 1
    assert (await call(server, "get_today", {"cat": "Mochi"})).structured_content["pending_alerts"] == []


async def test_care_event_and_food_change_updates_profile(env):
    server, store, _ = env
    r = (await call(server, "log_care_event", {"cat": "Mochi", "type": "food_change", "details": {"from": "Brand Y dry", "to": "Brand Z wet"}})).structured_content
    assert r["logged"]["type"] == "food_change"
    assert store.get_cat("demo", "mochi").current_food == "Brand Z wet"


async def test_vet_summary_has_ui_and_readable_resource(env):
    server, *_ = env
    async with Client(server) as c:
        r = await c.call_tool("get_vet_summary", {"cat": "Mochi"})
        assert r.meta["ui"]["resourceUri"] == VET_SUMMARY_URI
        s = r.structured_content["summary"]
        assert len(s["timeline"]) == 30 and s["weight"]["last_kg"] == 4.0
        res = await c.read_resource(VET_SUMMARY_URI)
        assert res.contents[0].mime_type == "text/html;profile=mcp-app"
        assert "ui/initialize" in res.contents[0].text


async def test_unknown_cat_is_a_tool_error(env):
    server, store, _ = env
    await call(server, "add_cat", {"name": "Tofu", "sex": "male"})
    async with Client(server) as c:
        r = await c.call_tool("get_today", {"cat": "Biscuit"})
    assert r.is_error and "Known cats" in r.content[0].text
