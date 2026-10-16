"""Web Alexa+ simulator: a browser page (voice in, voice out, Echo Show screen) backed by an MCP client + LLM.

Hackathon participants cannot connect to real Alexa+, so this plays Alexa+'s
role: it connects to the Cat Nanny MCP server exactly as Alexa+ would and
renders MCP Apps views in a sandboxed iframe like an Echo Show.

The page itself is CatNannyFramework's simulator (Shu Wang) with its FastAPI
backend replaced by MCP tool calls. The conversation goes through the agent
(Bedrock); the side panels call the read-only tools directly.

Run:  uvicorn sim.app:app --port 8080   (then open http://127.0.0.1:8080)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from .agent import BedrockAgent, ScriptedAgent, history_is_json_safe, summarize_steps
from .mcp_bridge import McpBridge

log = logging.getLogger("simulator")

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp").strip()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "").strip()
LLM = os.environ.get("SIM_LLM", "").strip() or ("bedrock" if MODEL_ID else "scripted")
TZ = os.environ.get("HOUSEHOLD_TZ", "America/Los_Angeles")
STATIC = Path(__file__).parent / "static"

agent = BedrockAgent(MODEL_ID, os.environ.get("AWS_REGION")) if LLM == "bedrock" else ScriptedAgent()
app = FastAPI(title="Cat Nanny Alexa+ simulator")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    speaker: str = "Alex"
    selected_cat: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


class AddCatIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sex: str = "unknown"


def bridge() -> McpBridge:
    return McpBridge(MCP_URL, MCP_AUTH_TOKEN)


async def direct(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one tool for a side panel (no language model) and unwrap it like a REST response."""
    try:
        async with bridge() as b:
            result = await b.call(name, arguments)
    except (MCPError, OSError) as exc:
        raise HTTPException(502, f"The Cat Nanny MCP server is not reachable at {MCP_URL}: {exc}") from exc
    if result.get("isError"):
        raise HTTPException(400, " ".join(c.get("text", "") for c in result.get("content", [])))
    return result


async def ui_for(b: McpBridge, tool: str, result: dict[str, Any]) -> dict[str, Any] | None:
    uri = b.ui_uri(tool, result)
    return {"uri": uri, "html": await b.read_ui(uri), "tool_result": result} if uri else None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {"llm": LLM, "model": MODEL_ID or None, "mcp_url": MCP_URL}


@app.post("/api/turn")
async def turn(body: TurnIn) -> dict[str, Any]:
    try:
        async with bridge() as b:
            result = await agent.turn(b, body.text, body.history, body.speaker, TZ, body.selected_cat)
            ui = None
            for step in reversed(result.steps):
                if not step.is_error and (ui := await ui_for(b, step.tool, step.result)):
                    break
    except (MCPError, OSError) as exc:
        raise HTTPException(502, f"The Cat Nanny MCP server is not reachable at {MCP_URL}: {exc}") from exc
    except BotoCoreError as exc:  # Bedrock: no credentials, no region, model access, throttling, ...
        raise HTTPException(502, f"Bedrock ({MODEL_ID}): {exc}") from exc
    except ClientError as exc:
        raise HTTPException(502, f"Bedrock ({MODEL_ID}): {exc.response.get('Error', {}).get('Message', exc)}") from exc
    return {"reply": result.reply, "history": history_is_json_safe(result.history),
            "steps": summarize_steps(result.steps), "ui": ui}


@app.get("/api/household")
async def household() -> dict[str, Any]:
    return (await direct("list_household", {}))["structuredContent"]


@app.post("/api/add-cat")
async def add_cat(body: AddCatIn) -> dict[str, Any]:
    return (await direct("add_cat", {"name": body.name, "sex": body.sex}))["structuredContent"]


@app.get("/api/care-log")
async def care_log(cat: str, days: int = 90) -> dict[str, Any]:
    return (await direct("get_care_log", {"cat": cat, "days": days, "limit": 50}))["structuredContent"]


@app.get("/api/observation")
async def observation(cat: str, event_id: str) -> dict[str, Any] | None:
    log_ = (await direct("get_care_log", {"cat": cat, "days": 365, "limit": 50}))["structuredContent"]
    return next((o for o in log_["observations"] if o["event_id"] == event_id), None)


@app.get("/api/vet-summary")
async def vet_summary(cat: str, days: int = 7) -> dict[str, Any]:
    try:
        async with bridge() as b:
            result = await b.call("get_vet_summary", {"cat": cat, "days": days})
            if result.get("isError"):
                raise HTTPException(400, " ".join(c.get("text", "") for c in result.get("content", [])))
            return {"summary": result["structuredContent"], "ui": await ui_for(b, "get_vet_summary", result)}
    except (MCPError, OSError) as exc:
        raise HTTPException(502, f"The Cat Nanny MCP server is not reachable at {MCP_URL}: {exc}") from exc
