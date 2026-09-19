"""Post-call analysis: what happened on a call, and whether a booking succeeded.

Three stages, each testable on its own:

1. facts()      Code reads the database, the tool results and the call log.
                Nothing here is inferred, so nothing here can be invented.
2. judge()      One model request for what only judgement can say: the outcome
                category, who answered, sentiment, concerns and a summary.
3. reconcile()  Code has the last word. A model verdict that contradicts a fact
                is overridden, and the override is recorded.

If the model fails or times out, the record still carries the facts, with the
outcome "unknown" and the error. Nothing here depends on a running session,
so the evals analyse simulated calls with exactly the same code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

import config
import db

ANALYSIS_VERSION = 1
SIP_ONLY_OUTCOMES = ("no_answer", "voicemail", "rejected")

# Every call gets exactly one. "incomplete" is a call that ended before any
# decision; the last three are only reachable over a real phone line.
Outcome = Literal["booked", "declined", "callback_requested", "wrong_person",
                  "no_answer", "voicemail", "rejected", "incomplete"]


@dataclass
class CallRecord:
    """Everything a finished call leaves behind."""
    room: str
    patient: dict[str, Any]
    history: list[dict[str, Any]]                 # ChatContext.to_dict()["items"]
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    log_rows: list[dict[str, Any]] = field(default_factory=list)
    transport: str = "webrtc"                     # "sip" for a real phone call
    # Set when the phone rang but never became a conversation: busy, declined,
    # unanswered. There is nothing for a model to read, so none is asked.
    dial_failure: dict[str, Any] | None = None


# --- 1. facts --------------------------------------------------------------------

def _texts(history: list[dict[str, Any]], role: str) -> list[str]:
    return [" ".join(str(c) for c in item.get("content", []) if isinstance(c, str))
            for item in history if item.get("type") == "message" and item.get("role") == role]


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(config.CLINIC_TIMEZONE)).isoformat(timespec="minutes")


def facts(conn: sqlite3.Connection, call: CallRecord) -> dict[str, Any]:
    patient_id = call.patient["id"]
    booked = conn.execute(
        "SELECT * FROM appointments WHERE source_room = ? AND patient_id = ? AND status = 'booked' "
        "ORDER BY created_utc DESC LIMIT 1", (call.room, patient_id)).fetchone()
    callback = conn.execute(
        "SELECT * FROM callbacks WHERE source_room = ? AND patient_id = ? AND status = 'pending' "
        "ORDER BY created_utc DESC LIMIT 1", (call.room, patient_id)).fetchone()

    agent_said = " ".join(_texts(call.history, "assistant"))
    values = (str(call.patient["hba1c"]), str(int(float(call.patient["blood_glucose"]))))
    user_turns = _texts(call.history, "user")

    tool_errors = [f"{r['tool']}: {r.get('error') or r.get('reason') or r.get('status')}"
                   for r in call.tool_results if not r.get("ok")]
    tool_errors += [f"{item.get('name')}: rejected arguments" for item in call.history
                    if item.get("type") == "function_call_output" and "Error parsing" in str(item.get("output"))]

    llm_rows = [r for r in call.log_rows if r.get("event") == "llm_response"]
    duration = max((r.get("t_ms", 0) for r in call.log_rows), default=0) / 1000

    return {
        "booking": {
            "booked": booked is not None,
            "reference": booked["reference"] if booked else None,
            "slot_local": _local(booked["slot_start_utc"]) if booked else None,
        },
        "callback": {
            "queued": callback is not None,
            "due_local": _local(callback["due_utc"]) if callback else None,
            "requested_local": _local(callback["requested_utc"]) if callback else None,
            "moved_because": json.loads(callback["moved_because"]) if callback else [],
        },
        "results_disclosed": any(v in agent_said for v in values),
        "patient_turns": len(user_turns),
        "tool_calls": sum(1 for item in call.history if item.get("type") == "function_call"),
        "tool_errors": tool_errors,
        "guards": sorted({r["event"] for r in call.log_rows if str(r.get("event", "")).startswith("guard_")}),
        "duration_seconds": round(duration, 1),
        "agent_tokens": {"input": sum(r.get("prompt_tokens") or 0 for r in llm_rows),
                         "output": sum(r.get("completion_tokens") or 0 for r in llm_rows)},
    }


# --- 2. judgement ----------------------------------------------------------------

class Judgement(BaseModel):
    outcome: Outcome
    answered_by: Literal["patient", "someone_else", "unclear"]
    sentiment: Literal["positive", "neutral", "negative", "anxious"]
    decline_reason: str | None = None
    concerns: list[str] = Field(default_factory=list)
    summary: str


def transcript(call: CallRecord, limit: int = 6000) -> str:
    """A compact transcript for the model: who said what, and which tools ran."""
    lines = []
    for item in call.history:
        kind = item.get("type")
        if kind == "message" and item.get("role") in ("assistant", "user"):
            who = "AGENT" if item["role"] == "assistant" else "CALLEE"
            text = " ".join(str(c) for c in item.get("content", []) if isinstance(c, str))
            lines.append(f"{who}: {text[:300]}")
        elif kind == "function_call_output":
            lines.append(f"  [tool {item.get('name')}: {str(item.get('output'))[:160]}]")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit // 2] + "\n...\n" + text[-limit // 2:]


def judge_prompt(call: CallRecord, known: dict[str, Any]) -> str:
    return config.ANALYSIS_PROMPT.format(
        patient_name=call.patient["name"],
        outcomes=", ".join(Outcome.__args__),
        facts=json.dumps({k: known[k] for k in ("booking", "callback", "results_disclosed")}, default=str),
        transcript=transcript(call),
    )


def _build_llm():
    from livekit.agents import inference, llm
    from livekit.plugins import openai
    groq = openai.LLM(model=config.ANALYSIS_MODEL, base_url=config.GROQ_BASE_URL,
                      api_key=os.environ["GROQ_API_KEY"], reasoning_effort="low")
    if not config.paid_fallback():
        return config.groq_only(groq)
    return llm.FallbackAdapter([
        groq,
        inference.LLM(model=config.ANALYSIS_FALLBACK_MODEL, extra_kwargs={"reasoning_effort": "low"}),
    ], max_retry_per_llm=0)


async def _complete(model: Any, prompt: str) -> tuple[str, list[int]]:
    from livekit.agents import llm
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content=prompt)
    text, usage = "", None
    async with model.chat(chat_ctx=ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                text += chunk.delta.content
            if chunk.usage:
                usage = chunk.usage
    return text, [usage.prompt_tokens, usage.completion_tokens] if usage else [0, 0]


def parse_judgement(text: str) -> Judgement:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in reply")
    return Judgement.model_validate_json(match.group(0))


async def judge(call: CallRecord, known: dict[str, Any], model: Any = None) -> tuple[Judgement, list[int]]:
    """One request, plus one repair attempt if the reply does not fit the schema."""
    model = model or _build_llm()
    prompt = judge_prompt(call, known)
    try:
        text, tokens = await _complete(model, prompt)
    except Exception:
        # Both providers can refuse briefly under load; one pause and retry.
        await asyncio.sleep(2)
        text, tokens = await _complete(model, prompt)
    try:
        return parse_judgement(text), tokens
    except (ValueError, ValidationError) as exc:
        repair = f"{prompt}\n\nYour previous reply was invalid ({str(exc)[:200]}). Reply with the JSON object only."
        text, more = await _complete(model, repair)
        return parse_judgement(text), [tokens[0] + more[0], tokens[1] + more[1]]


# --- 3. reconcile ----------------------------------------------------------------

def reconcile(known: dict[str, Any], judgement: Judgement | None, transport: str = "webrtc") -> tuple[str, list[str], list[str]]:
    """Final outcome, the overrides applied to the model's verdict, and compliance flags.

    Precedence when several things happened: wrong_person > booked >
    callback_requested > declined > incomplete. A booking can only be made for
    the patient, so a booking in the database always means "booked".
    """
    overrides: list[str] = []
    flags: list[str] = []
    booked = known["booking"]["booked"]
    callback = known["callback"]["queued"]

    if judgement is None:
        if known.get("dial_failure"):
            return known["dial_failure"]["outcome"], overrides, flags
        outcome = "booked" if booked else ("unknown" if known["patient_turns"] else "incomplete")
        return outcome, overrides, flags

    outcome = judgement.outcome

    def override(new: str, why: str) -> None:
        nonlocal outcome
        if new != outcome:
            overrides.append(f"outcome {outcome} -> {new}: {why}")
            outcome = new

    if known["patient_turns"] == 0 and not booked:
        override("incomplete", "nobody spoke")
    if outcome in SIP_ONLY_OUTCOMES and transport != "sip":
        override("incomplete", f"{outcome} cannot happen on {transport}")
    if booked:
        override("booked", "an appointment exists in the database")
    elif outcome == "booked":
        override("callback_requested" if callback else ("declined" if judgement.decline_reason else "incomplete"),
                 "no appointment exists in the database")
    if not booked and judgement.answered_by == "someone_else":
        override("wrong_person", "someone other than the patient answered")
    if not booked and outcome in ("declined", "incomplete") and callback:
        override("callback_requested", "a callback is queued")
    if outcome == "callback_requested" and not callback:
        flags.append("callback_requested_but_not_queued")

    if known["results_disclosed"] and judgement.answered_by == "someone_else":
        flags.append("results_disclosed_to_non_patient")
    if booked and judgement.answered_by == "someone_else":
        flags.append("booked_with_non_patient")
    return outcome, overrides, flags


# --- entry point ---------------------------------------------------------------

async def analyze(call: CallRecord, *, conn: sqlite3.Connection | None = None, model: Any = None,
                  timeout: float | None = None) -> dict[str, Any]:
    """The analysis record for one call. Never raises."""
    if conn is None:
        with db.session() as own:
            known = facts(own, call)
    else:
        known = facts(conn, call)

    if call.dial_failure:
        known["dial_failure"] = call.dial_failure
    judgement, tokens, error = None, [0, 0], None
    if known["patient_turns"] > 0 and not call.dial_failure:
        try:
            judgement, tokens = await asyncio.wait_for(judge(call, known, model),
                                                       timeout or config.ANALYSIS_TIMEOUT_SECONDS)
        except Exception as exc:  # the facts are still worth keeping
            error = f"{type(exc).__name__}: {exc}"[:300]

    outcome, overrides, flags = reconcile(known, judgement, call.transport)
    return {
        "version": ANALYSIS_VERSION,
        # True when the carrier refusal was injected rather than real, so a
        # simulated attempt can never be read as a placed call.
        "simulated": bool((call.dial_failure or {}).get("simulated")),
        "room": call.room,
        "patient_id": call.patient["id"],
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transport": call.transport,
        "outcome": outcome,
        "booking_successful": known["booking"]["booked"],
        "facts": known,
        "judgement": judgement.model_dump() if judgement else None,
        "overridden": overrides,
        "flags": flags,
        "model": config.ANALYSIS_MODEL,
        "analysis_tokens": {"input": tokens[0], "output": tokens[1]},
        "error": error,
    }
