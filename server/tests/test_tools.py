"""In-process tests of the MCP tools (no HTTP, no AWS).

The behaviours come from CatNannyFramework's conversation tests: one observation
per event, follow-ups update the same event, unknown answers are not asked
again, corrections move the event and its photos with an audit record.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp import Client

from cat_nanny.config import Settings
from cat_nanny.demo_data import seed_store
from cat_nanny.photos import LocalPhotoStorage
from cat_nanny.store import MemoryStore
from cat_nanny.tools import VET_SUMMARY_URI, build_server, register_photo

NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(tmp_path: Path):
    store = MemoryStore()
    seed_store(store, "demo", now=NOW)
    server = build_server(store, LocalPhotoStorage(tmp_path), Settings(), clock=lambda: NOW)
    return server, store


async def call(server, name, args):
    async with Client(server) as c:
        r = await c.call_tool(name, args)
        assert not r.is_error, r.content
        return r.structured_content


async def test_lists_all_tools_with_output_schemas(env):
    server, _ = env
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"list_household", "add_cat", "log_observation", "correct_cat", "get_care_log",
                          "get_photo_upload_link", "get_vet_summary"}
    assert tools["get_vet_summary"].meta["ui"]["resourceUri"] == VET_SUMMARY_URI
    assert tools["get_care_log"].annotations.read_only_hint is True
    assert all(t.output_schema for t in tools.values())
    assert "- has_blood:" in tools["log_observation"].description
    assert tools["log_observation"].input_schema["properties"]["event_type"]["enum"] == ["vomit", "appetite"]


async def test_household_listing(env):
    server, _ = env
    r = await call(server, "list_household", {})
    assert [c["name"] for c in r["cats"]] == ["Jeremy", "Mochi"]
    assert r["supported_event_types"] == ["vomit", "appetite"]


async def test_follow_up_updates_the_same_event(env):
    server, store = env
    first = await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"episode_count": 2}, "reporter": "Alex"})
    assert first["follow_up"]["field"] == "content"
    assert 'Then ask exactly: "Was it food, a hairball, liquid, or foam?"' in first["assistant_guidance"]
    assert first["observation"]["status"] == "awaiting_answer"

    second = await call(server, "log_observation", {"cat": "mochi", "event_type": "vomit", "event_id": first["event_id"], "details": {"content": "food"}})
    assert second["event_id"] == first["event_id"]
    assert second["follow_up"]["field"] == "has_blood"
    assert second["observation"]["details"]["episode_count"] == 2  # earlier answer kept
    assert second["observation"]["reporter"] == "Alex"

    third = await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "event_id": first["event_id"], "unknown_fields": ["has_blood"]})
    assert third["follow_up"]["field"] == "episode_count" or third["follow_up"]["field"] == "low_energy"
    assert third["observation"]["unknown_fields"] == ["has_blood"]
    assert len(store.query_observations("demo", "cat-mochi", NOW.replace(year=2020))) == 3  # 2 seeded + 1 new, not 3 new


async def test_red_flag_comes_first_in_guidance(env):
    server, _ = env
    r = await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"content": "food", "has_blood": True}})
    assert r["red_flags"][0]["rule_id"] == "vomit_blood"
    assert r["assistant_guidance"].startswith('Say first: "Please call your vet today.')


async def test_third_vomit_in_24h_counts_that_cats_history_only(env):
    server, _ = env
    await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"content": "food", "has_blood": False},
                                           "occurred_at": "2026-10-15T06:00:00+00:00"})
    await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"content": "liquid", "has_blood": False},
                                           "occurred_at": "2026-10-15T10:00:00+00:00"})
    r = await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"content": "foam", "has_blood": False}})
    assert any(f["rule_id"] == "vomit_three_times_24h" for f in r["red_flags"])
    j = await call(server, "log_observation", {"cat": "Jeremy", "event_type": "vomit", "details": {"content": "foam", "has_blood": False}})
    assert j["red_flags"] == []


async def test_invalid_details_are_rejected_not_coerced(env):
    server, _ = env
    async with Client(server) as c:
        r = await c.call_tool("log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"has_blood": "false"}})
    assert r.is_error and "Invalid details" in r.content[0].text


async def test_correction_asks_before_creating_then_moves_event_and_photos(env):
    server, store = env
    logged = await call(server, "log_observation", {"cat": "Mochi", "event_type": "vomit", "details": {"content": "food"}, "reporter": "Alex"})
    register_photo(store, "demo", logged["event_id"], f"households/demo/events/{logged['event_id']}/photo-1.jpg", "image/jpeg", NOW)

    ask = await call(server, "correct_cat", {"event_id": logged["event_id"], "to_cat": "Tofu"})
    assert ask["needs_confirmation"] and not ask["corrected"]
    assert "Should I create it and move only this observation" in ask["assistant_guidance"]
    assert store.get_observation("demo", logged["event_id"]).event.cat_id == "cat-mochi"

    moved = await call(server, "correct_cat", {"event_id": logged["event_id"], "to_cat": "Tofu", "create_if_missing": True,
                                               "reporter": "Alex", "reason": "no actually it is for Tofu"})
    assert moved["corrected"] and moved["to_cat"] == "Tofu" and moved["from_cat"] == "Mochi"
    assert moved["observation"]["cat_name"] == "Tofu" and moved["observation"]["details"]["content"] == "food"
    assert [p["photo_id"] for p in moved["observation"]["photos"]] == ["photo-1"]
    assert moved["follow_up"]["field"] == "has_blood"
    record = store.get_observation("demo", logged["event_id"])
    assert record.event.cat_id == "cat-tofu" and record.event.event_id == logged["event_id"]
    assert store.list_photos("demo", logged["event_id"])[0].cat_id == "cat-tofu"
    audit = store.list_reassignments("demo", logged["event_id"])
    assert len(audit) == 1 and (audit[0].from_cat_id, audit[0].to_cat_id, audit[0].reporter) == ("cat-mochi", "cat-tofu", "Alex")
    assert store.query_observations("demo", "cat-mochi", NOW.replace(year=2020)) and all(
        r.event.event_id != logged["event_id"] for r in store.query_observations("demo", "cat-mochi", NOW.replace(year=2020)))


async def test_correction_to_known_cat_needs_no_confirmation(env):
    server, _ = env
    logged = await call(server, "log_observation", {"cat": "Mochi", "event_type": "appetite", "details": {"status": "not_eating"}})
    moved = await call(server, "correct_cat", {"event_id": logged["event_id"], "to_cat": "jeremy"})
    assert moved["corrected"] and moved["to_cat"] == "Jeremy"


async def test_care_log_and_vet_summary(env):
    server, _ = env
    log = await call(server, "get_care_log", {"cat": "Mochi", "days": 7})
    assert log["total"] == 2 and log["observations"][0]["event_type"] == "vomit"
    assert log["observations"][0]["status"] == "awaiting_answer"

    async with Client(server) as c:
        r = await c.call_tool("get_vet_summary", {"cat": "Mochi", "days": 7})
        assert r.meta["ui"]["resourceUri"] == VET_SUMMARY_URI
        s = r.structured_content
        assert s["counts"] == {"vomit": 2, "appetite": 0, "total_events": 2}
        assert s["timeline"][0]["cat_name"] == "Mochi"
        res = await c.read_resource(VET_SUMMARY_URI)
        assert res.contents[0].mime_type == "text/html;profile=mcp-app"


async def test_unknown_cat_and_missing_event_are_tool_errors(env):
    server, _ = env
    async with Client(server) as c:
        r = await c.call_tool("get_care_log", {"cat": "Biscuit"})
        assert r.is_error and "Known cats: Jeremy, Mochi" in r.content[0].text
        r = await c.call_tool("get_photo_upload_link", {"event_id": "nope"})
        assert r.is_error


async def test_add_cat_is_idempotent_by_name(env):
    server, _ = env
    a = await call(server, "add_cat", {"name": "Tofu", "sex": "male"})
    b = await call(server, "add_cat", {"name": "tofu"})
    assert a["cat_id"] == b["cat_id"] == "cat-tofu"
