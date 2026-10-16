"""Simulator (offline scripted mode) against a real Cat Nanny server over HTTP.

The conversation flows are the ones CatNannyFramework's tests describe: follow-ups
update the same observation, "not sure" is not asked again, a correction moves the
observation (asking first when the cat is new), and the vet summary renders on screen.
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

TOKEN = "sim-test-token-123456"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from cat_nanny.app import create_app
    from cat_nanny.config import Settings
    from cat_nanny.demo_data import seed_store
    from cat_nanny.photos import LocalPhotoStorage
    from cat_nanny.store import MemoryStore

    store = MemoryStore()
    seed_store(store, "demo")
    port = _free_port()
    settings = Settings(auth_token=TOKEN, public_base_url=f"http://127.0.0.1:{port}")
    server = uvicorn.Server(uvicorn.Config(create_app(settings, store=store, photos=LocalPhotoStorage(tmp_path_factory.mktemp("photos"))),
                                          host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    os.environ.update(MCP_URL=f"http://127.0.0.1:{port}/mcp", MCP_AUTH_TOKEN=TOKEN, SIM_LLM="scripted")
    from fastapi.testclient import TestClient

    from sim.app import app

    yield TestClient(app)
    server.should_exit = True


def say(client, text, history, speaker="Alex", selected_cat=None):
    r = client.post("/api/turn", json={"text": text, "speaker": speaker, "history": history, "selected_cat": selected_cat})
    assert r.status_code == 200, r.text
    return r.json()


def test_page_and_panels(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/chat.js").status_code == 200
    assert client.get("/api/config").json()["llm"] == "scripted"
    household = client.get("/api/household").json()
    assert [c["name"] for c in household["cats"]] == ["Jeremy", "Mochi"]
    log = client.get("/api/care-log", params={"cat": "Mochi", "days": 30}).json()
    assert log["total"] == 2
    assert client.get("/api/care-log", params={"cat": "Nobody"}).status_code == 400


def test_follow_up_updates_same_observation_and_unknown_is_skipped(client):
    t1 = say(client, "Mochi just threw up", [])
    assert t1["steps"][0]["tool"] == "log_observation"
    event_id = t1["steps"][0]["result"]["structuredContent"]["event_id"]
    assert t1["reply"].startswith("Logged for Mochi.") and t1["reply"].endswith("Was it food, a hairball, liquid, or foam?")

    t2 = say(client, "it was food", t1["history"])
    assert t2["steps"][0]["arguments"]["event_id"] == event_id
    assert t2["reply"] == "Did you see any blood?"

    t3 = say(client, "I'm not sure", t2["history"])
    assert t3["steps"][0]["arguments"]["unknown_fields"] == ["has_blood"]
    assert t3["reply"] == "How many vomiting episodes are you recording?"
    assert t3["steps"][0]["result"]["structuredContent"]["observation"]["unknown_fields"] == ["has_blood"]

    t4 = say(client, "twice", t3["history"])
    assert t4["reply"] == "Did you notice lower energy than usual?"
    t5 = say(client, "no", t4["history"])
    assert t5["reply"] == "That's everything I need for this one."
    obs = t5["steps"][0]["result"]["structuredContent"]["observation"]
    assert obs["status"] == "completed" and obs["details"]["episode_count"] == 2 and obs["details"]["has_blood"] is None

    rec = client.get("/api/observation", params={"cat": "Mochi", "event_id": event_id}).json()
    assert rec["event_id"] == event_id and rec["reporter"] == "Alex"


def test_red_flag_is_spoken_first(client):
    t = say(client, "Jeremy threw up liquid with blood", [], speaker="Sam")
    assert t["reply"].startswith("Please call your vet today.")
    assert t["steps"][0]["result"]["structuredContent"]["red_flags"][0]["rule_id"] == "vomit_blood"


def test_correction_to_new_cat_asks_then_moves(client):
    t1 = say(client, "Mochi just threw up foam", [])
    event_id = t1["steps"][0]["result"]["structuredContent"]["event_id"]
    t2 = say(client, "no actually it is for Tofu", t1["history"])
    assert t2["steps"][0]["tool"] == "correct_cat"
    assert t2["reply"].startswith("I do not have a profile for Tofu.")
    t3 = say(client, "yes", t2["history"])
    assert t3["steps"][0]["arguments"]["create_if_missing"] is True
    assert t3["reply"].startswith("Corrected. This same observation is now recorded for Tofu, not Mochi.")
    assert t3["steps"][0]["result"]["structuredContent"]["observation"]["event_id"] == event_id
    assert "Tofu" in [c["name"] for c in client.get("/api/household").json()["cats"]]


def test_photo_link_and_vet_summary_on_screen(client):
    t1 = say(client, "Mochi hasn't eaten since yesterday", [])
    assert t1["steps"][0]["arguments"]["details"] == {"status": "not_eating", "hours_since_food": 24}
    assert t1["reply"].startswith("Please call your vet today.")
    t2 = say(client, "send me the photo link", t1["history"])
    assert t2["steps"][0]["tool"] == "get_photo_upload_link"
    assert t2["steps"][0]["result"]["structuredContent"]["photo_upload_url"].endswith(
        "/photos/upload?event_id=" + t1["steps"][0]["result"]["structuredContent"]["event_id"])

    t3 = say(client, "Show the summary for the vet", [], selected_cat="Mochi")
    assert t3["ui"]["uri"].startswith("ui://") and "ui/initialize" in t3["ui"]["html"]
    assert t3["ui"]["tool_result"]["structuredContent"]["cat"] == "Mochi"

    r = client.get("/api/vet-summary", params={"cat": "Mochi", "days": 7}).json()
    assert r["summary"]["counts"]["total_events"] >= 3 and r["ui"]["uri"] == t3["ui"]["uri"]


def test_unknown_phrase_gets_help_without_tool_calls(client):
    t = say(client, "what's the weather", [])
    assert t["steps"] == [] and "demo phrases" in t["reply"]
