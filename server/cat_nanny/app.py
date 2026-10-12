"""The ASGI app: MCP over Streamable HTTP at /mcp, plus /healthz.

Mode: stateless + plain JSON responses. Every POST is handled on its own (no
MCP session id, no SSE stream), which is what AWS Lambda needs and matches how
Alexa+ talks to servers (state lives in DynamoDB, not in the connection).

Run locally:  uvicorn cat_nanny.app:app --port 8000
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings, get_settings
from .notify import Notifier, make_notifier
from .store import MemoryStore, Store, make_store
from .tools import build_server

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("cat_nanny")


class GuardMiddleware:
    """Origin check and optional bearer token for /mcp.

    - MCP spec: if an Origin header is present and not allowed, answer 403.
    - Alexa+ docs: unauthenticated requests get 401 *without* a WWW-Authenticate header.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            origin = headers.get("origin")
            if origin and origin not in self.settings.allowed_origins:
                await _error(403, "Origin not allowed")(scope, receive, send)
                return
            token = self.settings.auth_token
            if token and not hmac.compare_digest(headers.get("authorization", ""), f"Bearer {token}"):
                await _error(401, "Unauthorized")(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _error(status: int, message: str) -> Response:
    body = {"jsonrpc": "2.0", "error": {"code": -32001, "message": message}}
    return Response(json.dumps(body), status_code=status, media_type="application/json")


def create_mcp(settings: Settings | None = None, store: Store | None = None, notifier: Notifier | None = None) -> tuple[MCPServer[Any], Settings]:
    """Build the MCP server once (tools, schemas, store and notifier clients)."""
    settings = settings or get_settings()
    store = store or make_store(settings.store, settings.table_name)
    notifier = notifier or make_notifier(settings.notifier, settings.sns_topic_arn)

    if settings.seed_on_start and isinstance(store, MemoryStore):
        from .demo_data import seed_store

        seed_store(store, settings.household_id)

    mcp = build_server(store, notifier, settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True, "store": settings.store, "notifier": settings.notifier})

    log.info("cat-nanny ready: store=%s notifier=%s household=%s auth=%s",
             settings.store, settings.notifier, settings.household_id, bool(settings.auth_token))
    return mcp, settings


def asgi_app(mcp: MCPServer[Any], settings: Settings) -> ASGIApp:
    """A fresh HTTP app around the server. Cheap; Lambda builds one per invocation (see handler.py)."""
    inner = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Host/Origin checks are done by GuardMiddleware so the app works behind API Gateway's host name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return GuardMiddleware(inner, settings)


def create_app(settings: Settings | None = None, store: Store | None = None, notifier: Notifier | None = None) -> ASGIApp:
    return asgi_app(*create_mcp(settings, store, notifier))


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    app: ASGIApp | None = None  # Lambda uses handler.py
else:
    app = create_app()
