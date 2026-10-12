"""The simulator's MCP client: the part that plays Alexa+'s role towards the server.

Connects the same way Alexa+ does: Streamable HTTP, the initialize handshake
(protocol 2025-11-25), bearer token, and advertises MCP Apps support so the
server's UI resources can be rendered.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import Client
from mcp.client import advertise
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation

APPS_EXTENSION = "io.modelcontextprotocol/ui"
APP_MIME = "text/html;profile=mcp-app"


class McpBridge:
    """One connection per conversation turn; the server is stateless, so this is cheap."""

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token
        self._stack: AsyncExitStack | None = None
        self.client: Client | None = None
        self.tools: list[Any] = []
        self._ui_cache: dict[str, str] = {}

    async def __aenter__(self) -> McpBridge:
        self._stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        http = await self._stack.enter_async_context(httpx2.AsyncClient(headers=headers, timeout=30))
        self.client = await self._stack.enter_async_context(
            Client(
                streamable_http_client(self.url, http_client=http),
                mode="legacy",
                client_info=Implementation(name="Cat Nanny Alexa+ simulator", version="0.1.0"),
                extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME]})],
            )
        )
        self.tools = (await self.client.list_tools()).tools
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack:
            await self._stack.aclose()

    @property
    def protocol_version(self) -> str:
        return self.client.protocol_version if self.client else ""

    def tool_specs(self) -> list[dict[str, Any]]:
        """Tools in the shape an LLM tool-use API expects (name, description, JSON schema)."""
        return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in self.tools]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self.client
        r = await self.client.call_tool(name, arguments)
        return r.model_dump(mode="json", by_alias=True, exclude_none=True)

    def ui_uri(self, tool_name: str, result: dict[str, Any]) -> str | None:
        """Where the UI for this result lives: the result's _meta (Alexa+ docs) or the tool's _meta (MCP Apps spec)."""
        uri = ((result.get("_meta") or {}).get("ui") or {}).get("resourceUri")
        if uri:
            return uri
        tool = next((t for t in self.tools if t.name == tool_name), None)
        return ((getattr(tool, "meta", None) or {}).get("ui") or {}).get("resourceUri") if tool else None

    async def read_ui(self, uri: str) -> str:
        if uri not in self._ui_cache:
            assert self.client
            res = await self.client.read_resource(uri)
            self._ui_cache[uri] = res.contents[0].text  # type: ignore[union-attr]
        return self._ui_cache[uri]
