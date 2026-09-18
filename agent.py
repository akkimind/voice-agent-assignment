"""Outbound healthcare voice agent.

Calls a patient, tells them their recent biomarker results, and tries to book a
follow-up consultation through a tool call.

The agent is deliberately transport-agnostic. It behaves the same whether the
other participant arrived over WebRTC (development) or over SIP (a real phone
call). Only the dial path differs, and that lives outside this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterable, Literal

from dotenv import load_dotenv
from pydantic import BeforeValidator
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    RunContext,
    function_tool,
    inference,
    llm,
    stt,
)
from livekit.plugins import deepgram, openai, silero

import booking
from call_log import CallLog, NullLog
import callback_queue
import config
import db
import opik_integration
import post_call
import scheduling
import telephony
from scheduling import Day, PartOfDay, SchedulingError

load_dotenv(".env")

logger = logging.getLogger("adit-agent")

AGENT_NAME = "adit-outbound-agent"
TRANSCRIPT_DIR = Path(__file__).parent / "session_reports"

# How long to wait for the callee to speak first before the agent opens anyway.
# A real person answering a phone says "hello" within a second or two; a silent
# pickup still needs to hear something.
GREETING_FALLBACK_SECONDS = 2.5

def _prewarm(proc: JobProcess) -> None:
    """Load the voice-activity model before any job arrives.

    Silero's ONNX session takes ~400ms to build. Doing that inside the
    entrypoint blocks the event loop and delays audio and turn handling, which
    the framework warns about. Here it happens once per worker process, off the
    hot path.
    """
    proc.userdata["vad"] = silero.VAD.load()
    # Idempotent: creates tables if missing and upserts seed patients.
    with db.session() as conn:
        db.init_db(conn)


server = AgentServer()
server.setup_fnc = _prewarm


def _user_messages(ctx: RunContext) -> list[str]:
    """What the caller has said so far, one entry per committed turn."""
    return [item.text_content or "" for item in ctx.session.history.items
            if getattr(item, "type", "message") == "message" and item.role == "user"]


def normalize_day(value: Any) -> Any:
    """Map near-miss day values onto the Day vocabulary before validation.

    The schema lists the exact values, yet in rehearsals the model sent
    "Friday", "day after tomorrow" and "2026-09-16". Each was rejected and
    retried, costing a whole extra LLM request. Anything still unrecognised
    falls through and fails validation as before.
    """
    if not isinstance(value, str):
        return value
    if value.strip().lower() in ("", "null", "none"):
        return None
    text = re.sub(r"^(this|next|on)\s+", "", value.strip().lower())
    text = re.sub(r"[\s-]+", "_", text)
    if text not in ("today", "tomorrow", "day_after_tomorrow", "day_after_offered", *scheduling.WEEKDAYS):
        # "friday_18_september" or "appointment_on_friday": keep the day, drop the rest.
        for word in ("day_after_tomorrow", "day_after_offered", "tomorrow", "today", *scheduling.WEEKDAYS):
            if word in text:
                return word
    try:
        date = datetime.strptime(text[:10], "%Y_%m_%d").date()
    except ValueError:
        return text
    today = booking.clinic_now().date()
    if date.year != today.year:
        # The model has sent "2024-09-17" in 2026. Its month and day are reliable,
        # its year is not.
        try:
            date = date.replace(year=today.year)
        except ValueError:
            return value
        if date < today:
            date = date.replace(year=today.year + 1)
    ahead = (date - today).days
    if ahead == 0:
        return "today"
    if ahead == 1:
        return "tomorrow"
    if 1 < ahead < 7:
        return scheduling.WEEKDAYS[date.weekday()]
    return value


DayArg = Annotated[Day | None, BeforeValidator(normalize_day)]
# The search also accepts "day_after_offered": the model recognises "the next
# day" and code does the date maths. In rehearsal the model searched the offered
# day again and then tried to book today when it did that maths itself.
ApptDay = Literal[
    "today", "tomorrow", "day_after_tomorrow",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "day_after_offered",
]
ApptDayArg = Annotated[ApptDay | None, BeforeValidator(normalize_day)]


def normalize_part_of_day(value: Any) -> Any:
    """Anything that is not a part of the day becomes empty. The model once sent
    part_of_day "driving"; rejecting it cost a whole retry request."""
    if isinstance(value, str) and value.strip().lower() in ("morning", "afternoon", "evening"):
        return value.strip().lower()
    return None


PartOfDayArg = Annotated[PartOfDay | None, BeforeValidator(normalize_part_of_day)]


def normalize_time(value: Any) -> Any:
    """An empty time is no time. The model sends "" for fields it leaves empty,
    and each rejection cost a whole retried request."""
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
        return None
    return value


TimeArg = Annotated[str | None, BeforeValidator(normalize_time)]

def normalize_bool(value: Any) -> Any:
    """ "true" and "yes" as strings: one rejection cost a whole retried request."""
    if isinstance(value, str):
        return {"true": True, "yes": True, "false": False, "no": False}.get(value.strip().lower(), value)
    return value


BoolArg = Annotated[bool, BeforeValidator(normalize_bool)]

# Where in the clinic's day, for appointments. Deliberately not "evening": to
# the model an evening is after six, when the clinic is shut, so "later in the
# day" kept landing on the afternoon and then on noon.
ClinicWindow = Literal["early", "middle", "late"]
WINDOW_PART_OF_DAY = {"early": "morning", "middle": "afternoon", "late": "evening"}


def normalize_window(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip().lower()
        text = {v: k for k, v in WINDOW_PART_OF_DAY.items()}.get(text, text)
        return text if text in WINDOW_PART_OF_DAY else None
    return value


WindowArg = Annotated[ClinicWindow | None, BeforeValidator(normalize_window)]


# A reply that failed a check is asked for again with the reason, this many
# times in all. If none passes, the turn stays silent and the log says why:
# there is no fixed sentence to fall back on.
REPLY_ATTEMPTS = 3


class DotCollapser:
    """Replaces runs of three or more dots (or an ellipsis character) with a space.

    The model sometimes writes "for you?............When would", which the voice
    may render as a long silence.
    """

    def __init__(self) -> None:
        self._dots = 0

    def feed(self, piece: str) -> str:
        out = []
        for ch in piece:
            if ch == ".":
                self._dots += 1
            elif ch == "…":
                self._dots += 3
            else:
                out.append(self._flush() + ch)
        return "".join(out)

    def finish(self) -> str:
        dots, self._dots = self._dots, 0
        return "." * dots if dots < 3 else ""

    def _flush(self) -> str:
        dots, self._dots = self._dots, 0
        return "." * dots if dots < 3 else " "


class QuestionCutter:
    """Ends a reply at its first question, and drops any tool call after it.

    A question hands the turn to the other person. In rehearsal the model asked
    "Would you like to confirm that appointment?", carried on with "Yes, that
    works for me." as if the patient had answered, and booked on it; it also
    asked two questions at once. Where the reply stops is structure, not words.
    """

    def __init__(self) -> None:
        self.asked = False
        self.removed = ""

    @property
    def cut(self) -> bool:
        return bool(self.removed.strip())

    def feed(self, piece: str) -> str:
        if self.asked:
            self.removed += piece
            return ""
        i = piece.find("?")
        if i < 0:
            return piece
        self.asked = True
        self.removed += piece[i + 1:]
        return piece[:i + 1]


_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d\d))?\s*([ap])\.?\s?m\b\.?", re.I)
_CLOCK_24 = re.compile(r"\b(\d{1,2}):(\d\d)\b")
_SENTENCE_END = re.compile(r"[.?!](?=\s)")
_WEEKDAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)
# The clinic's own week, which the agent may always state.
_CLINIC_WEEK = re.compile(r"\bmonday\s+(to|through|-|and)\s+saturday\b|\b(closed|open)\b[^.?!]{0,20}\bsundays?\b"
                          r"|\bsundays?\b[^.?!]{0,20}\bclosed\b", re.I)
# The policy is to name no condition at all, not even to deny it. These are the
# conditions a glucose result invites; the eval judge looks for any other.
CONDITION = re.compile(r"\b(pre-?diabet\w*|diabet\w*|hyperglyc\w*|hypoglyc\w*|insulin resistan\w*|"
                       r"metabolic syndrome)", re.I)
# A whole sentence in brackets is a stage direction, "(end call)", not speech.
_STAGE = re.compile(r"[(\[*][^()\[\]*]*[)\]*][.!?]?")
# Seven or more digits in a row, however they are spaced: any phone number.
PHONE = re.compile(r"\+?\d(?:[\s().-]*\d){6,}")


def _texts(chat_ctx: Any) -> list[tuple[str, str]]:
    """(kind, text) for every tool result and spoken message, in order."""
    out = []
    for item in chat_ctx.items:
        kind = getattr(item, "type", None)
        if kind == "function_call_output":
            out.append(("tool", str(getattr(item, "output", ""))))
        elif kind == "message" and item.role in ("user", "assistant"):
            out.append((item.role, getattr(item, "text_content", "") or ""))
    return out


def known_times(chat_ctx: llm.ChatContext) -> set[str]:
    """Clock times, as "H:MM" in 12-hour form, that the agent may say: any a tool
    returned, the patient said, the agent already said, or the clinic's hours."""
    times = {f"{config.CLINIC_OPEN_HOUR % 12 or 12}:00", f"{config.CLINIC_CLOSE_HOUR % 12 or 12}:00"}
    for role, text in _texts(chat_ctx):
        for m in list(_CLOCK.finditer(text)) + list(_CLOCK_24.finditer(text)):
            times.add(f"{int(m.group(1)) % 12 or 12}:{m.group(2) or '00'}")
        if role == "user":
            # "at 3": a bare hour the patient said grounds H:00.
            times |= {f"{int(t) % 12 or 12}:00" for t in re.findall(r"\b\d{1,2}\b", text) if 1 <= int(t) <= 23}
    return times


def known_days(chat_ctx: llm.ChatContext) -> set[str]:
    """Weekday names the agent may say: any a tool returned, the patient said, the
    agent already said, plus today's and tomorrow's names."""
    now = booking.clinic_now()
    days = {scheduling.WEEKDAYS[now.weekday()], scheduling.WEEKDAYS[(now.weekday() + 1) % 7]}
    for _, text in _texts(chat_ctx):
        days |= {m.group(1).lower() for m in _WEEKDAY.finditer(text)}
    return days


def result_values(patient: dict[str, Any]) -> re.Pattern[str]:
    """The patient's own values, as the agent would say them."""
    glucose = float(patient["blood_glucose"])
    values = [str(patient["hba1c"]), str(int(glucose)) if glucose.is_integer() else str(glucose)]
    return re.compile("|".join(rf"(?<![\d.]){re.escape(v)}(?![\d])" for v in values))


# Why a sentence was held back, told to the model when it is asked again.
BLOCK_REASONS = {
    "time": "it named a time that no tool returned and the person did not say; find_slot gives real times",
    "day": "it named a day that no tool returned and the person did not say",
    "condition": "it named a medical condition, which you never do, not even to deny it",
    "phone": "it contained a phone number, which you never read out",
    "results": "it contained the patient's results before verify_identity confirmed the patient",
    "clinic": "it named the clinic before the person confirmed they are the patient",
    "stage": "it was a stage direction; actions are tool calls, and only speech is said",
    "offer": "a slot find_slot returned is an offer: say it and ask whether it works, then wait",
}


def offer_pending(chat_ctx: llm.ChatContext) -> bool:
    """The last thing that happened this turn is a search that found a slot.
    The reply to that must be a question: in evals the model announced "I've
    booked you for 9 AM" from a search result, before anyone agreed."""
    for item in reversed(chat_ctx.items):
        kind = getattr(item, "type", None)
        if kind == "message" and item.role == "user":
            return False
        if kind == "function_call_output":
            return getattr(item, "name", "") == "find_slot" and str(item.output).startswith("status: free")
    return False


class SpeechGate:
    """Holds each sentence until it is complete and releases it only if it passes.

    The checks are facts, not vocabulary: a time or day must come from a tool or
    the caller; the patient's values and the clinic's name wait for a confirmed
    identity; no phone number and no condition name is ever spoken. The first
    sentence that fails stops the reply.
    """

    def __init__(self, *, times: set[str], days: set[str], values: re.Pattern[str] | None,
                 clinic: str | None) -> None:
        self._times, self._days, self._values, self._clinic = times, days, values, clinic
        self._pending = ""
        self.released = ""
        self.blocked: str | None = None
        self.reason = ""

    def feed(self, piece: str) -> str:
        if self.blocked is not None:
            return ""
        self._pending += piece
        out = ""
        while (m := _SENTENCE_END.search(self._pending)):
            sentence, self._pending = self._pending[:m.end()], self._pending[m.end():]
            if not self._ok(sentence):
                self.blocked, self._pending = sentence, ""
                break
            out += sentence
        self.released += out
        return out

    def finish(self) -> str:
        rest, self._pending = self._pending, ""
        if self.blocked is not None or not rest:
            return ""
        if not self._ok(rest):
            self.blocked = rest
            return ""
        self.released += rest
        return rest

    def _ok(self, sentence: str) -> bool:
        self.reason = self._problem(sentence) or ""
        return not self.reason

    def _problem(self, sentence: str) -> str | None:
        if not all(f"{int(m.group(1)) % 12 or 12}:{m.group(2) or '00'}" in self._times
                   for m in _CLOCK.finditer(sentence)):
            return "time"
        # In an eval the agent confirmed "a callback on Monday morning" after the
        # caller said "next morning" on a Tuesday.
        if {m.group(1).lower() for m in _WEEKDAY.finditer(_CLINIC_WEEK.sub(" ", sentence))} - self._days:
            return "day"
        if _STAGE.fullmatch(sentence.strip()):
            return "stage"
        if CONDITION.search(sentence):
            return "condition"
        if PHONE.search(sentence):
            return "phone"
        if self._values is not None and self._values.search(sentence):
            return "results"
        if self._clinic and self._clinic.lower() in sentence.lower():
            return "clinic"
        return None


MAX_TOOL_RESULTS_PER_TURN = 2


def tool_results_this_turn(chat_ctx: llm.ChatContext) -> int:
    """Tool results since the patient last spoke."""
    count = 0
    for item in reversed(chat_ctx.items):
        kind = getattr(item, "type", None)
        if kind == "message" and item.role == "user":
            break
        if kind == "function_call_output":
            count += 1
    return count


# Schemas are resent with every LLM request, so tools the call cannot use yet
# are withheld. Which ones is decided by the call's state, never by words.
IDENTITY_TOOLS = frozenset({"verify_identity"})
BOOKING_TOOLS = frozenset({"find_slot", "book_appointment"})


def tools_for(identity: str, results_told: bool) -> set[str]:
    """Tool names on offer: identity until it is settled, booking once the
    confirmed patient has heard their results, callbacks and ending always."""
    names = {"request_callback", "end_call"}
    if identity == "unknown":
        names |= IDENTITY_TOOLS
    if identity == "patient" and results_told:
        names |= BOOKING_TOOLS
    return names


def results_told(chat_ctx: llm.ChatContext, patient: dict[str, Any]) -> bool:
    """The agent has said the patient's values aloud in this call."""
    values = result_values(patient)
    return any(role == "assistant" and values.search(text) for role, text in _texts(chat_ctx))


def _slot_facts(slot: datetime, now: datetime) -> str:
    """A slot as facts: "today (Saturday 19 September) at 9:00 AM". The weekday
    sits next to "today" because the model once called a Saturday "today, Monday"."""
    when = scheduling.describe(slot, now)
    local = slot.astimezone(now.tzinfo)
    if when.startswith(("today", "tomorrow")):
        word, rest = when.split(" ", 1)
        return f"{word} ({local.strftime('%A')} {local.day} {local.strftime('%B')}) {rest}"
    return when


def _slot_id(slot: datetime) -> str:
    """How a slot is named to the model: local date and time, no zone."""
    return slot.astimezone(booking.clinic_now().tzinfo).strftime("%Y-%m-%dT%H:%M")


class HealthcareAgent(Agent):
    """Voice agent for one patient. The prompt holds their name, not their results."""

    def __init__(self, patient: dict[str, Any], room_name: str = "", call_log: CallLog | None = None) -> None:
        super().__init__(instructions=config.build_system_prompt(patient))
        self._patient = patient
        self._room_name = room_name
        self._call_log = call_log or NullLog()
        self._llm_turn = 0
        self._tts_turn = 0
        self._tool_seq = 0
        # Who answered: "unknown", "patient" or "other". Set only by
        # verify_identity, and "other" never becomes "patient" in the same call.
        self.identity = "unknown"
        # Slots a search has offered, mapped to how many patient turns existed
        # then. A slot can be booked only if it was offered and the patient has
        # spoken since: the model once offered a time and booked it in one breath.
        self._offered_slots: dict[str, int] = {}
        self._last_offered: datetime | None = None
        # True when the last offer was itself the answer to "the next day", so a
        # repeated day_after_offered refers to that offer rather than adding a day.
        self._last_offer_was_day_after = False
        self.ended = False
        # Second opinion on "I am the patient"; tests replace or remove it.
        self.identity_checker = identity_checker
        # Tool results are mirrored here so post-call analysis and the Opik
        # trace can read what actually happened, not just what was said.
        self.tool_results: list[dict[str, Any]] = []

    @property
    def patient(self) -> dict[str, Any]:
        return self._patient

    def _remember_offered(self, ctx: RunContext, slots: list[datetime], *, from_day_after: bool = False) -> None:
        turns = len(_user_messages(ctx))
        for slot in slots:
            # Offering a slot again reopens it.
            self._offered_slots[scheduling.to_utc_iso(slot)] = turns
            self._last_offered = slot
            self._last_offer_was_day_after = from_day_after

    def _answered_since_offer(self, ctx: RunContext, slot: datetime) -> bool:
        """The patient's latest reply is the first since this slot was offered.

        An offer stays open for one answer. In an eval the patient turned a slot
        down, was asked what else would suit, said a vague "that works", and the
        declined slot was booked."""
        offered_at = self._offered_slots.get(scheduling.to_utc_iso(slot))
        return offered_at is not None and len(_user_messages(ctx)) == offered_at + 1

    def _day_after_offered(self, day: str | None, time: str | None) -> tuple[str | None, str | None, str | None]:
        """Turn day_after_offered into a concrete day and, if missing, the offered time.

        Returns (day, time, problem). The offered time is kept, so "the next day"
        means the same time one day later.
        """
        if day != "day_after_offered":
            return day, time, None
        if self._last_offered is None:
            return None, time, "nothing has been offered yet"
        target = self._last_offered + timedelta(days=0 if self._last_offer_was_day_after else 1)
        ahead = (target.date() - booking.clinic_now().date()).days
        if ahead == 0:
            word = "today"
        elif ahead == 1:
            word = "tomorrow"
        elif 1 < ahead < 7:
            word = scheduling.WEEKDAYS[target.weekday()]
        else:
            return None, time, "that day is too far ahead to name"
        return word, time or target.strftime("%H:%M"), None

    def _booking_closed(self, ctx: RunContext) -> str | None:
        """Why booking is not open yet, if it is not. Checked inside the tools:
        withholding a tool from the request is not enough, because the model has
        called tools it was not offered."""
        if self.identity != "patient":
            return "the person has not been confirmed as the patient"
        if not results_told(ctx.session.history, self._patient):
            return "the patient has not been told their results yet"
        return None

    def _log(self, tool: str, ok: bool, **detail: Any) -> None:
        self.tool_results.append({"tool": tool, "ok": ok, **json.loads(json.dumps(detail, default=str))})

    # --- timing instrumentation ---------------------------------------------
    # Each pipeline stage is wrapped so our own code timestamps what goes in
    # and what comes out. Durations are computed here, not taken from providers.

    def _tool_started(self, tool: str, **args: Any) -> tuple[int, float]:
        self._tool_seq += 1
        started = self._call_log.event("tool_call", tool=tool, seq=self._tool_seq, args=args)
        return self._tool_seq, started

    def _tool_finished(self, call_id: tuple[int, float], tool: str, result: str) -> None:
        seq, started = call_id
        self._call_log.event("tool_result", tool=tool, seq=seq, result=result,
                             duration_ms=round(self._call_log.elapsed_ms() - started, 1))

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings):
        log = self._call_log
        speech_started: float | None = None
        async for ev in Agent.default.stt_node(self, audio, model_settings):
            if isinstance(ev, stt.SpeechEvent):
                if ev.type == stt.SpeechEventType.START_OF_SPEECH:
                    speech_started = log.event("stt_speech_start")
                elif ev.type == stt.SpeechEventType.END_OF_SPEECH:
                    log.event("stt_speech_end", since_speech_start_ms=_since(log, speech_started))
                elif ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                    log.event("stt_final", text=ev.alternatives[0].text,
                              confidence=ev.alternatives[0].confidence,
                              since_speech_start_ms=_since(log, speech_started))
                elif ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT and ev.alternatives:
                    log.event("stt_interim", text=ev.alternatives[0].text)
            yield ev

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        self._call_log.event("user_turn_committed", text=new_message.text_content)

    def _gate(self, chat_ctx: llm.ChatContext) -> SpeechGate:
        confirmed = self.identity == "patient"
        return SpeechGate(times=known_times(chat_ctx), days=known_days(chat_ctx),
                          values=None if confirmed else result_values(self._patient),
                          clinic=config.CLINIC_NAME if self.identity == "unknown" else None)

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings):
        log = self._call_log
        self._llm_turn += 1
        turn = self._llm_turn
        last = chat_ctx.items[-1] if chat_ctx.items else None
        if self.ended and getattr(last, "type", None) == "function_call_output" and any(
                getattr(i, "type", None) == "message" and i.role == "assistant"
                for i in chat_ctx.items[-4:]):
            # end_call came with the closing words; nothing more to say.
            return
        allowed = tools_for(self.identity, results_told(chat_ctx, self._patient))
        tools = [t for t in tools if getattr(getattr(t, "info", None), "name", None) in allowed]
        if tool_results_this_turn(chat_ctx) >= MAX_TOOL_RESULTS_PER_TURN:
            # The framework stops after a few tool rounds without saying anything;
            # in an eval the model made four calls and the patient heard silence.
            # With no tools on offer, the model has to speak.
            log.event("guard_tools_withheld", turn=turn, results=tool_results_this_turn(chat_ctx))
            tools = []
        request_ctx = chat_ctx
        must_ask = offer_pending(chat_ctx)
        for attempt in range(1, REPLY_ATTEMPTS + 1):
            sent = log.event("llm_request", turn=turn, attempt=attempt, items=len(request_ctx.items), tools=len(tools),
                             tool_names=[getattr(getattr(t, "info", None), "name", None) for t in tools],
                             last_item_type=getattr(last, "type", None),
                             last_item=_item_text(last))
            first: float | None = None
            text: list[str] = []
            tool_calls: list[dict[str, str]] = []
            usage = None
            cutter = QuestionCutter()
            dots = DotCollapser()
            gate = self._gate(chat_ctx)
            try:
                async for chunk in Agent.default.llm_node(self, request_ctx, tools, model_settings):
                    delta = None if isinstance(chunk, str) else getattr(chunk, "delta", None)
                    content = chunk if isinstance(chunk, str) else getattr(delta, "content", None)
                    calls = getattr(delta, "tool_calls", None) or []
                    if first is None and (content or calls):
                        first = log.event("llm_first_token", turn=turn, ttft_ms=round(log.elapsed_ms() - sent, 1))
                    if content:
                        # Text goes through the checks as plain strings, which llm_node
                        # may yield; tool calls and usage keep their original chunk.
                        safe = gate.feed(cutter.feed(dots.feed(content)))
                        if safe:
                            text.append(safe)
                            if not must_ask:
                                yield safe
                    if calls and (cutter.asked or gate.blocked is not None):
                        # After a question the other person speaks next; after a
                        # blocked sentence nothing it led to can stand.
                        log.event("guard_dropped_tool_call", turn=turn,
                                  calls=[{"name": c.name, "arguments": c.arguments} for c in calls])
                        calls = []
                    if calls and tool_calls:
                        # One action per response: the model has fired several at once,
                        # even with parallel calls switched off.
                        log.event("guard_dropped_tool_call", turn=turn, reason="one call per response",
                                  calls=[{"name": c.name, "arguments": c.arguments} for c in calls])
                        calls = []
                    elif len(calls) > 1:
                        log.event("guard_dropped_tool_call", turn=turn, reason="one call per response",
                                  calls=[{"name": c.name, "arguments": c.arguments} for c in calls[1:]])
                        calls = calls[:1]
                    for call in calls:
                        tool_calls.append({"name": call.name, "arguments": call.arguments})
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if isinstance(chunk, str):
                        continue
                    if content or getattr(delta, "tool_calls", None):
                        if calls or getattr(chunk, "usage", None):
                            yield chunk.model_copy(update={"delta": delta.model_copy(
                                update={"content": None, "tool_calls": calls})})
                        continue
                    yield chunk
            except Exception as exc:
                # Both providers refusing, or a provider rejecting the request,
                # costs this attempt, not the call.
                log.event("guard_llm_error", turn=turn, attempt=attempt, error=f"{type(exc).__name__}: {exc}")
                if attempt == REPLY_ATTEMPTS:
                    raise
                await asyncio.sleep(attempt)   # both providers limited: a pause, not three instant failures
                continue
            tail = gate.feed(cutter.feed(dots.finish())) + gate.finish()
            if tail:
                text.append(tail)
                if not must_ask:
                    yield tail
            if must_ask:
                # Held until complete: an answer to a search must ask about the slot.
                if "".join(text).strip() and not cutter.asked and not tool_calls:
                    gate.blocked, gate.reason, text = "".join(text), "offer", []
                elif text:
                    yield "".join(text)
            if cutter.cut:
                log.event("guard_cut_after_question", turn=turn, removed=cutter.removed)
            if gate.blocked is not None:
                log.event(f"guard_blocked_{gate.reason}", turn=turn, sentence=gate.blocked)
            log.event("llm_response", turn=turn, attempt=attempt, text="".join(text), tool_calls=tool_calls,
                      total_ms=round(log.elapsed_ms() - sent, 1),
                      prompt_tokens=getattr(usage, "prompt_tokens", None),
                      completion_tokens=getattr(usage, "completion_tokens", None))
            spoken = "".join(text).strip()
            if tool_calls or spoken:
                # Something was said or done. A sentence blocked after others were
                # spoken is simply left out: restarting would repeat them.
                return
            # Nothing usable: ask again, saying why, if a check stopped it.
            request_ctx = chat_ctx.copy()
            if gate.blocked is not None:
                request_ctx.add_message(role="system", content=(
                    f'Your reply "{gate.blocked.strip()}" was not said: {BLOCK_REASONS[gate.reason]}. '
                    "Reply again without that."))
            else:
                # gpt-oss occasionally returns nothing at all.
                log.event("guard_empty_reply", turn=turn, attempt=attempt)
        # Nothing passed. Silence is better than a sentence nobody chose.
        log.event("guard_silent_turn", turn=turn)

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        log = self._call_log
        self._tts_turn += 1
        turn = self._tts_turn
        received: list[str] = []
        first_text: float | None = None

        async def _tap() -> AsyncIterable[str]:
            nonlocal first_text
            async for piece in text:
                if first_text is None:
                    first_text = log.event("tts_text_in_first", turn=turn)
                received.append(piece)
                yield piece

        first_audio: float | None = None
        audio_seconds = 0.0
        async for frame in Agent.default.tts_node(self, _tap(), model_settings):
            if first_audio is None:
                first_audio = log.event("tts_audio_out_first", turn=turn,
                                        since_text_in_ms=_since(log, first_text))
            audio_seconds += frame.samples_per_channel / frame.sample_rate
            yield frame
        log.event("tts_done", turn=turn, text="".join(received), audio_seconds=round(audio_seconds, 2),
                  since_text_in_ms=_since(log, first_text))

    # --- tools ----------------------------------------------------------------
    # Every reply states what is true, as "field: value" pairs. How to handle
    # each state is in the prompt; what to say is the model's choice.

    @function_tool
    async def verify_identity(self, ctx: RunContext, is_patient: BoolArg, name_given: str = "") -> str:
        """Record who answered, as soon as they say, either way.

        Args:
            is_patient: True only if they confirmed they are the patient themselves.
            name_given: The name they said, if any.
        """
        call_id = self._tool_started("verify_identity", is_patient=is_patient, name_given=name_given)
        result = await self._verify(ctx, is_patient, name_given)
        self._tool_finished(call_id, "verify_identity", result)
        return result

    async def _speaker_is_patient(self, ctx: RunContext) -> bool:
        """A second model reads what the caller said and answers one question:
        did the speaker say they themselves are the patient? Identity is the
        privacy gate, and "Kavya's right here, says it's fine" once passed it.
        Fails closed: no answer means not confirmed."""
        checker = getattr(self, "identity_checker", None)
        if checker is None:
            return True
        lines = [f"{'PERSON' if i.role == 'user' else 'CLINIC'}: {i.text_content or ''}"
                 for i in ctx.session.history.items
                 if getattr(i, "type", "message") == "message" and i.role in ("user", "assistant")][-5:]
        try:
            verdict = checker(self._patient["name"], lines)
            return bool(await verdict if asyncio.iscoroutine(verdict) else verdict)
        except Exception as exc:
            self._call_log.event("guard_identity_check_failed", error=f"{type(exc).__name__}: {exc}")
            return False

    def _name_matches(self, name: str) -> bool:
        """The name given is the patient's first, last or full name."""
        given = re.sub(r"[^a-z ]", "", name.lower()).split()
        record = self._patient["name"].lower().split()
        return bool(given) and all(part in record for part in given)

    async def _verify(self, ctx: RunContext, is_patient: bool, name_given: str = "") -> str:
        turns = len(_user_messages(ctx))
        if not turns:
            return "identity: unknown · reason: nobody has answered yet"
        if is_patient and name_given.strip() and not self._name_matches(name_given):
            # "Yes, Arjan here" for Arjun: a mishearing or a relative. The
            # same name, or a plain yes once they have been asked, settles it.
            self._name_asked_at = turns
            self._call_log.event("guard_identity_name_differs", name_given=name_given)
            return (f"identity: unclear · reason: the name given, {name_given.strip()}, is not the patient's name "
                    "· ask whether you are speaking with the patient")
        if is_patient and getattr(self, "_name_asked_at", None) == turns:
            return "identity: unclear · reason: they have not answered the question about their name yet"
        if is_patient and self.identity != "patient" and not await self._speaker_is_patient(ctx):
            self._call_log.event("guard_identity_second_opinion")
            return ("identity: unclear · reason: their words do not say they themselves are the patient "
                    "· ask whether you are speaking with the patient")
        if self.identity == "other" and is_patient:
            # A caller who said they were someone else, then "joked", is the
            # oldest trick for getting someone's results.
            self._call_log.event("guard_identity_switch")
            return "identity: someone else · unchanged: whoever said they are not the patient stays so in this call"
        self.identity = "patient" if is_patient else "other"
        self._log("verify_identity", True, identity=self.identity)
        if not is_patient:
            return "identity: someone else"
        glucose = float(self._patient["blood_glucose"])
        return (f"identity: patient confirmed · hba1c: {self._patient['hba1c']}% · fasting_glucose: "
                f"{int(glucose) if glucose.is_integer() else glucose} mg/dL")

    @function_tool
    async def find_slot(
        self,
        ctx: RunContext,
        day: ApptDayArg = None,
        time: TimeArg = None,
        window: WindowArg = None,
    ) -> str:
        """Find the free appointment closest to what the patient wants, and offer it.

        Fill only what they asked for; leave everything empty for the earliest.

        Args:
            day: The day they want. "day_after_offered" when they want the day after the one offered.
            time: A time they named, 24-hour HH:MM.
            window: Where in the clinic day they want to come: early (9 AM to noon), middle (noon to 3 PM) or late (3 to 5 PM, the end of the clinic day).
        """
        part_of_day = WINDOW_PART_OF_DAY.get(window) if window else None
        call_id = self._tool_started("find_slot", day=day, time=time, window=window)
        result = await self._find(ctx, day, time, part_of_day)
        self._tool_finished(call_id, "find_slot", result)
        return result

    async def _find(self, ctx: RunContext, day: str | None, time: str | None,
                    part_of_day: PartOfDay | None) -> str:
        closed = self._booking_closed(ctx)
        if closed:
            self._call_log.event("guard_booking_closed", tool="find_slot", reason=closed)
            return f"status: not searched · reason: {closed}"
        relative = day == "day_after_offered"
        day, time, problem = self._day_after_offered(day, time)
        if problem:
            return f"status: not searched · reason: {problem}"
        now = booking.clinic_now()
        offered_day = self._last_offered.date() if (self._last_offered and day is None and time) else None

        def _lookup():
            with db.session() as conn:
                if offered_day is not None:
                    # A time with no day, answering an offer, means that day.
                    hour, minute = (int(x) for x in time.split(":")[:2])
                    asked = datetime(offered_day.year, offered_day.month, offered_day.day, hour, minute,
                                     tzinfo=now.tzinfo)
                    return booking.nearest_free_slot(conn, now=now, pref=booking.around(asked)), asked
                return booking.find_slot(conn, now=now, day=day, time=time, part_of_day=part_of_day)

        try:
            slot, asked = await asyncio.to_thread(_lookup)
        except (SchedulingError, ValueError) as exc:
            return f"status: not searched · reason: {exc}"
        self._log("find_slot", slot is not None, day=day, time=time, part_of_day=part_of_day, slot=slot)
        if slot is None:
            return f"status: nothing free · searched: the next {config.APPOINTMENT_MAX_DAYS_AHEAD} days"
        self._remember_offered(ctx, [slot], from_day_after=relative)
        # Compared with what was asked, not with the search's anchor: a request
        # for 7 PM is anchored at the last slot, which is not what they asked.
        exact = time is not None and slot.strftime("%H:%M") == time and (day is None or slot.date() == asked.date())
        searched = " ".join(x for x in (day or ("the offered day" if offered_day else "any day"),
                                        part_of_day and f"in the {part_of_day}", time and f"near {time}") if x)
        return (f"status: free · searched: {searched} · slot_id: {_slot_id(slot)} · slot: {_slot_facts(slot, now)} · "
                f"{'exactly the time asked' if exact else 'closest free to that'} · booked: no")

    @function_tool
    async def book_appointment(self, ctx: RunContext, slot_id: str) -> str:
        """Book a slot find_slot offered, after the patient said yes to it.

        Args:
            slot_id: The slot_id find_slot returned.
        """
        call_id = self._tool_started("book_appointment", slot_id=slot_id)
        result = await self._book(ctx, slot_id)
        self._tool_finished(call_id, "book_appointment", result)
        return result

    async def _book(self, ctx: RunContext, slot_id: str) -> str:
        closed = self._booking_closed(ctx)
        if closed:
            self._call_log.event("guard_booking_closed", tool="book_appointment", reason=closed)
            return f"status: not booked · reason: {closed}"
        now = booking.clinic_now()
        try:
            slot = datetime.fromisoformat(slot_id.strip()).replace(tzinfo=now.tzinfo)
        except ValueError:
            return "status: not booked · reason: slot_id is not one find_slot returned"
        if scheduling.to_utc_iso(slot) not in self._offered_slots:
            self._call_log.event("guard_book_not_offered", slot_id=slot_id)
            return "status: not booked · reason: this slot has not been offered to the patient"
        if not self._answered_since_offer(ctx, slot):
            self._call_log.event("guard_book_before_answer", slot_id=slot_id)
            return ("status: not booked · reason: the offer is open only for the patient's reply to it; "
                    "offer the slot again and wait for their answer")

        def _request():
            with db.session() as conn:
                return booking.request_appointment(conn, patient=self._patient, now=now, slot=slot,
                                                   source_room=self._room_name)

        out = await asyncio.to_thread(_request)
        self._remember_offered(ctx, out.alternatives)
        self._log("book_appointment", out.status == "booked", status=out.status, reason=out.reason,
                  requested=out.requested, record=out.record, alternatives=out.alternatives,
                  existing=out.existing)
        if out.status == "booked":
            logger.info("booked %s at %s", out.record["reference"], out.record["slot_start_utc"])
            return (f"status: booked · slot: {_slot_facts(out.requested, now)} · doctor: "
                    f"{config.DOCTOR['name']} · reference: {out.record['reference']}")
        if out.status == "has_existing":
            existing_at = scheduling.describe(scheduling.from_iso(out.existing["slot_start_utc"]), now)
            return f"status: not booked · reason: the patient already has an appointment {existing_at}"
        if not out.alternatives:
            return f"status: not booked · reason: {out.reason} · nothing free after it"
        alt = out.alternatives[0]
        return (f"status: not booked · reason: {out.reason} · nearest_free: slot_id {_slot_id(alt)}, "
                f"{_slot_facts(alt, now)} · booked: no")

    @function_tool
    async def request_callback(
        self,
        ctx: RunContext,
        requested_by: str = "",
        in_minutes: int | None = None,
        day: DayArg = None,
        time: TimeArg = None,
        part_of_day: PartOfDayArg = None,
        no_preference: bool = False,
        phrase: str = "",
    ) -> str:
        """Schedule a phone call back to the patient, never an appointment. Fill only what they said.

        Args:
            requested_by: Who asked.
            in_minutes: A delay, e.g. 60 for "in an hour".
            time: 24-hour HH:MM.
            no_preference: They want a callback but have no preferred time.
            phrase: Their words about timing.
        """
        call_id = self._tool_started("request_callback", requested_by=requested_by, in_minutes=in_minutes,
                                     day=day, time=time, part_of_day=part_of_day,
                                     no_preference=no_preference, phrase=phrase)
        result = await self._callback(phrase, requested_by, in_minutes, day, time, part_of_day, no_preference)
        self._tool_finished(call_id, "request_callback", result)
        return result

    async def _callback(self, phrase: str, requested_by: str, in_minutes: int | None, day: Day | None,
                        time: str | None, part_of_day: PartOfDay | None, no_preference: bool) -> str:
        if not no_preference and all(v is None for v in (in_minutes, day, time, part_of_day)):
            return "status: not scheduled · reason: no time given and no_preference not set"
        now = callback_queue.patient_now(self._patient)

        def _schedule():
            with db.session() as conn:
                return callback_queue.schedule(
                    conn, patient=self._patient, now=now, phrase=phrase, requested_by=requested_by,
                    in_minutes=in_minutes, day=day, time=time, part_of_day=part_of_day,
                    no_time_given=no_preference, source_room=self._room_name)

        try:
            record = await asyncio.to_thread(_schedule)
        except SchedulingError as exc:
            logger.info("callback not scheduled: %s", exc)
            self._log("request_callback", False, error=str(exc))
            return f"status: not scheduled · reason: {exc}"
        logger.info("callback %s queued for %s", record["reference"], record["due_utc"])
        self._log("request_callback", True, result=record)
        reply = f"status: scheduled · when: {scheduling.describe(record['scheduled'], now)}"
        if record["moved_because"]:
            reply += (f" · asked_for: {scheduling.describe(record['requested'], now)} · moved_because: "
                      f"{' and '.join(record['moved_because'])}")
        return reply

    @function_tool
    async def end_call(self, ctx: RunContext) -> str:
        """End the call, together with your closing words."""
        call_id = self._tool_started("end_call")
        self.ended = True
        result = "status: ending after your closing words"
        self._tool_finished(call_id, "end_call", result)
        return result


IDENTITY_QUESTION = """\
A clinic called asking for {name}. The end of the call so far (CLINIC is the \
caller, PERSON is who answered):
{lines}

Has PERSON said, in any words, that they themselves are {name}? Answering the \
request for {name} by affirming it, or by saying they are the one on the line, \
counts. A statement that {name} is nearby, agrees, or consents does not. Answer \
yes or no."""


async def identity_checker(name: str, lines: list[str]) -> bool:
    """Yes/no from the small analysis model, with the same provider fallback as
    post-call analysis: Groq's free daily limit on it runs out."""
    text, _ = await asyncio.wait_for(post_call._complete(post_call._build_llm(), IDENTITY_QUESTION.format(
        name=name, lines="\n".join(lines))), timeout=6)
    return text.strip().lower().startswith("yes")


def _select_patient(ctx: JobContext) -> dict[str, Any]:
    """Pick the patient this job is about.

    A dispatched job carries the patient id in its metadata. Console and dev
    runs carry nothing, so the first real patient is used for frictionless testing.
    """
    raw = (ctx.job.metadata or "").strip()
    with db.session() as conn:
        if raw:
            try:
                patient_id = json.loads(raw).get("patient_id")
                if patient_id:
                    return db.get_patient(conn, patient_id)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("unusable job metadata (%s); falling back", exc)
        callable_patients = db.list_callable_patients(conn)
    if not callable_patients:
        raise LookupError("no patient is waiting to be called: everyone already has an appointment")
    return callable_patients[0]


def _name_keyterms(patient: dict[str, Any]) -> list[str]:
    """Names worth boosting in speech recognition for this call."""
    parts = patient["name"].split()
    return [patient["name"], *parts, config.CLINIC_NAME, config.AGENT_DISPLAY_NAME]


def _build_llm() -> llm.LLM:
    """Two providers serving the same model, each the other's fallback.

    Both serve gpt-oss-120b, so a fallback mid-call does not change behaviour.
    Reasoning effort is low on both: equal reply quality on our scenarios and
    far fewer reasoning tokens, which count against rate limits and latency.
    """
    # One tool call per response: three parallel searches once registered three
    # offers the patient never heard.
    primary = inference.LLM(
        model=config.LIVEKIT_LLM_MODEL,
        extra_kwargs={"reasoning_effort": config.LLM_REASONING_EFFORT, "parallel_tool_calls": False},
    )
    fallback = openai.LLM(
        model=config.LLM_MODEL,
        base_url=config.GROQ_BASE_URL,
        api_key=os.environ["GROQ_API_KEY"],
        reasoning_effort=config.LLM_REASONING_EFFORT,
        parallel_tool_calls=False,
    )
    livekit, groq = primary, fallback
    order = [livekit, groq] if config.LLM_PRIMARY == "livekit" else [groq, livekit]
    # No retries on the same provider: a 429 moves to the other one at once
    # instead of backing off while the patient waits in silence.
    return llm.FallbackAdapter(order, max_retry_per_llm=0)


def _build_session(vad: silero.VAD, patient: dict[str, Any]) -> AgentSession:
    """Assemble the speech pipeline from the providers chosen in config."""
    return AgentSession(
        stt=deepgram.STT(
            model=config.STT_MODEL,
            language="en-US",
            # Bias recognition toward the names this call depends on. Without it,
            # a patient's name was heard as something unrecognisable and the agent wrongly
            # decided a stranger had answered.
            keyterm=_name_keyterms(patient),
        ),
        llm=_build_llm(),
        tts=deepgram.TTS(model=config.TTS_MODEL),
        vad=vad,
    )


async def _open_conversation(session: AgentSession, *, let_them_speak_first: bool) -> None:
    """Open the call. The words are generated; the speech gate keeps the
    clinic's name out until whoever answered has confirmed who they are.

    On a real outbound call the callee says "hello" first, so we wait briefly for
    them, then open regardless so a silent pickup is not met with silence.
    """
    if let_them_speak_first:
        heard = asyncio.Event()

        def _on_transcript(_ev: Any) -> None:
            heard.set()

        session.on("user_input_transcribed", _on_transcript)
        try:
            await asyncio.wait_for(heard.wait(), GREETING_FALLBACK_SECONDS)
            logger.info("callee spoke first; responding")
            return   # their words start the first turn
        except asyncio.TimeoutError:
            logger.info("no speech within %.1fs; opening anyway", GREETING_FALLBACK_SECONDS)
        finally:
            session.off("user_input_transcribed", _on_transcript)

    session.generate_reply(instructions=config.OPENING_INSTRUCTION)


def _since(log: CallLog, started: float | None) -> float | None:
    return None if started is None else round(log.elapsed_ms() - started, 1)


def _item_text(item: Any) -> str | None:
    """Short readable form of a chat item for the log."""
    if item is None:
        return None
    kind = getattr(item, "type", None)
    if kind == "message":
        return (item.text_content or "")[:500]
    if kind == "function_call":
        return f"{item.name}({item.arguments})"
    if kind == "function_call_output":
        return str(item.output)[:500]
    return None


def _log_session_events(session: AgentSession, log: CallLog) -> None:
    """Timestamp state changes as our process observes them.

    agent_state "speaking" marks when audio playback began, so the gap from
    user_state "listening" (the patient stopped talking) to it is the delay
    the patient actually experiences.
    """
    session.on("user_state_changed", lambda ev: log.event("user_state", old=ev.old_state, new=ev.new_state))
    session.on("agent_state_changed", lambda ev: log.event("agent_state", old=ev.old_state, new=ev.new_state))
    session.on("speech_created", lambda ev: log.event("speech_created", source=ev.source,
                                                      user_initiated=ev.user_initiated))
    session.on("conversation_item_added", lambda ev: log.event(
        "conversation_item", role=getattr(ev.item, "role", None), text=_item_text(ev.item),
        interrupted=getattr(ev.item, "interrupted", None)))
    session.on("error", lambda ev: log.event("error", source=type(ev.source).__name__, error=str(ev.error)))
    session.on("close", lambda ev: log.event("session_close", reason=str(ev.reason)))


def _save_transcript_for(room: str, agent: HealthcareAgent, session: AgentSession) -> Path:
    """Persist the conversation so later phases have something to analyze."""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = TRANSCRIPT_DIR / f"{room}_{stamp}.json"

    payload = {
        "room": room,
        "patient": agent.patient,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "history": session.history.to_dict() if session else {"items": []},
        "tool_results": agent.tool_results,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("transcript saved to %s", path)
    return path


# Long enough for the last words to reach the caller before the line drops.
HANGUP_DELAY_SECONDS = 2.0


def _masked(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return f"******{digits[-4:]}" if len(digits) >= 4 else "******"


def _in_console() -> bool:
    """True under `agent.py console`, where the terminal is the other party."""
    try:
        from livekit.agents.cli.cli import AgentsConsole
        return bool(AgentsConsole.get_instance().enabled)
    except Exception:
        return False


def _job_metadata(ctx: JobContext) -> dict[str, Any]:
    try:
        return json.loads((ctx.job.metadata or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}


def _phone_to_dial(ctx: JobContext, patient: dict[str, Any]) -> str:
    """The number to ring, or "" when the agent is not the one dialling.

    A dispatched phone job carries {"phone": ...} or {"transport": "sip"} in its
    metadata; the patient record supplies the number in the second case.
    """
    meta = _job_metadata(ctx)
    if meta.get("phone"):
        return str(meta["phone"])
    return str(patient.get("phone", "")) if meta.get("transport") == "sip" else ""


def _transport(ctx: JobContext, dial_to: str) -> str:
    """How the audio reaches the patient.

    - "sip": the agent dials out through an outbound trunk.
    - "sip_inbound": the call arrives already in progress, because the phone
      provider bridged it to our inbound trunk. Still a phone call in every way
      that matters: the callee says hello first, and we hang up at the end.
    - "webrtc": a browser, for development.
    """
    if dial_to:
        return "sip"
    return "sip_inbound" if _job_metadata(ctx).get("transport") == "sip_inbound" else "webrtc"


def _hang_up_when_finished(ctx: JobContext, session: AgentSession, agent: HealthcareAgent,
                           log: CallLog) -> None:
    """End a phone call once the agent has called end_call and finished speaking.

    The model decides the conversation is over; code only waits for the last
    words to play out. Without this the patient is left holding a silent line.
    """
    hanging_up = False

    def _on_state(ev: Any) -> None:
        nonlocal hanging_up
        if hanging_up or not agent.ended or getattr(ev, "new_state", None) != "listening":
            return
        hanging_up = True

        async def _end() -> None:
            await asyncio.sleep(HANGUP_DELAY_SECONDS)
            log.event("hangup", reason="agent ended the call")
            await telephony.hang_up(ctx.api, ctx.room.name)

        asyncio.create_task(_end())

    session.on("agent_state_changed", _on_state)


# Calls in progress in this process, so the session-end hook can reach them.
_CALLS: dict[str, dict[str, Any]] = {}


def _save_recording(room: str, call: dict[str, Any]) -> Path | None:
    """Keep the call's audio. The recorder writes into a temporary session folder
    that is deleted at shutdown, so it is copied out while it still exists."""
    ctx, session = call.get("ctx"), call.get("session")
    if not (config.RECORD_CALLS and ctx and session):
        return None
    try:
        source = ctx.make_session_report(session).audio_recording_path
    except Exception:
        logger.exception("could not read the session recording")
        return None
    if not source or not Path(source).exists() or Path(source).stat().st_size == 0:
        return None
    config.RECORDINGS_DIR.mkdir(exist_ok=True)
    target = config.RECORDINGS_DIR / f"{room}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.ogg"
    shutil.copyfile(source, target)
    return target


async def _analyze_call(room: str, call: dict[str, Any]) -> None:
    """Save the transcript and the post-call analysis next to it. Never raises."""
    agent, session, call_log = call["agent"], call["session"], call["log"]
    transcript = _save_transcript_for(room, agent, session)
    recording = _save_recording(room, call)
    if recording:
        call_log.event("recording_saved", path=str(recording), bytes=recording.stat().st_size)
    try:
        rows = [json.loads(line) for line in call_log.path.read_text().splitlines() if line.strip()]
        history = session.history.to_dict()["items"] if session else []
        record = post_call.CallRecord(room=room, patient=agent.patient, history=history,
                                      tool_results=agent.tool_results, log_rows=rows,
                                      transport=call.get("transport", "webrtc"),
                                      dial_failure=call.get("dial_failure"))
        analysis = await post_call.analyze(record)
        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        path = TRANSCRIPT_DIR / f"{room}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_analysis.json"
        path.write_text(json.dumps(analysis, indent=2, default=str))
        audio = ({"recorded": True, "file": recording.name, "format": "ogg",
                  "seconds": analysis["facts"].get("duration_seconds")} if recording else None)
        # In a thread: the Opik SDK's first import took 17 s and froze the
        # agent's event loop when run here directly.
        trace_id = await asyncio.to_thread(opik_integration.send_call, record, analysis, audio=audio,
                                           files=[transcript, path, call_log.path] + ([recording] if recording else []))
        call_log.event("analysis_done", outcome=analysis["outcome"],
                       booking_successful=analysis["booking_successful"],
                       flags=analysis["flags"], error=analysis["error"], path=str(path),
                       opik_trace=trace_id)
        logger.info("call analysis: %s (booking %s) saved to %s",
                    analysis["outcome"], analysis["booking_successful"], path)
    except Exception as exc:
        logger.exception("post-call analysis failed")
        call_log.event("analysis_failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        call["analyzed"] = True


async def _retry_after_failure(patient: dict[str, Any], failure: Any, room: str,
                               log: CallLog) -> dict[str, Any]:
    """Queue another attempt when nobody took the call. Never raises."""
    reason = telephony.retry_reason(failure.status_code)
    if reason is None:
        decided = {"retried": False, "reason": None,
                   "why_not": "declined, unknown number, or an unclear status"}
    else:
        def _queue():
            with db.session() as conn:
                return callback_queue.schedule_retry(
                    conn, patient=patient, now=callback_queue.patient_now(patient),
                    reason=reason, source_room=room)

        try:
            decided = await asyncio.to_thread(_queue)
        except Exception as exc:
            logger.exception("could not queue a retry")
            decided = {"retried": False, "reason": reason, "why_not": f"{type(exc).__name__}: {exc}"}
    log.event("retry_decision", **decided)
    return decided


async def _on_session_end(ctx: JobContext) -> None:
    # Runs after the session closes and before shutdown callbacks, with a far
    # longer allowance (300 s) than they get (10 s), so analysis fits here.
    if (call := _CALLS.get(ctx.room.name)) and not call.get("analyzed"):
        await _analyze_call(ctx.room.name, call)


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=_on_session_end)
async def entrypoint(ctx: JobContext) -> None:
    try:
        patient = _select_patient(ctx)
    except LookupError as exc:
        logger.info("not calling: %s", exc)
        return
    with db.session() as conn:
        booked = db.upcoming_appointment(conn, patient["id"], scheduling.to_utc_iso(db.utc_now()))
    if booked:
        # The dispatcher already skips booked patients; this catches a stale job
        # or a booking made after it was queued. Nobody is dialled or spoken to.
        logger.info("not calling %s: already booked (%s)", patient["id"], booked["reference"])
        return
    dial_to = _phone_to_dial(ctx, patient)
    transport = _transport(ctx, dial_to)
    on_phone = transport.startswith("sip")
    logger.info("starting %s call for %s (%s)", transport, patient["name"], patient["id"])

    await ctx.connect()

    call_log = CallLog(ctx.room.name)
    call_log.event("call_start", room=ctx.room.name, patient_id=patient["id"], transport=transport)
    agent = HealthcareAgent(patient, room_name=ctx.room.name, call_log=call_log)
    call: dict[str, Any] = {"agent": agent, "session": None, "log": call_log, "transport": transport,
                            "ctx": ctx}
    _CALLS[ctx.room.name] = call

    async def _on_shutdown() -> None:
        ended = _CALLS.pop(ctx.room.name, None)
        if ended and not ended.get("analyzed") and ended.get("session"):
            # The session-end hook did not run; keep at least the transcript.
            _save_transcript_for(ctx.room.name, agent, ended["session"])
        call_log.close()

    ctx.add_shutdown_callback(_on_shutdown)

    simulate = _job_metadata(ctx).get("simulate_status")
    if dial_to:
        # Ring first and wait for an answer: starting the session now would have
        # the agent talking over the ringtone.
        started = call_log.event("dial_start", phone=_masked(dial_to), simulated=bool(simulate))
        failure = (telephony.simulated_failure(int(simulate)) if simulate
                   else await telephony.dial(ctx.api, telephony.DialRequest(
                       room=ctx.room.name, phone=dial_to, patient_id=patient["id"],
                       trunk_id=telephony.trunk_id())))
        call_log.event("dial_end", answered=failure is None, outcome=getattr(failure, "outcome", None),
                       status_code=getattr(failure, "status_code", None),
                       detail=getattr(failure, "detail", None),
                       waited_ms=round(call_log.elapsed_ms() - started, 1))
        if failure is not None:
            # Nobody spoke, so there is nothing to transcribe or judge; the
            # attempt is still recorded, analysed and sent to Opik.
            call["dial_failure"] = {"outcome": failure.outcome, "status_code": failure.status_code,
                                    "status": failure.status, "detail": failure.detail,
                                    "simulated": bool(simulate),
                                    "retry": await _retry_after_failure(patient, failure, ctx.room.name, call_log),
                                    "needs_front_desk": telephony.needs_front_desk(failure.status_code)}
            await _analyze_call(ctx.room.name, call)
            return

    if transport == "sip_inbound" or (transport == "webrtc" and not _in_console()):
        # Someone has to be there to hear the opening. A dispatched browser call
        # starts before anyone joins the room; a bridged phone call has its
        # audio leg arriving. Console mode has no one to wait for.
        participant = await ctx.wait_for_participant()
        call_log.event("participant_joined", identity=participant.identity,
                       participant_kind=str(participant.kind))

    session = _build_session(ctx.proc.userdata["vad"], patient)
    call["session"] = session
    _log_session_events(session, call_log)
    if on_phone:
        _hang_up_when_finished(ctx, session, agent, call_log)
    # Audio only: transcripts and traces already go to our own log and to Opik.
    await session.start(room=ctx.room, agent=agent,
                        record={"audio": config.RECORD_CALLS, "traces": False,
                                "logs": False, "transcript": False})
    call_log.event("session_started", recording=config.RECORD_CALLS)

    # On a phone call the callee says "hello" first, so the agent waits briefly.
    # Over WebRTC nobody does, and waiting only delays the opening.
    await _open_conversation(session, let_them_speak_first=on_phone)


if __name__ == "__main__":
    agents.cli.run_app(server)
