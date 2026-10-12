"""The simulator's "Alexa+ brain": decides which MCP tool to call for what was said, then phrases the reply.

Two modes:
- BedrockAgent: a real LLM on Amazon Bedrock (Converse API with tool use). This is the demo mode.
- ScriptedAgent: keyword rules, no AWS needed. Only for checking the plumbing offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anyio

from .mcp_bridge import McpBridge

SYSTEM_PROMPT = """You are Alexa, a voice assistant in a family's home, using the Cat Nanny add-on.
Rules:
- When someone reports anything about the cat's health or care, call the matching Cat Nanny tool instead of answering from general knowledge.
- Every tool result has assistant_guidance. Follow it exactly: say a red-flag sentence first, and ask the follow-up question word for word.
- When the person answers a follow-up question, call log_health_event again with the event_id from the previous result.
- Fill reporter with the speaker's name. Convert relative times ("ten minutes ago") to ISO 8601 using the current time below.
- Never diagnose or name a disease. Never read ids, JSON or field names aloud.
- Answer like a smart speaker: one or two short spoken sentences, no lists, no markdown."""


@dataclass
class Step:
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]

    @property
    def is_error(self) -> bool:
        return bool(self.result.get("isError"))


@dataclass
class TurnResult:
    reply: str
    history: list[dict[str, Any]]
    steps: list[Step] = field(default_factory=list)


def _context_line(speaker: str, tz: str) -> str:
    now = datetime.now(ZoneInfo(tz))
    return f"Current time: {now.isoformat(timespec='minutes')}. The person speaking is {speaker}."


class BedrockAgent:
    def __init__(self, model_id: str, region: str | None = None, max_rounds: int = 6) -> None:
        import boto3

        self.model_id = model_id
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.max_rounds = max_rounds

    async def turn(self, bridge: McpBridge, text: str, history: list[dict[str, Any]], speaker: str, tz: str) -> TurnResult:
        messages = [*history, {"role": "user", "content": [{"text": text}]}]
        tool_config = {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
                                               "inputSchema": {"json": t["input_schema"]}}} for t in bridge.tool_specs()]}
        system = [{"text": SYSTEM_PROMPT + "\n" + _context_line(speaker, tz)}]
        steps: list[Step] = []

        for _ in range(self.max_rounds):
            resp = await anyio.to_thread.run_sync(lambda: self.client.converse(
                modelId=self.model_id, system=system, messages=messages, toolConfig=tool_config,
                inferenceConfig={"maxTokens": 400, "temperature": 0.2}))
            message = resp["output"]["message"]
            messages.append(message)
            if resp.get("stopReason") != "tool_use":
                reply = " ".join(b["text"] for b in message["content"] if "text" in b).strip()
                return TurnResult(reply=reply, history=messages, steps=steps)

            results = []
            for block in message["content"]:
                if "toolUse" not in block:
                    continue
                use = block["toolUse"]
                result = await bridge.call(use["name"], use.get("input") or {})
                steps.append(Step(use["name"], use.get("input") or {}, result))
                payload = result.get("structuredContent")
                content = [{"json": payload}] if isinstance(payload, dict) else [
                    {"text": " ".join(c.get("text", "") for c in result.get("content", []))}]
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": content,
                                               "status": "error" if result.get("isError") else "success"}})
            messages.append({"role": "user", "content": results})

        return TurnResult(reply="Sorry, I couldn't finish that.", history=messages, steps=steps)


class ScriptedAgent:
    """Offline stand-in for the LLM. Understands a handful of demo phrases; good enough to test the wiring."""

    CONTENT_WORDS = ("food", "hairball", "liquid", "foam")

    async def turn(self, bridge: McpBridge, text: str, history: list[dict[str, Any]], speaker: str, tz: str) -> TurnResult:
        t = text.lower()
        state = next((h for h in reversed(history) if h.get("role") == "scripted_state"), {})
        cat = self._cat_name(text) or state.get("cat") or "Mochi"
        call: tuple[str, dict[str, Any]] | None = None

        if state.get("pending_event") and (any(w in t for w in self.CONTENT_WORDS) or re.search(r"\b(yes|no)\b", t)):
            details: dict[str, Any] = {}
            if word := next((w for w in self.CONTENT_WORDS if w in t), None):
                details["content"] = word
            if "blood" in t or state.get("pending_field") == "blood":
                details["blood"] = not re.search(r"\bno\b", t)
            call = ("log_health_event", {"cat": cat, "type": "vomit", "event_id": state["pending_event"], "details": details})
        elif re.search(r"threw up|throw(ing)? up|vomit|puk", t):
            details = {}
            if word := next((w for w in self.CONTENT_WORDS if w in t), None):
                details["content"] = word
            if "blood" in t:
                details["blood"] = "no blood" not in t
            call = ("log_health_event", {"cat": cat, "type": "vomit", "details": details, "reporter": speaker})
        elif re.search(r"(hasn't|has not|didn't|did not|won't|not) eat", t):
            call = ("log_health_event", {"cat": cat, "type": "appetite", "details": {"level": "none"}, "reporter": speaker})
        elif re.search(r"\b(called the vet|we know|got it|handled)\b", t):
            call = ("acknowledge_alerts", {"cat": cat, "reporter": speaker, "note": text})
        elif re.search(r"summary|for the vet", t):
            call = ("get_vet_summary", {"cat": cat})
        elif re.search(r"\bwhy\b|pattern|lately", t):
            call = ("get_patterns", {"cat": cat})
        elif re.search(r"^(did|has|have)\b|today", t):
            call = ("get_today", {"cat": cat})
        elif m := re.search(r"weighs? ([\d.]+) ?(kg|kilo|lb|pound)", t):
            kg = float(m.group(1)) / (2.205 if m.group(2) in ("lb", "pound") else 1)
            call = ("log_care_event", {"cat": cat, "type": "weight", "details": {"kg": round(kg, 2)}, "reporter": speaker})
        elif re.search(r"\b(fed|feed)\b", t):
            call = ("log_care_event", {"cat": cat, "type": "meal", "reporter": speaker})
        elif re.search(r"pill|medicine|medication", t):
            call = ("log_care_event", {"cat": cat, "type": "medication", "reporter": speaker})

        if call is None:
            reply = "Offline mode only knows a few demo phrases, like 'Mochi just threw up' or 'did anyone feed Mochi today?'."
            return TurnResult(reply=reply, history=history)

        result = await bridge.call(*call)
        sc = result.get("structuredContent") or {}
        new_state: dict[str, Any] = {"role": "scripted_state", "cat": cat}
        if call[0] == "log_health_event" and sc.get("follow_up"):
            new_state.update(pending_event=sc["event_id"], pending_field=sc["follow_up"]["field"])
        reply = self._phrase(call[0], result)
        return TurnResult(reply=reply, history=[*history, new_state], steps=[Step(call[0], call[1], result)])

    @staticmethod
    def _cat_name(text: str) -> str | None:
        m = re.search(r"\b([A-Z][a-z]+)\b(?:'s)? (?:just|has|hasn't|threw|is|weighs|didn't)", text)
        return m.group(1) if m and m.group(1) not in ("I", "She", "He", "We", "Alexa") else None

    @staticmethod
    def _phrase(tool: str, result: dict[str, Any]) -> str:
        if result.get("isError"):
            return " ".join(c.get("text", "") for c in result.get("content", []))
        sc = result.get("structuredContent") or {}
        parts: list[str] = []
        if sc.get("red_flags"):
            parts.append(sc["red_flags"][0]["say_first"])
            if sc.get("family_notified"):
                parts.append("I've emailed the rest of the family.")
        if tool in ("log_health_event", "log_care_event"):
            parts.append("Logged." + (f" That's the {sc['pattern_note']}." if sc.get("pattern_note") else ""))
            if sc.get("follow_up"):
                parts.append(sc["follow_up"]["question"])
        elif tool == "get_today":
            meals = sc.get("meals", [])
            meds = sc.get("medications", [])
            parts.append(f"{sc.get('cat')} has had {len(meals)} meal{'s' if len(meals) != 1 else ''} logged today"
                         + (f", and {meds[-1].get('reporter') or 'someone'} gave her medicine at {meds[-1]['at'][11:16]}." if meds else ", and no medicine yet."))
        elif tool == "get_patterns":
            pats = sc.get("patterns", [])
            parts.append(pats[0]["summary"] if pats else "No clear pattern stands out yet.")
        elif tool == "get_vet_summary":
            parts.append("Here's the summary for your vet.")
        elif tool == "acknowledge_alerts":
            parts.append("Okay, I've cleared the family alert.")
        alerts = sc.get("pending_alerts") or []
        if alerts:
            parts.append(f"Also, heads up: {alerts[0]['message']}")
        return " ".join(parts)


def summarize_steps(steps: list[Step]) -> list[dict[str, Any]]:
    return [{"tool": s.tool, "arguments": s.arguments, "result": s.result, "is_error": s.is_error} for s in steps]


def history_is_json_safe(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bedrock messages can contain bytes in rare cases; keep only JSON-serialisable history for the browser."""
    return json.loads(json.dumps(history, default=str))
