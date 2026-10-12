"""Web Alexa+ simulator: a browser page (voice in, voice out, Echo Show screen) backed by an MCP client + LLM.

Hackathon participants cannot connect to real Alexa+, so this plays Alexa+'s
role: it connects to the Cat Nanny MCP server exactly as Alexa+ would and
renders MCP Apps views in a sandboxed iframe like an Echo Show.

Run:  uvicorn sim.app:app --port 8080   (then open http://localhost:8080)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import BedrockAgent, ScriptedAgent, history_is_json_safe, summarize_steps
from .mcp_bridge import McpBridge

log = logging.getLogger("simulator")

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp").strip()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "").strip()
LLM = os.environ.get("SIM_LLM", "").strip() or ("bedrock" if MODEL_ID else "scripted")
TZ = os.environ.get("HOUSEHOLD_TZ", "America/Los_Angeles")
STATIC = Path(__file__).parent / "static"

agent = BedrockAgent(MODEL_ID, os.environ.get("AWS_REGION")) if LLM == "bedrock" else ScriptedAgent()
app = FastAPI(title="Cat Nanny Alexa+ simulator")


class TurnIn(BaseModel):
    text: str
    speaker: str = "Alex"
    history: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {"llm": LLM, "model": MODEL_ID or None, "mcp_url": MCP_URL}


@app.post("/api/turn")
async def turn(body: TurnIn) -> dict[str, Any]:
    async with McpBridge(MCP_URL, MCP_AUTH_TOKEN) as bridge:
        result = await agent.turn(bridge, body.text, body.history, body.speaker, TZ)
        ui = None
        for step in reversed(result.steps):
            uri = bridge.ui_uri(step.tool, step.result)
            if uri and not step.is_error:
                ui = {"uri": uri, "html": await bridge.read_ui(uri), "tool_result": step.result}
                break
        return {
            "reply": result.reply,
            "history": history_is_json_safe(result.history),
            "steps": summarize_steps(result.steps),
            "ui": ui,
            "protocol_version": bridge.protocol_version,
        }
