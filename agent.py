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
import post_call
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
    "thirty": "30",
}


def _tokens(text: str) -> set[str]:
    """Lowercased word and number tokens, with number words folded to digits."""
    return {_NUMBER_WORDS.get(tok, tok).lstrip("0") or "0"
            for tok in re.findall(r"[a-z]+|\d+", text.lower())}


def day_grounded(day: str | None, user_text: str) -> bool:
    """True if the patient's words name the day (or no particular day is implied)."""
    said = _tokens(user_text)
    if day == "day_after_tomorrow":
        return {"after", "tomorrow"} <= said
    if day == "tomorrow" and TOMORROW_PHRASES.search(user_text):
        return True
    return day in (None, "today") or day in said


# Ways of saying tomorrow without the word. "Next day" is left out: after an
# offer it means the day after the offered one.
TOMORROW_PHRASES = re.compile(r"\b(next|tomorrow) (morning|afternoon|evening)\b|\bfirst thing\b", re.I)


def slot_grounded(day: str | None, time: str | None, part_of_day: str | None, user_text: str) -> bool:
    """True if the patient's own words support the chosen day and time.

    The model once answered "Sure, let's book" by inventing "next Tuesday at
    10 AM" and booking it. A booking is refused unless the day (when one is
    named) and the hour or part of day appear in what the patient said. Times
    the system itself offered are trusted separately by the caller.
    """
    said = _tokens(user_text)
    if not day_grounded(day, user_text):
        return False
    if time is not None:
        hour, minute = (int(x) for x in time.split(":")[:2])
        if not {str(hour), str(hour % 12 or 12)} & said:
            return False
        # The hour alone is not enough: "10 AM" must not ground 10:30, which the
        # model once booked after inventing the patient's yes.
        return minute == 0 or str(minute) in said or (minute == 30 and "half" in said)
    if part_of_day is not None:
        return part_of_day in said
    return False


def _user_messages(ctx: RunContext) -> list[str]:
    """What the caller has said so far, one entry per committed turn."""
    return [item.text_content or "" for item in ctx.session.history.items
            if getattr(item, "type", "message") == "message" and item.role == "user"]


def _user_text(ctx: RunContext) -> str:
    """Everything the caller has said so far in this session."""
    parts = []
    for item in ctx.session.history.items:
        if getattr(item, "type", "message") == "message" and item.role == "user":
            parts.append(item.text_content or "")
    return " ".join(parts)


def normalize_day(value: Any) -> Any:
    """Map near-miss day values onto the Day vocabulary before validation.

    The schema lists the exact values, yet in rehearsals the model sent
    "Friday", "day after tomorrow" and "2026-09-16". Each was rejected and
    retried, costing a whole extra LLM request. Anything still unrecognised
    falls through and fails validation as before.
    """
    if not isinstance(value, str):
        return value
    text = re.sub(r"^(this|next|on)\s+", "", value.strip().lower())
    text = re.sub(r"[\s-]+", "_", text)
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


DayArg = Annotated[Day, BeforeValidator(normalize_day)]
# Appointment tools also accept "day_after_offered": the model recognises "the
# next day" and code does the date maths. In rehearsal the model searched the
# offered day again and then tried to book today when it did that maths itself.
ApptDay = Literal[
    "today", "tomorrow", "day_after_tomorrow",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "day_after_offered",
]
ApptDayArg = Annotated[ApptDay, BeforeValidator(normalize_day)]


def normalize_part_of_day(value: Any) -> Any:
    """Anything that is not a part of the day becomes empty. The model once sent
    part_of_day "driving"; rejecting it cost a whole retry request."""
    if isinstance(value, str) and value.strip().lower() in ("morning", "afternoon", "evening"):
        return value.strip().lower()
    return None


PartOfDayArg = Annotated[PartOfDay | None, BeforeValidator(normalize_part_of_day)]


EMPTY_REPLY_ATTEMPTS = 2
EMPTY_REPLY_FALLBACK = "Sorry, could you say that again?"
# How much text after a question to hold back before deciding it is safe.
_HOLD_CHARS = 16
_INVENTED_REPLY = re.compile(r"^\s*(yes|yeah|yep|no(?!\s+(problem|worries|pressure|rush))|nope|sure|okay|ok|that works|sounds good)\b", re.I)


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
            elif ch == "\u2026":
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


class ReplyCutter:
    """Removes a patient reply the model wrote into its own turn.

    In rehearsal the model asked "Would you like to confirm that appointment?"
    and carried straight on with "Yes, that works for me.", which the voice would
    have spoken as if the patient had said it. After each question mark the next
    few characters are held back; if they read like an answer, everything from
    there on is dropped. Anything else is released unchanged.
    """

    def __init__(self) -> None:
        self._held = ""
        self._holding = False
        self.cut = False
        self.removed = ""

    def feed(self, piece: str) -> str:
        if self.cut:
            self.removed += piece
            return ""
        out = ""
        for ch in piece:
            if self.cut:
                self.removed += ch
            elif self._holding:
                self._held += ch
                if len(self._held.strip()) >= _HOLD_CHARS:
                    out += self._release()
            else:
                out += ch
                if ch == "?":
                    self._holding = True
        return out

    @property
    def holding(self) -> bool:
        return self._holding and not self.cut

    def finish(self) -> str:
        """Decide on whatever is still held. Also called as soon as a tool call
        arrives, so the call is never let through before its text is judged."""
        out = ""
        while self.holding:
            out += self._release()
        return out

    def _release(self) -> str:
        held, self._held, self._holding = self._held, "", False
        if _INVENTED_REPLY.match(held):
            self.cut, self.removed = True, held
            return ""
        # Not an answer: run it through again, so a later question starts a new hold.
        return self.feed(held)


_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d\d))?\s*([ap])\.?\s?m\b\.?", re.I)
_CLOCK_24 = re.compile(r"\b(\d{1,2}):(\d\d)\b")
_SENTENCE_END = re.compile(r"[.?!](?=\s)")
# Asks rather than promises: "let me check" was said twice in an eval with nothing
# checked, while a question hands the turn back to the caller.
UNGROUNDED_TIME_FALLBACK = "What day and time would work best?"
DIAGNOSIS_FALLBACK = "The doctor will go through exactly what these results mean for you."
_DIAGNOSIS = re.compile(
    r"\b(you have|you'?re|suggests?|indicates?|means? (that )?you|consistent with|signs? of|confirms?|shows?)\b"
    r"[^.?!]{0,40}\b(pre-?diabet\w*|diabet\w*|insulin resistance|hyperglyc\w*)", re.I)


def diagnoses_condition(sentence: str) -> bool:
    """A sentence that tells the patient what condition their results mean. In an
    eval the agent said 8.2% "is a sign of diabetes". Denials and deferrals to the
    doctor ("doesn't mean you have diabetes", "only your doctor can say") pass."""
    m = _DIAGNOSIS.search(sentence)
    if not m:
        return False
    window = sentence[max(0, m.start() - 30):m.end()]
    return not re.search(r"\b(not|cannot|can'?t|only|doctor)\b|n't", window, re.I)


def known_times(chat_ctx: llm.ChatContext) -> set[str]:
    """Clock times, as "H:MM" in 12-hour form, that the agent may say: any a tool
    returned, the patient said, the agent already said, or the clinic's hours."""
    times = {f"{config.CLINIC_OPEN_HOUR % 12 or 12}:00", f"{config.CLINIC_CLOSE_HOUR % 12 or 12}:00"}
    for item in chat_ctx.items:
        kind = getattr(item, "type", None)
        if kind == "function_call_output":
            text = str(getattr(item, "output", ""))
        elif kind == "message" and item.role in ("user", "assistant"):
            text = item.text_content or ""
        else:
            continue
        for m in list(_CLOCK.finditer(text)) + list(_CLOCK_24.finditer(text)):
            hour, minute = int(m.group(1)), m.group(2) or "00"
            times.add(f"{hour % 12 or 12}:{minute}")
        if kind == "message" and item.role == "user":
            # "at 3" or "at five": a bare hour the patient said grounds H:00.
            times |= {f"{int(t) % 12 or 12}:00" for t in _tokens(text) if t.isdigit() and 1 <= int(t) <= 23}
    return times


_WEEKDAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)
# The clinic's own week, which the agent may always state.
_CLINIC_WEEK = re.compile(r"\bmonday\s+(to|through|-|and)\s+saturday\b|\b(closed|open)\b[^.?!]{0,20}\bsundays?\b"
                          r"|\bsundays?\b[^.?!]{0,20}\bclosed\b", re.I)


def known_days(chat_ctx: llm.ChatContext) -> set[str]:
    """Weekday names the agent may say: any a tool returned, the patient said, the
    agent already said, plus today's and tomorrow's names."""
    now = booking.clinic_now()
    days = {scheduling.WEEKDAYS[now.weekday()], scheduling.WEEKDAYS[(now.weekday() + 1) % 7]}
    for item in chat_ctx.items:
        kind = getattr(item, "type", None)
        if kind == "function_call_output":
            text = str(getattr(item, "output", ""))
        elif kind == "message" and item.role in ("user", "assistant"):
            text = item.text_content or ""
        else:
            continue
        days |= {m.group(1).lower() for m in _WEEKDAY.finditer(text)}
    return days


class TimeGate:
    """Holds each sentence until it is complete and releases it only if every clock
    time in it is known. In rehearsal the agent told a patient "around 10:45 AM"
    when the tool had scheduled 9 AM, and offered 10:00 and 2:30 without checking,
    with 10:00 already taken. The first unknown time blocks the rest of the reply.
    """

    def __init__(self, allowed: set[str], allowed_days: set[str] | None = None) -> None:
        self._allowed = allowed
        self._allowed_days = allowed_days
        self._pending = ""
        self.released = ""
        self.blocked: str | None = None   # the sentence that was stopped
        self.reason = ""                  # "time" or "diagnosis"

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
        if not all(f"{int(m.group(1)) % 12 or 12}:{m.group(2) or '00'}" in self._allowed
                   for m in _CLOCK.finditer(sentence)):
            self.reason = "time"
            return False
        if self._allowed_days is not None:
            # In an eval the agent confirmed "a callback on Monday morning" after the
            # caller said "next morning" on a Tuesday.
            spoken = {m.group(1).lower() for m in _WEEKDAY.finditer(_CLINIC_WEEK.sub(" ", sentence))}
            if spoken - self._allowed_days:
                self.reason = "day"
                return False
        if diagnoses_condition(sentence):
            self.reason = "diagnosis"
            return False
        return True


# A clock time in the patient's words: "10 AM", "10:30", "at 3".
_SPOKEN_CLOCK = re.compile(r"\b\d{1,2}(:\d\d)?\s*([ap]\.?\s?m)\b|\b\d{1,2}:\d\d\b|\bat \d{1,2}\b", re.I)

# Everyday words that name a part of the day without the word itself.
PART_OF_DAY_SYNONYMS = {"evening": {"tonight", "night"}, "afternoon": {"lunch", "noon"}}

# Words showing the patient has said something about when, which a search for
# the earliest slot needs. "Yes, I'd like to see the doctor" has none of them.
PREFERENCE_WORDS = re.compile(
    r"\b(earliest|soonest|soon|asap|first|any|anytime|whenever|pick|choose|decide|up to you|"
    r"(doesn'?t|does not) matter|no preference|available|free|open|next|after|instead|another|"
    r"other|different|later|earlier|week|today|tomorrow|morning|afternoon|evening|noon|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d+)\b", re.I)


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
# are withheld. request_callback is always sent: a busy patient or someone else
# can ask for a callback before identity is confirmed.
BOOKING_TOOLS = frozenset({"book_appointment", "find_earliest_slot"})
_BOOKING_WORDS = re.compile(r"\b(book\w*|appointment|consult\w*|earliest)\b", re.I)


def booking_unlocked(chat_ctx: llm.ChatContext, patient: dict[str, Any]) -> bool:
    """True once the call has reached the point where booking makes sense.

    Either the agent has already shared the results, which it only does with the
    confirmed patient, or the caller has asked about booking in their own words.
    Worked out from the history on each request, so there is no state to drift.
    """
    if results_shared(chat_ctx.items, patient):
        return True
    return any(getattr(item, "type", None) == "message" and item.role == "user"
               and _BOOKING_WORDS.search(item.text_content or "") for item in chat_ctx.items)


def results_shared(items: list[Any], patient: dict[str, Any]) -> bool:
    """True if the agent has already said either result value aloud in this call."""
    values = (str(patient["hba1c"]), str(patient["blood_glucose"]))
    return any(getattr(item, "type", None) == "message" and item.role == "assistant"
               and any(v in (item.text_content or "") for v in values) for item in items)


def _offer(slot: datetime, start: datetime, now: datetime, day: str | None) -> str:
    """Tool reply offering exactly one slot. It always says nothing is booked yet:
    the model once offered a time and announced it as booked in the same reply."""
    when = scheduling.describe(slot, now)
    if slot == start:
        return (f"{when} is free. NOT booked yet. If they asked for exactly this time, call "
                "book_appointment now. Otherwise offer it and wait for their yes.")
    if day is not None and slot.date() != start.date():
        return (f"Nothing free on that day. The earliest after it is {when}. NOT booked yet. "
                "Offer only this time, ask if it works, and wait for their answer.")
    return (f"The earliest free time is {when}. NOT booked yet. Offer only this time, ask if it "
            "works, and wait for their answer.")


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
        # Slots this call's tools have offered, mapped to how many patient turns
        # existed at the time. Booking one is only trusted once the patient has
        # spoken since the offer: the model once listed free times and, in the
        # same breath, booked one of them before the patient could answer.
        self._offered_slots: dict[str, int] = {}
        self._last_offered: datetime | None = None
        # True when the last offer was itself the answer to "the next day", so a
        # repeated day_after_offered refers to that offer rather than adding a day.
        self._last_offer_was_day_after = False
        # Patient-turn count when request_callback last refused for lack of a time.
        self._callback_refused_at: int | None = None
        # Tool results are mirrored here so post-call analysis and the Opik
        # trace can read what actually happened, not just what was said.
        self.tool_results: list[dict[str, Any]] = []

    @property
    def patient(self) -> dict[str, Any]:
        return self._patient

    def _remember_offered(self, ctx: RunContext, slots: list[datetime], *, from_day_after: bool = False) -> None:
        turns = len(_user_messages(ctx))
        for slot in slots:
            self._offered_slots.setdefault(scheduling.to_utc_iso(slot), turns)
            self._last_offered = slot
            self._last_offer_was_day_after = from_day_after

    def _patient_answered_offer(self, ctx: RunContext, slot: datetime) -> bool:
        offered_at = self._offered_slots.get(scheduling.to_utc_iso(slot))
        return offered_at is not None and len(_user_messages(ctx)) > offered_at

    def _offered_clock_answered(self, ctx: RunContext, slot: datetime) -> bool:
        """The same clock time was offered on another day and the patient has replied since.

        Covers "Friday at that time instead": the day is in their words, the
        time is the one we offered.
        """
        turns = len(_user_messages(ctx))
        clock = slot.strftime("%H:%M")
        return any(scheduling.from_iso(iso).astimezone(slot.tzinfo).strftime("%H:%M") == clock and turns > at
                   for iso, at in self._offered_slots.items())

    def _day_after_offered(self, day: str | None, time: str | None) -> tuple[str | None, str | None, str | None]:
        """Turn day_after_offered into a concrete day and, if missing, the offered time.

        Returns (day, time, problem). The offered time is kept, so "the next day"
        means the same time one day later.
        """
        if day != "day_after_offered":
            return day, time, None
        if self._last_offered is None:
            return None, time, "no time has been offered yet"
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

    def _booking_trusted(self, ctx: RunContext, day: str | None, time: str,
                         requested: datetime, relative: bool) -> bool:
        """The patient chose this slot: said it, accepted it, or asked for an offered
        time on another day. The model's own suggestion is never enough.

        A day_after_offered slot is trusted only once it has itself been offered
        and answered. In rehearsal the model sent day_after_offered for "yes, that
        works" to tomorrow, and Thursday was booked; the patient must hear the
        actual day before it is booked.
        """
        if self._patient_answered_offer(ctx, requested):
            return True
        if relative:
            return False
        said = _user_text(ctx)
        if day_grounded(day, said) and self._offered_clock_answered(ctx, requested):
            return True
        return slot_grounded(day, time, None, said)

    @staticmethod
    def _callback_values_unsaid(ctx: RunContext, day: str | None, time: str | None,
                                part_of_day: str | None) -> list[str]:
        """Callback fields the caller's own words do not support, the same check a
        booking gets. in_minutes is exempt: "a couple of hours" has no number to find."""
        said = _user_text(ctx)
        tokens = _tokens(said)
        unsaid = []
        if day == "today":
            # day_grounded treats "today" as the default; for a callback it must be said.
            if not re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", said, re.I):
                unsaid.append("day")
        elif day is not None and not day_grounded(day, said):
            unsaid.append("day")
        if time is not None and not slot_grounded(None, time, None, said):
            unsaid.append("time")
        # With a clock time the caller said, a part of day the model added is redundant.
        if (part_of_day is not None and "time" not in unsaid and time is None
                and not ({part_of_day} | PART_OF_DAY_SYNONYMS.get(part_of_day, set())) & tokens):
            unsaid.append("part of the day")
        return unsaid

    def _callback_time_asked(self, ctx: RunContext) -> bool:
        """Someone was asked when to call back, and has answered since.

        Checked by turn order, not by their words: either this tool already
        refused for lack of a time and they have spoken since, or the agent's
        message just before their latest reply asked about timing.
        """
        messages = _user_messages(ctx)
        if self._callback_refused_at is not None and len(messages) > self._callback_refused_at:
            return True
        items = [i for i in ctx.session.history.items if getattr(i, "type", None) == "message"]
        seen_user = False
        for item in reversed(items):
            if item.role == "user":
                seen_user = True
            elif item.role == "assistant" and seen_user:
                text = item.text_content or ""
                return "?" in text and bool(re.search(r"\b(when|time)\b", text, re.I))
        return False

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
        if not booking_unlocked(chat_ctx, self._patient):
            tools = [t for t in tools if getattr(getattr(t, "info", None), "name", None) not in BOOKING_TOOLS]
        if tool_results_this_turn(chat_ctx) >= MAX_TOOL_RESULTS_PER_TURN:
            # The framework stops after a few tool rounds without saying anything;
            # in an eval the model made four calls and the patient heard silence.
            # With no tools on offer, the model has to speak.
            log.event("guard_tools_withheld", turn=turn, results=tool_results_this_turn(chat_ctx))
            tools = []
        last = chat_ctx.items[-1] if chat_ctx.items else None
        request_ctx = chat_ctx
        fallback = EMPTY_REPLY_FALLBACK
        for attempt in range(1, EMPTY_REPLY_ATTEMPTS + 1):
            sent = log.event("llm_request", turn=turn, attempt=attempt, items=len(request_ctx.items), tools=len(tools),
                             tool_names=[getattr(getattr(t, "info", None), "name", None) for t in tools],
                             last_item_type=getattr(last, "type", None),
                             last_item=_item_text(last))
            first: float | None = None
            text: list[str] = []
            tool_calls: list[dict[str, str]] = []
            usage = None
            cutter = ReplyCutter()
            dots = DotCollapser()
            gate = TimeGate(known_times(chat_ctx), known_days(chat_ctx))

            def _filter(piece: str) -> str:
                return gate.feed(cutter.feed(dots.feed(piece)))

            async for chunk in Agent.default.llm_node(self, request_ctx, tools, model_settings):
                delta = None if isinstance(chunk, str) else getattr(chunk, "delta", None)
                content = chunk if isinstance(chunk, str) else getattr(delta, "content", None)
                calls = getattr(delta, "tool_calls", None) or []
                if first is None and (content or calls):
                    first = log.event("llm_first_token", turn=turn, ttft_ms=round(log.elapsed_ms() - sent, 1))
                if content:
                    # Text goes through the filters as plain strings, which llm_node
                    # may yield; tool calls and usage keep their original chunk.
                    safe = _filter(content)
                    if safe:
                        text.append(safe)
                        yield safe
                if calls and cutter.holding:
                    # Judge the held text before letting a tool call through.
                    safe = gate.feed(cutter.finish())
                    if safe:
                        text.append(safe)
                        yield safe
                if calls and cutter.cut:
                    # Anything after an invented patient reply rests on that reply.
                    log.event("guard_dropped_tool_call", turn=turn,
                              calls=[{"name": c.name, "arguments": c.arguments} for c in calls])
                    calls = []
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
            tail = gate.feed(cutter.feed(dots.finish()) + cutter.finish()) + gate.finish()
            if tail:
                text.append(tail)
                yield tail
            if cutter.cut:
                log.event("guard_cut_invented_reply", turn=turn, removed=cutter.removed)
            if gate.blocked is not None:
                kind = {"time": "guard_ungrounded_time", "day": "guard_ungrounded_day"}.get(gate.reason, "guard_diagnosis")
                log.event(kind, turn=turn, sentence=gate.blocked, allowed=sorted(gate._allowed))
            log.event("llm_response", turn=turn, attempt=attempt, text="".join(text), tool_calls=tool_calls,
                      total_ms=round(log.elapsed_ms() - sent, 1),
                      prompt_tokens=getattr(usage, "prompt_tokens", None),
                      completion_tokens=getattr(usage, "completion_tokens", None))
            if tool_calls:
                return
            if gate.blocked is not None:
                spare = UNGROUNDED_TIME_FALLBACK if gate.reason in ("time", "day") else DIAGNOSIS_FALLBACK
                if "".join(text).strip():
                    # Part of the reply was already spoken; do not restart it.
                    yield " " + spare
                    return
                # Nothing spoken yet: ask again, telling the model what went wrong.
                request_ctx = chat_ctx.copy()
                if gate.reason in ("time", "day"):
                    latest = next((str(i.output) for i in reversed(chat_ctx.items)
                                   if getattr(i, "type", None) == "function_call_output"), None)
                    note = (f'Your reply "{gate.blocked.strip()}" named a {gate.reason} no tool returned. Only say '
                            "days and times a tool returned in this call or the patient said."
                            + (f' The latest tool result was: "{latest}"' if latest else " Call a tool if you need a time."))
                else:
                    note = (f'Your reply "{gate.blocked.strip()}" told the patient what condition their results '
                            "mean. Do not diagnose. Say the numbers are above the usual range and the doctor will "
                            "explain what they mean.")
                request_ctx.add_message(role="system", content=note)
                fallback = spare
                continue
            if "".join(text).strip():
                return
            # gpt-oss occasionally returns nothing at all. On a phone that is dead
            # air, so ask again, and if it is still empty say something ourselves.
            log.event("guard_empty_reply", turn=turn, attempt=attempt)
        yield fallback

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
    async def find_earliest_slot(
        self,
        ctx: RunContext,
        day: ApptDayArg | None = None,
        time: str | None = None,
        part_of_day: PartOfDayArg = None,
    ) -> str:
        """Find the earliest free appointment at or after a point in time.

        Use when the patient has no preference, names only a day or part of the
        day, or turns down an offered time. Leave every field empty for the
        earliest overall.

        Args:
            day: Search from this day. "day_after_offered" only when they turn down the offered day and want the following one; never to accept an offer.
            time: Search from this 24-hour HH:MM time.
            part_of_day: Search from the start of morning or afternoon.
        """
        call_id = self._tool_started("find_earliest_slot", day=day, time=time, part_of_day=part_of_day)
        result = await self._earliest(ctx, day, time, part_of_day)
        self._tool_finished(call_id, "find_earliest_slot", result)
        return result

    async def _earliest(self, ctx: RunContext, day: str | None, time: str | None,
                        part_of_day: PartOfDay | None) -> str:
        relative = day == "day_after_offered"
        day, time, problem = self._day_after_offered(day, time)
        if problem:
            return f"Could not search: {problem}. Ask which day they mean."
        messages = _user_messages(ctx)
        if not relative and time is None and messages and _SPOKEN_CLOCK.search(messages[-1]):
            # In an eval "tomorrow at 10 AM" was searched as "tomorrow" and 9 AM offered.
            self._call_log.event("guard_search_dropped_time", last_user=messages[-1])
            return ("Not searched: they named a time. Call book_appointment with their day and that time; "
                    "if it cannot be booked it returns the next free time.")
        if not relative and (not messages or not PREFERENCE_WORDS.search(messages[-1])):
            # Checked in code because the prompt rule alone did not hold: the model
            # searched and offered a time the moment the patient agreed to book.
            self._call_log.event("guard_search_before_preference", last_user=messages[-1] if messages else "")
            return ("Not searched: the patient has not said when suits them yet. Ask when would suit "
                    "them for the appointment and wait for their answer.")
        now = booking.clinic_now()
        try:
            start = booking.search_start(now, day=day, time=time, part_of_day=part_of_day)
        except SchedulingError as exc:
            return f"Could not search: {exc}. Ask the patient to repeat when."

        def _lookup():
            with db.session() as conn:
                return booking.earliest_free_slot(conn, now=now, not_before=start)

        slot = await asyncio.to_thread(_lookup)
        self._log("find_earliest_slot", slot is not None, day=day, time=time, part_of_day=part_of_day, slot=slot)
        if slot is None:
            return "Nothing free in the next few weeks. Offer a callback instead."
        if relative:
            self._remember_offered(ctx, [slot], from_day_after=True)
            when = scheduling.describe(slot, now)
            lead = "" if slot == start else "Nothing free at that time. The earliest after it is "
            return (f"{lead}{when}. NOT booked yet. Say that day and time to the patient and ask "
                    "if it works, then wait for their answer.")
        if slot == start and time is not None and self._booking_trusted(ctx, day, time, slot, False):
            # They named this exact time and it is free: book it now rather than
            # offering it back and spending a turn and a request on "shall I book?".
            self._call_log.event("booked_from_search", day=day, time=time)
            return await self._book(ctx, day, time, False)
        self._remember_offered(ctx, [slot])
        return _offer(slot, start, now, day)

    @function_tool
    async def book_appointment(
        self,
        ctx: RunContext,
        time: str,
        day: ApptDayArg | None = None,
        replace_existing: bool = False,
    ) -> str:
        """Book a specific day and time the patient said or accepted.

        If that time cannot be booked, it returns the earliest free time after it.

        Args:
            time: 24-hour HH:MM.
            day: The day of the appointment. "day_after_offered" only when they turn down the offered day and want the following one; never to accept an offer.
            replace_existing: True only after they agreed to move their existing appointment.
        """
        call_id = self._tool_started("book_appointment", day=day, time=time, replace_existing=replace_existing)
        result = await self._book(ctx, day, time, replace_existing)
        self._tool_finished(call_id, "book_appointment", result)
        return result

    async def _book(self, ctx: RunContext, day: str | None, time: str, replace_existing: bool) -> str:
        now = booking.clinic_now()
        relative = day == "day_after_offered"
        if relative:
            resolved_day, resolved_time, problem = self._day_after_offered(day, time)
            if problem:
                return f"Not booked: {problem}. Ask which day they mean."
            try:
                target = scheduling.requested_datetime(now, day=resolved_day, time=resolved_time)
            except SchedulingError as exc:
                return f"Not booked: {exc}. Ask the patient to repeat the time."
            if not self._patient_answered_offer(ctx, target):
                self._call_log.event("guard_day_after_offered_offer_first", day=resolved_day, time=resolved_time)
                return await self._earliest(ctx, "day_after_offered", time, None)
        day, time, problem = self._day_after_offered(day, time)
        if problem:
            return f"Not booked: {problem}. Ask which day they mean."

        try:
            requested = scheduling.requested_datetime(now, day=day, time=time)
        except SchedulingError as exc:
            return f"Not booked: {exc}. Ask the patient to repeat the time."
        if not self._booking_trusted(ctx, day, time, requested, relative):
            logger.warning("refused ungrounded booking: day=%r time=%r", day, time)
            self._log("book_appointment", False, error="day or time not stated by the patient",
                      day=day, time=time)
            return ("Not booked: the patient has not chosen that time yet. Ask when suits them "
                    "and wait for their answer before booking.")

        def _book():
            with db.session() as conn:
                return booking.request_appointment(
                    conn, patient=self._patient, now=now, day=day, time=time,
                    replace_existing=replace_existing, source_room=self._room_name)

        out = await asyncio.to_thread(_book)
        self._remember_offered(ctx, out.alternatives)
        self._log("book_appointment", out.status == "booked", status=out.status, reason=out.reason,
                  requested=out.requested, record=out.record, alternatives=out.alternatives,
                  existing=out.existing)

        if out.status == "booked":
            when = scheduling.describe(out.requested, now)
            logger.info("booked %s at %s", out.record["reference"], out.record["slot_start_utc"])
            reply = (f"Booked for {when} with {config.DOCTOR['name']}. Reference "
                     f"{out.record['reference']}. Read the time and reference back to the patient.")
            # A patient who books before hearing their results must still hear them.
            # A prompt rule for this was ignored in rehearsal; tool replies are followed.
            if not results_shared(ctx.session.history.items, self._patient):
                reply += (f" They have not heard their results yet: then tell them their HbA1c is "
                          f"{self._patient['hba1c']}% and fasting blood glucose is "
                          f"{self._patient['blood_glucose']} mg/dL, and that the doctor will go through "
                          "them at this appointment. Do not offer another appointment.")
            return reply
        if out.status == "has_existing":
            existing_at = scheduling.describe(scheduling.from_iso(out.existing["slot_start_utc"]), now)
            return (f"Not booked, and not because of availability: they already have an appointment "
                    f"{existing_at}, and each patient has one. Tell them that, and ask whether they want "
                    "to move it to the new time. If yes, call book_appointment again with replace_existing true.")
        if not out.alternatives:
            return f"Not booked: {out.reason}. Nothing free after that in the next few weeks. Offer a callback."
        return f"Not booked: {out.reason}. {_offer(out.alternatives[0], out.requested, now, day)}"

    @function_tool
    async def request_callback(
        self,
        ctx: RunContext,
        phrase: str,
        requested_by: str = "",
        in_minutes: int | None = None,
        day: DayArg | None = None,
        time: str | None = None,
        part_of_day: PartOfDayArg = None,
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
        result = await self._callback(ctx, phrase, requested_by, in_minutes, day, time, part_of_day, no_time_given)
        self._tool_finished(call_id, "request_callback", result)
        return result

    async def _callback(self, ctx: RunContext, phrase: str, requested_by: str, in_minutes: int | None,
                        day: Day | None, time: str | None, part_of_day: PartOfDay | None,
                        no_time_given: bool) -> str:
        has_time = any(v is not None for v in (in_minutes, day, time, part_of_day))
        if not has_time and not (no_time_given and self._callback_time_asked(ctx)):
            # "No preference" is only believable as an answer to "when?". In
            # rehearsal "I'm driving" was scheduled as no preference, for a time
            # nobody agreed to.
            self._callback_refused_at = len(_user_messages(ctx))
            self._call_log.event("guard_callback_without_time", no_time_given=no_time_given, phrase=phrase)
            return "Not scheduled: they have not said when suits them. Ask when would be a good time and wait."
        unsaid = self._callback_values_unsaid(ctx, day, time, part_of_day)
        if unsaid:
            # The model once turned "can you call me later?" into tomorrow at 10 AM.
            self._callback_refused_at = len(_user_messages(ctx))
            self._call_log.event("guard_callback_value_unsaid", unsaid=unsaid, day=day, time=time,
                                 part_of_day=part_of_day)
            return (f"Not scheduled: they did not say that {' or '.join(unsaid)}. Ask when would be a "
                    "good time and wait.")
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


def _build_llm() -> llm.LLM:
    """Two providers serving the same model, each the other's fallback.

    Both serve gpt-oss-120b, so a fallback mid-call does not change behaviour.
    Reasoning effort is low on both: equal reply quality on our scenarios and
    far fewer reasoning tokens, which count against rate limits and latency.
    """
    primary = inference.LLM(
        model=config.LIVEKIT_LLM_MODEL,
        extra_kwargs={"reasoning_effort": config.LLM_REASONING_EFFORT},
    )
    fallback = openai.LLM(
        model=config.LLM_MODEL,
        base_url=config.GROQ_BASE_URL,
        api_key=os.environ["GROQ_API_KEY"],
        reasoning_effort=config.LLM_REASONING_EFFORT,
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
            # "Priya Sharma" was heard as "Rias Roma" and the agent wrongly
            # decided a stranger had answered.
            keyterm=_name_keyterms(patient),
        ),
        llm=_build_llm(),
        tts=deepgram.TTS(model=config.TTS_MODEL),
        vad=vad,
    )


async def _open_conversation(session: AgentSession, patient: dict[str, Any], *,
                             let_them_speak_first: bool) -> None:
    """Ask for the patient by name.

    Fixed text, not generated: it must never mention the clinic or the reason
    before we know who answered, and it costs no LLM request. On a real outbound
    call the callee says "hello" first, so we wait briefly for them, then open
    regardless so a silent pickup is not met with silence.
    """
    if let_them_speak_first:
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

    session.say(config.opening_line(patient))


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
        "history": session.history.to_dict(),
        "tool_results": agent.tool_results,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("transcript saved to %s", path)
    return path


# Calls in progress in this process, so the session-end hook can reach them.
_CALLS: dict[str, dict[str, Any]] = {}


async def _analyze_call(room: str, call: dict[str, Any]) -> None:
    """Save the transcript and the post-call analysis next to it. Never raises."""
    agent, session, call_log = call["agent"], call["session"], call["log"]
    _save_transcript_for(room, agent, session)
    try:
        rows = [json.loads(line) for line in call_log.path.read_text().splitlines() if line.strip()]
        record = post_call.CallRecord(room=room, patient=agent.patient,
                                      history=session.history.to_dict()["items"],
                                      tool_results=agent.tool_results, log_rows=rows)
        analysis = await post_call.analyze(record)
        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        path = TRANSCRIPT_DIR / f"{room}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_analysis.json"
        path.write_text(json.dumps(analysis, indent=2, default=str))
        call_log.event("analysis_done", outcome=analysis["outcome"],
                       booking_successful=analysis["booking_successful"],
                       flags=analysis["flags"], error=analysis["error"], path=str(path))
        logger.info("call analysis: %s (booking %s) saved to %s",
                    analysis["outcome"], analysis["booking_successful"], path)
    except Exception as exc:
        logger.exception("post-call analysis failed")
        call_log.event("analysis_failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        call["analyzed"] = True


async def _on_session_end(ctx: JobContext) -> None:
    # Runs after the session closes and before shutdown callbacks, with a far
    # longer allowance (300 s) than they get (10 s), so analysis fits here.
    if (call := _CALLS.get(ctx.room.name)) and not call.get("analyzed"):
        await _analyze_call(ctx.room.name, call)


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=_on_session_end)
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
    await _open_conversation(session, patient, let_them_speak_first=False)

    _CALLS[ctx.room.name] = {"agent": agent, "session": session, "log": call_log}

    async def _on_shutdown() -> None:
        call = _CALLS.pop(ctx.room.name, None)
        if call and not call.get("analyzed"):
            # The session-end hook did not run; keep at least the transcript.
            _save_transcript_for(ctx.room.name, agent, session)
        call_log.close()

    ctx.add_shutdown_callback(_on_shutdown)


if __name__ == "__main__":
    agents.cli.run_app(server)
