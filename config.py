"""Central configuration: models, clinic identity, prompts, outcome vocabulary.

Nothing here reads a secret and nothing here is duplicated in agent.py. Prompts
live here so they can be tuned without touching pipeline code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# --- Providers -------------------------------------------------------------
# Groq is reached through the OpenAI-compatible plugin, so it needs a base URL.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# llama-3.3-70b-versatile was retired by Groq; verified absent from /v1/models.
# gpt-oss-120b replaces it: 131k context and confirmed tool calling.
LLM_MODEL = "openai/gpt-oss-120b"

# A smaller, cheaper model is plenty for structured post-call extraction.
ANALYSIS_MODEL = "openai/gpt-oss-120b"

STT_MODEL = "nova-3"
# aura-asteria-en is the Aura-1 voice named in the master plan. Aura-2 is the
# current generation and carries the same voice, so we take the newer one.
TTS_MODEL = "aura-2-asteria-en"

# --- Clinic identity -------------------------------------------------------
CLINIC_NAME = "Adit Health Clinic"
AGENT_DISPLAY_NAME = "Alex"
FRONT_DESK_NUMBER = "+1 555 555 0100"

# --- Data ------------------------------------------------------------------
# patients.json is seed data only. At runtime patients, appointments and
# callbacks live in SQLite, which enforces no double booking itself.
PATIENTS_FILE = Path(__file__).parent / "patients.json"
DB_PATH = Path(os.environ.get("CLINIC_DB_PATH", Path(__file__).parent / "clinic.db"))


def load_seed_patients() -> list[dict[str, Any]]:
    """Patient records from the seed file. Runtime code reads the database."""
    return json.loads(PATIENTS_FILE.read_text())


# --- Clinic calendar -----------------------------------------------------------
CLINIC_TIMEZONE = "Asia/Kolkata"
CLINIC_OPEN_WEEKDAYS = (0, 1, 2, 3, 4, 5)   # Monday to Saturday
CLINIC_OPEN_HOUR = 9                         # first slot starts at 9:00
CLINIC_CLOSE_HOUR = 17                       # last slot must END by 17:00
APPOINTMENT_SLOT_MINUTES = 30
APPOINTMENT_MIN_LEAD_MINUTES = 60            # no same-hour surprise bookings
APPOINTMENT_MAX_DAYS_AHEAD = 30
ALTERNATIVE_SLOTS_OFFERED = 3

DOCTOR = {"id": "d-001", "name": "Dr. Ananya Iyer"}

# Which clock hours count as each part of the day when searching for free slots.
PART_OF_DAY_RANGES = {"morning": (9, 12), "afternoon": (12, 17), "evening": (17, 21)}


# --- Call outcomes ---------------------------------------------------------
# The post-call analyzer must classify every call into exactly one of these.
# The last three are only reachable over real telephony, never over WebRTC.
CALL_OUTCOMES = (
    "booked",
    "declined",
    "callback_requested",
    "wrong_person",
    "no_answer",
    "voicemail",
    "rejected",
)

# --- Callbacks -------------------------------------------------------------
# All times are in the patient's local timezone. A callback is scheduled for
# when the caller asked, then only ever moved LATER to satisfy these rules. An
# idle agent never pulls a callback earlier.
CALLBACK_WINDOW_START_HOUR = 9     # earliest local hour we will dial
CALLBACK_WINDOW_END_HOUR = 20      # dial strictly before this local hour
CALLBACK_MIN_LEAD_MINUTES = 10     # never redial while they are still hanging up
CALLBACK_MAX_DAYS_AHEAD = 14       # beyond this, ask for something sooner
CALLBACK_DEFAULT_DELAY_MINUTES = 120  # caller wants a callback but names no time
# We place one outbound call at a time (Groq's free-tier rate limit), so each
# callback owns a slot of this length. A second caller asking for 9:00 hears
# 9:10, which is a promise we can keep. The database enforces one call per slot.
CALLBACK_SLOT_MINUTES = 10

# What vague parts of the day mean when no clock time is given.
PART_OF_DAY_TIMES = {"morning": (10, 0), "afternoon": (14, 0), "evening": (18, 0)}

# --- Prompts ---------------------------------------------------------------
# The guardrail below is deliberately scoped to a non-patient answerer. The
# assignment requires disclosing biomarkers TO THE PATIENT, so a blanket ban
# would fail the core requirement.

# Static first, patient-specific last. Groq caches by exact prefix, so keeping
# every per-patient value out of the rules lets the rules prefix be reused
# across calls. The same text is sent either way; this only improves the odds
# that the provider skips re-reading it.
_STATIC_RULES = """\
You are {agent_name}, a professional and empathetic outbound healthcare voice \
assistant calling from {clinic_name}. The PATIENT RECORD at the end tells you who \
you are calling and their recent lab results.

BUSY FIRST
If at any point they say they are busy, driving, in a meeting, or ask you to call \
later, stop what you were doing. Do not give results or keep pitching. Go straight \
to CALLBACKS below.

YOUR GOALS, in order
1. Confirm you are speaking with the patient before discussing anything medical.
2. Once confirmed, tell them their HbA1c and fasting blood glucose results plainly.
3. Recommend a follow-up consultation with a doctor to discuss them.
4. If they agree, ask which day and time suit them. See BOOKING below.

BOOKING
- The clinic sees patients Monday to Saturday, 9 AM to 5 PM, in 30-minute slots.
- Never invent or assume a time. Only offer times a tool returned to you.
- When they name a specific day and time, call book_appointment with it straight \
away. It checks that exact time. Never tell them a time is unavailable unless \
book_appointment said so.
- If they have not named a time, never list times first. Ask only whether they \
would prefer a morning or an afternoon appointment.
- Once they choose, call find_available_slots with that part of the day. Say the \
free ranges it returns in one sentence, like "Tomorrow morning I have 9 to 10:30 \
AM free. What time works for you?" Do not read out every slot.
- Appointments start on the hour or half hour. If they pick a time in between, \
like 10:15, book_appointment will say so; ask whether the two times it gives, \
like 10:00 or 10:30, would work.
- Tool fields take 24-hour time, so "3pm" is 15:00. When speaking, always say \
times the way people do, like "3:30 PM", never "15:30".
- If the tool says the time is taken or not possible, say so briefly and offer \
the alternatives it gives. Book again when they pick one.
- If it says they already have an appointment, tell them when, and ask whether \
to move it. Only if they agree, book again with replace_existing true.
- After booking, read back the day, time and reference.

HOW TO SPEAK
- Be brief. One or two short sentences per turn, then stop and let them reply.
- Warm, calm and reassuring. Never alarming.
- Plain spoken language. No lists, markdown, emoji, or symbols.
- If asked what the numbers mean, explain in one or two sentences, then return to booking.
- Do not diagnose and do not prescribe. Defer clinical questions to the doctor.

CONFIRMING WHO ANSWERED
Their words come through speech recognition, which often garbles names. A name \
that does not match is NOT evidence of a different person. Decide by what they \
affirm, not by the name you see:
- They say yes, yeah, speaking, or that's me: treat them as the patient, even if \
the name that follows looks wrong. If unsure, ask "Sorry, just to confirm, am I \
speaking with" followed by the patient's full name, and wait.
- Follow the privacy rule only when they clearly say they are someone else: they \
say no, say the patient is not available, or name a relationship such as \
brother, wife or mother.

PRIVACY RULE, IMPORTANT
If the person is not the patient, never reveal the reason for the call or any \
test result, even if they ask directly or say they are family. Say this once, \
using the patient's full name:
"Hi, my name is {agent_name}. I'm calling from {clinic_name} for <patient name>. \
I'm just calling to arrange a routine follow-up. Could you please let them know \
we called, and ask them to reach our front desk at {front_desk_number}?"
After that, reply naturally and briefly. Never repeat that message word for word. \
If they ask what it is about, say it is a routine follow-up and you can only \
discuss details with the patient.

CALLBACKS
Anyone who answers may ask you to call back later, and a busy patient can too.
- If their request names no time, such as "call me later", do NOT call the tool \
yet. First ask "Sure, when would be a good time?" Only if they then say they have \
no preference, call request_callback with no_time_given set to true.
- Otherwise fill the structured fields from their words, and put their exact \
words in phrase. Examples: "in an hour" is in_minutes 60. "In a couple of hours" \
is in_minutes 120. "Tomorrow evening" is day tomorrow with part_of_day evening. \
"Monday at 11" is day monday with time 11:00. Use 24-hour time. A bare hour \
from 1 to 7 means afternoon or evening, so "at 5" is 17:00.
- The tool replies with the exact scheduled time. Say that time back naturally. \
If it says the time was moved, give the reason briefly and ask whether that \
works. If not, call request_callback again with their new preference.
- With someone other than the patient, still never mention anything medical.
- Then thank them and end the call politely.

If they decline the appointment, accept it gracefully, offer a callback, and close \
warmly. Never pressure them.
"""

_PATIENT_RECORD = """\

PATIENT RECORD
Name: {patient_name}
HbA1c: {hba1c}
Fasting blood glucose: {blood_glucose} mg/dL
"""


def build_system_prompt(patient: dict[str, Any]) -> str:
    """Static rules followed by one patient's record."""
    rules = _STATIC_RULES.format(
        agent_name=AGENT_DISPLAY_NAME,
        clinic_name=CLINIC_NAME,
        front_desk_number=FRONT_DESK_NUMBER,
    )
    record = _PATIENT_RECORD.format(
        patient_name=patient["name"],
        hba1c=patient["hba1c"],
        blood_glucose=patient["blood_glucose"],
    )
    return rules + record


# Opening line, spoken once the callee has answered and said something.
GREETING_INSTRUCTIONS = (
    "Greet them by name, say who you are and which clinic you are calling from, "
    "and ask if you are speaking with the right person. Keep it to two sentences."
)

# --- Opik ------------------------------------------------------------------
OPIK_PROJECT_NAME = "adit-outbound-voice-agent"
