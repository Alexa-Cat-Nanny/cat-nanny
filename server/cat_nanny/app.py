"""The ASGI app: MCP over Streamable HTTP at /mcp, plus the photo upload page and /healthz.

Mode: stateless + plain JSON responses. Every POST is handled on its own (no
MCP session id, no SSE stream), which is what AWS Lambda needs and matches how
Alexa+ talks to servers (state lives in DynamoDB, not in the connection).

Photo routes (plain HTTP, not MCP, because a phone browser uses them):
  GET  /photos/upload?event_id=...      the upload page
  GET  /photos/upload-target?event_id=  JSON: where the browser should PUT the bytes
  POST /photos/complete                 JSON {event_id, storage_key}: record the uploaded photo
  PUT  /photos/blob/<key>, GET /photos/blob/<key>   local storage only (S3 serves these itself)

Run locally:  uvicorn cat_nanny.app:app --port 8000
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings, get_settings, request_base_url
from .photos import ALLOWED, MAX_PHOTO_BYTES, LocalPhotoStorage, PhotoStorage, PhotoValidationError, make_photo_storage, photo_key, sniff_content_type
from .store import MemoryStore, Store, make_store
from .tools import build_server, register_photo

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("cat_nanny")


class GuardMiddleware:
    """Origin check and optional bearer token for /mcp; remembers the request's own URL for links.

    - MCP spec: if an Origin header is present and not allowed, answer 403.
    - Alexa+ docs: unauthenticated requests get 401 *without* a WWW-Authenticate header.
    - Links handed to phones (upload page, local photos) are built on the host the request came in on
      (API Gateway's domain on Lambda) unless PUBLIC_BASE_URL is set.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if host := headers.get("host"):
            scheme = headers.get("x-forwarded-proto", scope.get("scheme", "http"))
            request_base_url.set(f"{scheme}://{host}")
        if scope["path"].startswith("/mcp"):
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


def _photo_routes(mcp: MCPServer[Any], store: Store, photos: PhotoStorage, settings: Settings) -> None:
    household = settings.household_id
    page = files("cat_nanny.ui").joinpath("upload.html").read_text(encoding="utf-8")

    @mcp.custom_route("/photos/upload", methods=["GET"])
    async def upload_page(_: Request) -> Response:
        return HTMLResponse(page)

    @mcp.custom_route("/photos/event", methods=["GET"])
    async def event_info(request: Request) -> Response:
        """Enough about the observation for the upload page to show what the photo attaches to."""
        event_id = request.query_params.get("event_id", "")
        record = store.get_observation(household, event_id)
        if record is None:
            return JSONResponse({"detail": "Observation not found"}, status_code=404)
        cat = store.get_cat(household, record.event.cat_id)
        return JSONResponse({"event_id": event_id, "cat_name": cat.name if cat else record.event.cat_id,
                             "event_type": record.event.type, "occurred_at": record.event.ts.isoformat(),
                             "photos": len(store.list_photos(household, event_id))})

    @mcp.custom_route("/photos/upload-target", methods=["GET"])
    async def upload_target(request: Request) -> Response:
        event_id = request.query_params.get("event_id", "")
        content_type = request.query_params.get("content_type", "")
        if store.get_observation(household, event_id) is None:
            return JSONResponse({"detail": "Observation not found"}, status_code=404)
        if content_type not in ALLOWED:
            return JSONResponse({"detail": "Please choose a JPEG, PNG, or WebP image."}, status_code=422)
        key = photo_key(household, event_id, f"photo-{secrets.token_hex(6)}", content_type)
        target = photos.upload_target(key, content_type, settings.base_url())
        return JSONResponse({"url": target.url, "method": target.method, "headers": target.headers,
                             "storage_key": target.storage_key, "max_bytes": MAX_PHOTO_BYTES})

    @mcp.custom_route("/photos/complete", methods=["POST"])
    async def complete(request: Request) -> Response:
        body = await request.json()
        event_id, key = str(body.get("event_id", "")), str(body.get("storage_key", ""))
        expected_prefix = f"households/{household}/events/{event_id}/"
        if not key.startswith(expected_prefix) or not photos.exists(key):
            return JSONResponse({"detail": "Upload not found; please retry."}, status_code=404)
        content_type = next((ct for ct, ext in ALLOWED.items() if key.endswith(ext)), "image/jpeg")
        try:
            photo = register_photo(store, household, event_id, key, content_type, datetime.now(UTC))
        except KeyError:
            return JSONResponse({"detail": "Observation not found"}, status_code=404)
        return JSONResponse({"ok": True, "photo_id": photo.photo_id, "url": photos.view_url(key, settings.base_url())})

    if isinstance(photos, LocalPhotoStorage):

        @mcp.custom_route("/photos/blob/{key:path}", methods=["PUT", "GET"])
        async def blob(request: Request) -> Response:
            key = request.path_params["key"]
            try:
                if request.method == "PUT":
                    data = await request.body()
                    if not data or len(data) > MAX_PHOTO_BYTES:
                        return JSONResponse({"detail": "The image must not be empty or exceed 8 MB."}, status_code=422)
                    content_type = sniff_content_type(data[:16])
                    if not key.endswith(ALLOWED[content_type]):
                        return JSONResponse({"detail": "File type does not match the upload target."}, status_code=422)
                    photos.write(key, data)
                    return JSONResponse({"ok": True})
                if not photos.exists(key):
                    return Response(status_code=404)
                content_type = next((ct for ct, ext in ALLOWED.items() if key.endswith(ext)), "application/octet-stream")
                return Response(photos.read(key), media_type=content_type)
            except PhotoValidationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)


def create_mcp(settings: Settings | None = None, store: Store | None = None, photos: PhotoStorage | None = None
               ) -> tuple[MCPServer[Any], Settings]:
    """Build the MCP server once (tools, schemas, store and storage clients)."""
    settings = settings or get_settings()
    store = store or make_store(settings.store, settings.table_name)
    photos = photos or make_photo_storage(settings.photos, bucket=settings.photo_bucket, root=settings.photo_root)

    if settings.seed_on_start and isinstance(store, MemoryStore):
        from .demo_data import seed_store

        seed_store(store, settings.household_id)

    mcp = build_server(store, photos, settings)
    _photo_routes(mcp, store, photos, settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True, "store": settings.store, "photos": settings.photos})

    log.info("cat-nanny ready: store=%s photos=%s household=%s auth=%s",
             settings.store, settings.photos, settings.household_id, bool(settings.auth_token))
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


def create_app(settings: Settings | None = None, store: Store | None = None, photos: PhotoStorage | None = None) -> ASGIApp:
    return asgi_app(*create_mcp(settings, store, photos))


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    app: ASGIApp | None = None  # Lambda uses handler.py
else:
    app = create_app()
