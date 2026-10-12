"""The server over real HTTP (Streamable HTTP, protocol 2025-11-25) and through the Lambda handler."""

import base64
import json
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from cat_nanny.app import create_app
from cat_nanny.config import Settings
from cat_nanny.demo_data import seed_store
from cat_nanny.notify import LogNotifier
from cat_nanny.store import MemoryStore

TOKEN = "test-token-123456"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    store = MemoryStore()
    seed_store(store, "demo")
    app = create_app(Settings(auth_token=TOKEN, allowed_origins=["http://localhost:8080"]), store=store, notifier=LogNotifier())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


async def test_handshake_negotiates_2025_11_25_like_alexa(server_url):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    async with Client(streamable_http_client(f"{server_url}/mcp", http_client=http), mode="legacy") as c:
        assert c.protocol_version == "2025-11-25"
        tools = await c.list_tools()
        assert len(tools.tools) == 8
        r = await c.call_tool("get_today", {"cat": "Mochi"})
        assert not r.is_error and r.structured_content["cat"] == "Mochi"


def _post(url, body, headers=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": "2025-11-25", "Authorization": f"Bearer {TOKEN}", **(headers or {})}
    return httpx2.post(f"{url}/mcp", content=json.dumps(body), headers=h)


def test_alexa_style_initialize_payload_returns_plain_json(server_url):
    # The exact initialize request shown in the Alexa+ MCP client lifecycle docs.
    body = {"jsonrpc": "2.0", "method": "initialize", "id": "4e3bdaee-0",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {"roots": {"listChanged": True}},
                       "clientInfo": {"name": "Alexa+ MCP Client", "version": "1.0.0"}}}
    r = _post(server_url, body, {"MCP-Protocol-Version": "2025-03-26"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-03-26" and result["serverInfo"]["name"] == "cat-nanny"
    assert "mcp-session-id" not in {k.lower() for k in r.headers}


def test_auth_and_origin_guards(server_url):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = _post(server_url, ping, {"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and "www-authenticate" not in {k.lower() for k in r.headers}
    assert _post(server_url, ping, {"Origin": "https://evil.example"}).status_code == 403
    assert httpx2.get(f"{server_url}/healthz").json()["ok"] is True


def _apigw_event(body: dict) -> dict:
    raw = json.dumps(body).encode()
    return {
        "version": "2.0", "routeKey": "$default", "rawPath": "/mcp", "rawQueryString": "",
        "headers": {"content-type": "application/json", "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-11-25", "authorization": f"Bearer {TOKEN}", "host": "abc.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {"http": {"method": "POST", "path": "/mcp", "sourceIp": "1.2.3.4", "protocol": "HTTP/1.1", "userAgent": "test"},
                           "domainName": "abc.execute-api.us-east-1.amazonaws.com", "stage": "$default", "requestId": "r1"},
        "body": base64.b64encode(raw).decode(), "isBase64Encoded": True,
    }


def test_lambda_handler_end_to_end(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("SEED_ON_START", "true")
    import importlib

    import cat_nanny.app as app_mod
    import cat_nanny.config as config_mod
    import cat_nanny.handler as handler_mod

    config_mod.get_settings.cache_clear()
    importlib.reload(app_mod)
    importlib.reload(handler_mod)

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    resp = handler_mod.handler(_apigw_event(init), None)
    assert resp["statusCode"] == 200, resp
    body = resp["body"] if not resp.get("isBase64Encoded") else base64.b64decode(resp["body"]).decode()
    assert json.loads(body)["result"]["protocolVersion"] == "2025-11-25"

    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_today", "arguments": {"cat": "Mochi"}}}
    resp = handler_mod.handler(_apigw_event(call), None)
    body = resp["body"] if not resp.get("isBase64Encoded") else base64.b64decode(resp["body"]).decode()
    assert json.loads(body)["result"]["structuredContent"]["cat"] == "Mochi"
