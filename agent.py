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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterable

from dotenv import load_dotenv
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
    llm,
    stt,
)
from livekit.plugins import deepgram, openai, silero

import booking
from call_log import CallLog, NullLog
import callback_queue
import config
import db
import scheduling
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


_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}


def _tokens(text: str) -> set[str]:
    """Lowercased word and number tokens, with number words folded to digits."""
    return {_NUMBER_WORDS.get(tok, tok).lstrip("0") or "0"
            for tok in re.findall(r"[a-z]+|\d+", text.lower())}


def slot_grounded(day: str | None, time: str | None, part_of_day: str | None, user_text: str) -> bool:
    """True if the patient's own words support the chosen day and time.

    The model once answered "Sure, let's book" by inventing "next Tuesday at
    10 AM" and booking it. A booking is refused unless the day (when one is
    named) and the hour or part of day appear in what the patient said. Times
    the system itself offered are trusted separately by the caller.
    """
    said = _tokens(user_text)
    if day == "day_after_tomorrow":
        if not {"after", "tomorrow"} <= said:
            return False
    elif day not in (None, "today") and day not in said:
        return False
    if time is not None:
        hour = int(time.split(":")[0])
        return bool({str(hour), str(hour % 12 or 12)} & said)
    if part_of_day is not None:
        return part_of_day in said
    return False


def _user_text(ctx: RunContext) -> str:
    """Everything the caller has said so far in this session."""
    parts = []
    for item in ctx.session.history.items:
        if getattr(item, "type", "message") == "message" and item.role == "user":
            parts.append(item.text_content or "")
    return " ".join(parts)


def _say_slots(slots: list[datetime], now: datetime) -> str:
    spoken = [scheduling.describe(s, now) for s in slots]
    if len(spoken) <= 1:
        return "".join(spoken)
    return ", ".join(spoken[:-1]) + " or " + spoken[-1]


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def _day_phrase(moment: datetime, now: datetime) -> str:
    days = (moment.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B')}"


def _say_ranges(slots: list[datetime], now: datetime) -> str:
    """'tomorrow: from 9:00 AM to 10:30 AM, or at 11:30 AM', grouped per day."""
    by_day: dict[Any, list[datetime]] = {}
    for slot in slots:
        by_day.setdefault(slot.date(), []).append(slot)
    parts = []
    for day_slots in by_day.values():
        runs = [f"at {_clock(a)}" if a == b else f"from {_clock(a)} to {_clock(b)}"
                for a, b in booking.free_ranges(day_slots)]
        parts.append(f"{_day_phrase(day_slots[0], now)}: {', or '.join(runs)}")
    return "; ".join(parts)


ASK_PREFERENCE = ("Do not list times yet. Ask whether they would prefer a morning or an afternoon "
                  "appointment, then call find_available_slots with their choice.")


class HealthcareAgent(Agent):
    """Voice agent for one patient, with their record baked into the prompt."""

    def __init__(self, patient: dict[str, Any], room_name: str = "", call_log: CallLog | None = None) -> None:
        super().__init__(instructions=config.build_system_prompt(patient))
        self._patient = patient
        self._room_name = room_name
        self._call_log = call_log or NullLog()
        self._llm_turn = 0
        self._tts_turn = 0
        self._tool_seq = 0
        # Slots this call's tools have offered. Booking one of these after the
        # patient accepts is not a fabrication, even if they only said "yes".
        self._offered_slots: set[str] = set()
        # Tool results are mirrored here so post-call analysis and the Opik
        # trace can read what actually happened, not just what was said.
        self.tool_results: list[dict[str, Any]] = []

    @property
    def patient(self) -> dict[str, Any]:
        return self._patient

    def _remember_offered(self, slots: list[datetime]) -> None:
        self._offered_slots.update(scheduling.to_utc_iso(s) for s in slots)

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

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings):
        log = self._call_log
        self._llm_turn += 1
        turn = self._llm_turn
        last = chat_ctx.items[-1] if chat_ctx.items else None
        sent = log.event("llm_request", turn=turn, items=len(chat_ctx.items), tools=len(tools),
                         last_item_type=getattr(last, "type", None),
                         last_item=_item_text(last))
        first: float | None = None
        text: list[str] = []
        tool_calls: list[dict[str, str]] = []
        usage = None
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            delta = chunk if isinstance(chunk, str) else getattr(chunk, "delta", None)
            content = chunk if isinstance(chunk, str) else getattr(delta, "content", None)
            calls = [] if isinstance(chunk, str) else getattr(delta, "tool_calls", None) or []
            if first is None and (content or calls):
                first = log.event("llm_first_token", turn=turn, ttft_ms=round(log.elapsed_ms() - sent, 1))
            if content:
                text.append(content)
            for call in calls:
                tool_calls.append({"name": call.name, "arguments": call.arguments})
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            yield chunk
        log.event("llm_response", turn=turn, text="".join(text), tool_calls=tool_calls,
                  total_ms=round(log.elapsed_ms() - sent, 1),
                  prompt_tokens=getattr(usage, "prompt_tokens", None),
                  completion_tokens=getattr(usage, "completion_tokens", None))

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

    @function_tool
    async def find_available_slots(
        self,
        ctx: RunContext,
        day: Day | None = None,
        part_of_day: PartOfDay | None = None,
    ) -> str:
        """Look up free appointment times with the doctor.

        Call only once the patient has said whether they prefer morning or
        afternoon. Never suggest a time that this tool, or book_appointment,
        did not return.

        Args:
            day: The day to check. Omit to find the first day with free times.
            part_of_day: The patient's preference: morning or afternoon.
        """
        now = booking.clinic_now()
        call_id = self._tool_started("find_available_slots", day=day, part_of_day=part_of_day)

        if part_of_day is None:
            self._tool_finished(call_id, "find_available_slots", ASK_PREFERENCE)
            return ASK_PREFERENCE

        def _lookup():
            with db.session() as conn:
                slots, reason = booking.available_slots(conn, now=now, day=day, part_of_day=part_of_day,
                                                        limit=None)
                if not slots:
                    anchor = scheduling.requested_datetime(now, day=day or "today", part_of_day=part_of_day) or now
                    return [], reason, booking.nearest_free_slots(conn, requested=anchor, now=now)
                return slots, None, []

        slots, reason, nearest = await asyncio.to_thread(_lookup)
        self._remember_offered(slots or nearest)
        self._log("find_available_slots", True, day=day, part_of_day=part_of_day,
                  free=slots, nearest=nearest, reason=reason)
        if slots:
            result = (f"Free {part_of_day} times, 30-minute appointments starting on the hour or half "
                      f"hour, {_say_ranges(slots, now)}. Tell them these ranges in one sentence and "
                      "ask what time they would like. Do not read out every slot.")
        else:
            result = (f"Nothing then, because {reason}. Nearest free times: {_say_slots(nearest, now)}. "
                      "Offer these instead.")
        self._tool_finished(call_id, "find_available_slots", result)
        return result

    @function_tool
    async def book_appointment(
        self,
        ctx: RunContext,
        day: Day | None = None,
        time: str | None = None,
        part_of_day: PartOfDay | None = None,
        replace_existing: bool = False,
        notes: str = "",
    ) -> str:
        """Book the patient's follow-up consultation in a specific slot.

        Call only after the patient has chosen a day and time in their own
        words, or has accepted a time you offered from a tool. If they named
        only a day or part of the day, still call this: it returns free times.

        Args:
            day: The chosen day. "Tomorrow" is tomorrow, "on Tuesday" is tuesday.
            time: The chosen clock time in 24-hour HH:MM. "3pm" is 15:00.
            part_of_day: When they named only morning, afternoon or evening.
            replace_existing: True only after the patient agreed to move an
                appointment they already have.
            notes: Anything the clinic should know.
        """
        call_id = self._tool_started("book_appointment", day=day, time=time, part_of_day=part_of_day,
                                     replace_existing=replace_existing, notes=notes)
        result = await self._book(ctx, day, time, part_of_day, replace_existing, notes)
        self._tool_finished(call_id, "book_appointment", result)
        return result

    async def _book(self, ctx: RunContext, day: Day | None, time: str | None,
                    part_of_day: PartOfDay | None, replace_existing: bool, notes: str) -> str:
        now = booking.clinic_now()

        if time is None and part_of_day is None:
            return f"Not booked yet: they have not chosen a time. {ASK_PREFERENCE}"

        if time is not None:
            try:
                requested = scheduling.requested_datetime(now, day=day, time=time)
            except SchedulingError as exc:
                return f"Not booked: {exc}. Ask the patient to repeat the time."
            offered = scheduling.to_utc_iso(requested) in self._offered_slots
            if not offered and not slot_grounded(day, time, part_of_day, _user_text(ctx)):
                logger.warning("refused ungrounded booking: day=%r time=%r", day, time)
                self._log("book_appointment", False, error="day or time not stated by the patient",
                          day=day, time=time)
                return ("Not booked: the patient has not said that day and time. Ask which "
                        "day and time suit them, or offer times from find_available_slots.")

        def _book():
            with db.session() as conn:
                return booking.request_appointment(
                    conn, patient=self._patient, now=now, day=day, time=time,
                    part_of_day=part_of_day, notes=notes,
                    replace_existing=replace_existing, source_room=self._room_name)

        out = await asyncio.to_thread(_book)
        self._remember_offered(out.alternatives)
        self._log("book_appointment", out.status == "booked", status=out.status, reason=out.reason,
                  requested=out.requested, record=out.record, alternatives=out.alternatives,
                  existing=out.existing)

        if out.status == "booked":
            when = scheduling.describe(out.requested, now)
            logger.info("booked %s at %s", out.record["reference"], out.record["slot_start_utc"])
            return (f"Booked for {when} with {config.DOCTOR['name']}. Reference "
                    f"{out.record['reference']}. Read the time and reference back to the patient.")
        if out.status == "needs_time":
            return (f"Not booked yet: they have not chosen a time. Free times, "
                    f"{_say_ranges(out.alternatives, now)}. Tell them these ranges in one sentence "
                    "and ask what time they would like.")
        if out.reason.startswith("appointments start on the hour"):
            return (f"Not booked: {out.reason}. Ask whether {_say_slots(out.alternatives, now)} "
                    "would work instead.")
        if out.status == "has_existing":
            existing_at = scheduling.describe(scheduling.from_iso(out.existing["slot_start_utc"]), now)
            return (f"Not booked: they already have an appointment {existing_at}. Ask whether they "
                    "want to move it to the new time. If yes, call book_appointment again with "
                    "replace_existing true.")
        return (f"Not booked: {out.reason}. Nearest free times: {_say_slots(out.alternatives, now)}. "
                "Offer these and ask which they prefer.")

    @function_tool
    async def request_callback(
        self,
        ctx: RunContext,
        phrase: str,
        requested_by: str = "",
        in_minutes: int | None = None,
        day: Day | None = None,
        time: str | None = None,
        part_of_day: PartOfDay | None = None,
        no_time_given: bool = False,
    ) -> str:
        """Schedule a call back to the patient at a later time.

        Use when whoever answered, or the patient, asks to be called later. This
        does not book an appointment and must not reveal anything medical. Fill
        only the fields their words support; code computes the actual time.

        Args:
            phrase: Their exact words about timing, for example "in a couple of hours".
            requested_by: Who asked, for example "patient" or "her brother".
            in_minutes: For relative times. "In an hour" is 60.
            day: For a named day. "Tomorrow" is tomorrow, "on Monday" is monday.
            time: A clock time in 24-hour HH:MM. "At 5" in conversation is 17:00.
            part_of_day: When they name a part of the day but no clock time.
            no_time_given: True only if they want a callback but have no preference.
        """
        call_id = self._tool_started("request_callback", phrase=phrase, requested_by=requested_by,
                                     in_minutes=in_minutes, day=day, time=time,
                                     part_of_day=part_of_day, no_time_given=no_time_given)
        result = await self._callback(phrase, requested_by, in_minutes, day, time, part_of_day, no_time_given)
        self._tool_finished(call_id, "request_callback", result)
        return result

    async def _callback(self, phrase: str, requested_by: str, in_minutes: int | None, day: Day | None,
                        time: str | None, part_of_day: PartOfDay | None, no_time_given: bool) -> str:
        now = callback_queue.patient_now(self._patient)

        def _schedule():
            with db.session() as conn:
                return callback_queue.schedule(
                    conn, patient=self._patient, now=now, phrase=phrase, requested_by=requested_by,
                    in_minutes=in_minutes, day=day, time=time, part_of_day=part_of_day,
                    no_time_given=no_time_given, source_room=self._room_name)

        try:
            record = await asyncio.to_thread(_schedule)
        except SchedulingError as exc:
            logger.info("callback not scheduled: %s", exc)
            self._log("request_callback", False, error=str(exc))
            return f"Not scheduled: {exc}."

        when = scheduling.describe(record["scheduled"], now)
        logger.info("callback %s queued for %s", record["reference"], record["due_utc"])
        self._log("request_callback", True, result=record)
        if record["moved_because"]:
            asked = scheduling.describe(record["requested"], now)
            return (f"Scheduled for {when}, not {asked}, because {' and '.join(record['moved_because'])}. "
                    "Tell them that time and the reason briefly, and ask if it works.")
        return f"Scheduled for {when}. Confirm that time with them, then close politely."


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
        return db.list_callable_patients(conn)[0]


def _name_keyterms(patient: dict[str, Any]) -> list[str]:
    """Names worth boosting in speech recognition for this call."""
    parts = patient["name"].split()
    return [patient["name"], *parts, config.CLINIC_NAME, config.AGENT_DISPLAY_NAME]


def _build_session(vad: silero.VAD, patient: dict[str, Any]) -> AgentSession:
    """Assemble the speech pipeline from the providers chosen in config."""
    return AgentSession(
        stt=deepgram.STT(
            model=config.STT_MODEL,
            language="en-US",
            # Bias recognition toward the names this call depends on. Without it,
            # "Priya Sharma" was heard as "Rias Roma" and the agent wrongly
            # decided a stranger had answered.
            keyterm=_name_keyterms(patient),
        ),
        llm=openai.LLM(
            model=config.LLM_MODEL,
            base_url=config.GROQ_BASE_URL,
            api_key=os.environ["GROQ_API_KEY"],
            # Measured on our scenarios: equal reply quality, 77% fewer reasoning
            # tokens than Groq's default. Reasoning tokens count against the
            # free tier's 8,000 tokens-per-minute cap.
            reasoning_effort="low",
        ),
        tts=deepgram.TTS(model=config.TTS_MODEL),
        vad=vad,
    )


async def _open_conversation(session: AgentSession, *, let_them_speak_first: bool) -> None:
    """Deliver the opening line.

    On a real outbound call the callee speaks first, so greeting immediately
    means talking over their "hello". We wait briefly for them, then open
    regardless so a silent pickup is not met with silence.
    """
    if not let_them_speak_first:
        await session.generate_reply(instructions=config.GREETING_INSTRUCTIONS)
        return

    heard = asyncio.Event()

    def _on_transcript(_ev: Any) -> None:
        heard.set()

    session.on("user_input_transcribed", _on_transcript)
    try:
        await asyncio.wait_for(heard.wait(), GREETING_FALLBACK_SECONDS)
        logger.info("callee spoke first; responding")
    except asyncio.TimeoutError:
        logger.info("no speech within %.1fs; opening anyway", GREETING_FALLBACK_SECONDS)
    finally:
        session.off("user_input_transcribed", _on_transcript)

    await session.generate_reply(instructions=config.GREETING_INSTRUCTIONS)


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


def _save_transcript(ctx: JobContext, agent: HealthcareAgent, session: AgentSession) -> Path:
    """Persist the conversation so later phases have something to analyze."""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = TRANSCRIPT_DIR / f"{ctx.room.name}_{stamp}.json"

    payload = {
        "room": ctx.room.name,
        "patient": agent.patient,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "history": session.history.to_dict(),
        "tool_results": agent.tool_results,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("transcript saved to %s", path)
    return path


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    patient = _select_patient(ctx)
    logger.info("starting call for %s (%s)", patient["name"], patient["id"])

    await ctx.connect()

    call_log = CallLog(ctx.room.name)
    call_log.event("call_start", room=ctx.room.name, patient_id=patient["id"])
    agent = HealthcareAgent(patient, room_name=ctx.room.name, call_log=call_log)
    session = _build_session(ctx.proc.userdata["vad"], patient)

    _log_session_events(session, call_log)
    await session.start(room=ctx.room, agent=agent)
    call_log.event("session_started")

    # Phase 7 turns this on for SIP calls. Over WebRTC nobody says hello first,
    # so waiting would only add a pause before the agent speaks.
    await _open_conversation(session, let_them_speak_first=False)

    async def _on_shutdown() -> None:
        _save_transcript(ctx, agent, session)
        call_log.close()

    ctx.add_shutdown_callback(_on_shutdown)


if __name__ == "__main__":
    agents.cli.run_app(server)
