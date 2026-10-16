"""The simulator's "Alexa+ brain": decides which MCP tool to call for what was said, then phrases the reply.

Two modes:
- BedrockAgent: a real LLM on Amazon Bedrock (Converse API with tool use). This is the demo mode.
- ScriptedAgent: keyword rules, no AWS needed. Only for checking the plumbing offline; the phrases it
  understands are the ones from CatNannyFramework's conversation tests (Yifeng Peng).
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
- When someone mentions a cat vomiting or not eating, call log_observation instead of answering from general knowledge. Pass only the details they actually said; never invent values.
- Every tool result has assistant_guidance. Follow it exactly: say a red-flag sentence first, and ask the follow-up question word for word.
- When the person answers a follow-up question, call log_observation again with the event_id from the previous result and only the new detail. If they say they are not sure, put that field in unknown_fields.
- If they say the observation was about a different cat, call correct_cat. Only pass create_if_missing=true after they confirmed creating a profile.
- Fill reporter with the speaker's name. Convert relative times ("ten minutes ago") to ISO 8601 using the current time below.
- Use the selected cat when the person does not name one.
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


def _context_line(speaker: str, tz: str, selected_cat: str | None) -> str:
    now = datetime.now(ZoneInfo(tz))
    cat = f" The cat selected on screen is {selected_cat}." if selected_cat else ""
    return f"Current time: {now.isoformat(timespec='minutes')}. The person speaking is {speaker}.{cat}"


class BedrockAgent:
    def __init__(self, model_id: str, region: str | None = None, max_rounds: int = 6) -> None:
        import boto3

        self.model_id = model_id
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.max_rounds = max_rounds

    async def turn(self, bridge: McpBridge, text: str, history: list[dict[str, Any]], speaker: str, tz: str,
                   selected_cat: str | None = None) -> TurnResult:
        messages = [*history, {"role": "user", "content": [{"text": text}]}]
        tool_config = {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
                                               "inputSchema": {"json": t["input_schema"]}}} for t in bridge.tool_specs()]}
        system = [{"text": SYSTEM_PROMPT + "\n" + _context_line(speaker, tz, selected_cat)}]
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
    HELP = ("Offline mode only knows a few demo phrases, like 'Mochi just threw up', 'Mochi hasn't eaten since "
            "yesterday', 'show the summary for the vet' or 'send me the photo link'.")

    async def turn(self, bridge: McpBridge, text: str, history: list[dict[str, Any]], speaker: str, tz: str,
                   selected_cat: str | None = None) -> TurnResult:
        t = text.lower().strip()
        state = next((h for h in reversed(history) if h.get("role") == "scripted_state"), {})
        cat = self._cat_name(text) or state.get("cat") or selected_cat or "Mochi"
        pending = state.get("pending_event")
        calls: list[tuple[str, dict[str, Any]]] = []

        if m := re.search(r"(?:actually|no,?|i meant)\s.*?\b(?:for|was|about|is)\s+([A-Z][a-z]+)", text):
            if pending:
                calls.append(("correct_cat", {"event_id": pending, "to_cat": m.group(1), "reporter": speaker, "reason": text}))
        elif state.get("confirm_create") and re.fullmatch(r"(yes|yeah|yep|sure|please|ok|okay)[.! ]*", t):
            calls.append(("correct_cat", {"event_id": pending, "to_cat": state["confirm_create"], "create_if_missing": True,
                                          "reporter": speaker, "reason": "confirmed"}))
        elif pending and state.get("pending_field") and (answer := self._answer(state, t)) is not None:
            args: dict[str, Any] = {"cat": cat, "event_type": state["event_type"], "event_id": pending}
            if answer == "unknown":
                args["unknown_fields"] = [state["pending_field"]]
            else:
                args["details"] = {state["pending_field"]: answer}
            calls.append(("log_observation", args))
        elif re.search(r"threw up|throw(ing|s)? up|vomit|puk|hairball", t):
            details: dict[str, Any] = {}
            if word := next((w for w in self.CONTENT_WORDS if w in t), None):
                details["content"] = word
            if "blood" in t:
                details["has_blood"] = not re.search(r"\bno blood\b|without blood", t)
            if m := re.search(r"\b(\d+|twice|three) times?\b|\btwice\b", t):
                details["episode_count"] = {"twice": 2, "three": 3}.get(m.group(1) or "twice", None) or int(m.group(1))
            calls.append(("log_observation", {"cat": cat, "event_type": "vomit", "details": details, "reporter": speaker}))
        elif re.search(r"(hasn't|has not|didn't|did not|won't|isn't|is not|not) (been )?eat|off (his|her) food|eating less", t):
            details = {"status": "eating_less"} if "less" in t else {"status": "not_eating"}
            if m := re.search(r"(\d+) hours", t):
                details["hours_since_food"] = int(m.group(1))
            elif "yesterday" in t:
                details["hours_since_food"] = 24
            calls.append(("log_observation", {"cat": cat, "event_type": "appetite", "details": details, "reporter": speaker}))
        elif re.search(r"photo|picture", t) and pending:
            calls.append(("get_photo_upload_link", {"event_id": pending}))
        elif re.search(r"summary|for the vet|report", t):
            calls.append(("get_vet_summary", {"cat": cat, "days": 7}))
        elif re.search(r"how (has|is|was)|what happened|this week|care log|anything logged|lately", t):
            calls.append(("get_care_log", {"cat": cat, "days": 7}))
        elif re.search(r"which cats|who do you know|list (the )?cats", t):
            calls.append(("list_household", {}))
        elif m := re.search(r"(?:add|new) (?:a )?cat (?:named |called )?([A-Z][a-z]+)", text):
            calls.append(("add_cat", {"name": m.group(1)}))

        if not calls:
            return TurnResult(reply=self.HELP, history=history)

        steps = []
        for name, args in calls:
            steps.append(Step(name, args, await bridge.call(name, args)))
        step = steps[-1]
        sc = step.result.get("structuredContent") or {}
        new_state: dict[str, Any] = {"role": "scripted_state", "cat": cat}
        if not step.is_error and step.tool in ("log_observation", "correct_cat") and sc.get("observation"):
            new_state["cat"] = sc["observation"]["cat_name"]
            if sc.get("follow_up"):
                new_state.update(pending_event=sc["observation"]["event_id"], pending_field=sc["follow_up"]["field"],
                                 event_type=sc["observation"]["event_type"])
            else:
                new_state.update(pending_event=sc["observation"]["event_id"], event_type=sc["observation"]["event_type"])
        elif step.tool == "correct_cat" and sc.get("needs_confirmation"):
            new_state.update(pending_event=step.arguments["event_id"], confirm_create=sc["to_cat"],
                             pending_field=state.get("pending_field"), event_type=state.get("event_type"))
        elif pending:
            new_state.update(pending_event=pending, pending_field=state.get("pending_field"), event_type=state.get("event_type"))
        return TurnResult(reply=self._phrase(step), history=[*history, new_state], steps=steps)

    def _answer(self, state: dict[str, Any], t: str) -> Any:
        """Turn a spoken answer into the pending field's value, or 'unknown', or None when it isn't an answer."""
        if re.search(r"not sure|don't know|do not know|no idea|unsure", t):
            return "unknown"
        f = state["pending_field"]
        if f == "content":
            return next((w for w in self.CONTENT_WORDS if w in t), None)
        if f in ("has_blood", "low_energy"):
            if re.search(r"\b(yes|yeah|yep|some|a bit|a little)\b", t):
                return True
            if re.search(r"\b(no|nope|none|not)\b", t):
                return False
            return None
        if f == "status":
            return "eating_less" if "less" in t else "not_eating" if re.search(r"not|nothing|at all", t) else None
        if f in ("episode_count", "hours_since_food"):
            words = {"once": 1, "one": 1, "twice": 2, "two": 2, "three": 3, "four": 4, "five": 5}
            if m := re.search(r"\b(\d+)\b", t):
                return int(m.group(1))
            return next((v for w, v in words.items() if re.search(rf"\b{w}\b", t)), None)
        return None

    @staticmethod
    def _cat_name(text: str) -> str | None:
        m = re.search(r"\b([A-Z][a-z]+)\b(?:'s)? (?:just|has|hasn't|threw|is|isn't|didn't|won't|vomited|puked)", text)
        return m.group(1) if m and m.group(1) not in ("I", "She", "He", "We", "It", "Alexa") else None

    @staticmethod
    def _phrase(step: Step) -> str:
        """Speak the assistant_guidance the way the model would: the quoted sentences, in order."""
        if step.is_error:
            return " ".join(c.get("text", "") for c in step.result.get("content", []))
        sc = step.result.get("structuredContent") or {}
        guidance = sc.get("assistant_guidance", "")
        if step.tool in ("log_observation", "correct_cat"):
            if sc.get("needs_confirmation"):
                return " ".join(re.findall(r'"([^"]+)"', guidance))
            obs = sc["observation"]
            parts = []
            if step.tool == "correct_cat":
                parts.append(f"Corrected. This same observation is now recorded for {sc['to_cat']}, not {sc['from_cat']}. Its details, time and photos were kept.")
            if sc.get("red_flags"):
                parts.append(sc["red_flags"][0]["say_first"])
            elif step.tool == "log_observation" and not step.arguments.get("event_id"):
                parts.append(f"Logged for {obs['cat_name']}." + (f" That's the {sc['pattern_note']}." if sc.get("pattern_note") else ""))
            parts.append(sc["follow_up"]["question"] if sc.get("follow_up") else "That's everything I need for this one.")
            return " ".join(parts)
        if step.tool == "get_vet_summary":
            return f"Here's {sc.get('cat')}'s summary for the vet on screen: {sc['counts']['total_events']} observations in the last {sc['days']} days."
        if step.tool == "get_care_log":
            obs = sc.get("observations", [])
            if not obs:
                return f"Nothing has been logged for {sc.get('cat')} in the last {sc.get('days')} days."
            last = obs[0]
            return f"{len(obs)} observation(s) for {sc.get('cat')} in the last {sc.get('days')} days. The latest was {last['event_type']} logged by {last.get('reporter') or 'someone'}."
        if step.tool == "list_household":
            return "I know " + ", ".join(c["name"] for c in sc.get("cats", [])) + "."
        if step.tool == "add_cat":
            return f"Okay, I've added {sc.get('name')}."
        if step.tool == "get_photo_upload_link":
            return "The photo link is on screen. Nothing was texted or emailed."
        return guidance or "Logged."


def summarize_steps(steps: list[Step]) -> list[dict[str, Any]]:
    return [{"tool": s.tool, "arguments": s.arguments, "result": s.result, "is_error": s.is_error} for s in steps]


def history_is_json_safe(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bedrock messages can contain bytes in rare cases; keep only JSON-serialisable history for the browser."""
    return json.loads(json.dumps(history, default=str))
