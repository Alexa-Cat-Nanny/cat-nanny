"""Simulator (offline scripted mode) against a real Cat Nanny server over HTTP."""

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
def client():
    from cat_nanny.app import create_app
    from cat_nanny.config import Settings
    from cat_nanny.demo_data import seed_store
    from cat_nanny.notify import LogNotifier
    from cat_nanny.store import MemoryStore

    store = MemoryStore()
    seed_store(store, "demo")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(Settings(auth_token=TOKEN), store=store, notifier=LogNotifier()),
                                          host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    os.environ.update(MCP_URL=f"http://127.0.0.1:{port}/mcp", MCP_AUTH_TOKEN=TOKEN, SIM_LLM="scripted")
    from fastapi.testclient import TestClient

    from sim.app import app

    yield TestClient(app)
    server.should_exit = True


def say(client, text, history, speaker="Alex"):
    r = client.post("/api/turn", json={"text": text, "speaker": speaker, "history": history})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_voice_flow(client):
    assert client.get("/").status_code == 200
    t1 = say(client, "Mochi just threw up", [])
    assert t1["protocol_version"] == "2025-11-25"
    assert t1["steps"][0]["tool"] == "log_health_event"
    assert "Was it food" in t1["reply"]

    t2 = say(client, "food, and there was blood", t1["history"])
    assert t2["steps"][0]["arguments"]["event_id"] == t1["steps"][0]["result"]["structuredContent"]["event_id"]
    assert t2["reply"].startswith("Please call your vet today.")

    t3 = say(client, "Did anyone give Mochi her medicine today?", [], speaker="Sam")
    assert "Alex reported" in t3["reply"]  # the family alert reaches the next person

    t4 = say(client, "Show Mochi's summary for the vet", [])
    assert t4["ui"]["uri"].startswith("ui://") and "ui/initialize" in t4["ui"]["html"]
    assert t4["ui"]["tool_result"]["structuredContent"]["summary"]["cat_name"] == "Mochi"
