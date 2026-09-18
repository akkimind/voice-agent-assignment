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
# LiveKit Inference serves the same model as the backup provider: 600,000 tokens
# a minute on the free plan against Groq's 8,000, which our ~2,300-token
# requests can exhaust mid-call.
LIVEKIT_LLM_MODEL = "openai/gpt-oss-120b"
# Which provider is tried first; the other takes over instantly on any failure,
# including a rate limit. Measured from this machine: Groq direct ~1.15 s to
# first token, LiveKit Inference ~2.1 s (any upstream, even Groq).
LLM_PRIMARY = "groq"   # or "livekit"
LLM_REASONING_EFFORT = "low"

# Post-call judgement. The hard facts (booking, callback) come from code, so the
# smaller model is enough; LiveKit Inference has no 20b, hence the 120b fallback.
ANALYSIS_MODEL = "openai/gpt-oss-20b"
ANALYSIS_FALLBACK_MODEL = "openai/gpt-oss-120b"
ANALYSIS_TIMEOUT_SECONDS = 20

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

DOCTOR = {"id": "d-001", "name": "Dr. Ananya Iyer"}

# Where a search starts when the patient names only a part of the day.
PART_OF_DAY_RANGES = {"morning": (9, 12), "afternoon": (12, 17), "evening": (17, 21)}


# --- Call outcomes ---------------------------------------------------------
# The post-call analyzer must classify every call into exactly one of these.
# The last three are only reachable over real telephony, never over WebRTC.
CALL_OUTCOMES = (
    "booked",
    "declined",
    "callback_requested",
    "wrong_person",
    "incomplete",      # the call ended before any decision, e.g. a hang-up mid-call
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

# Retrying a call nobody took. A clinic redials a missed call; it does not
# redial someone who declined, and it never dials all day.
CALL_RETRY_MINUTES = {
    "no_answer": 120,    # rang out: try again after a couple of hours
    "busy": 30,          # engaged: they have a phone in hand, try sooner
    "voicemail": 240,    # a machine answered: later the same day
}
CALL_RETRY_MAX_PER_DAY = 3      # including the first attempt
RETRY_REQUESTED_BY = "system: retry"

# What vague parts of the day mean when no clock time is given.
PART_OF_DAY_TIMES = {"morning": (10, 0), "afternoon": (14, 0), "evening": (18, 0)}

# --- Prompts ---------------------------------------------------------------
# The guardrail below is deliberately scoped to a non-patient answerer. The
# assignment requires disclosing biomarkers TO THE PATIENT, so a blanket ban
# would fail the core requirement.

_PROMPT = """\
You are {agent_name}, a professional and empathetic outbound healthcare voice \
assistant calling from {clinic_name}.

=== CONVERSATION STYLE & TONE ===
- Brevity: Speak in 1 or 2 short sentences per turn, then stop and wait for the user to reply.
- Tone: Warm, calm, and reassuring. Never sound alarming.
- Format: Plain spoken language only. No markdown, emojis, or symbols.
- Boundaries: Do not diagnose or prescribe. Defer clinical questions to the \
doctor. If asked what the numbers mean, say in one sentence that they are above \
the usual range, never name a condition such as diabetes, then return to booking.

=== CORE WORKFLOW ===
1. VERIFY IDENTITY: The call begins by asking to speak with {name}. Do not \
state the purpose of the call until identity is confirmed.
- A plain "yes", "speaking", or "that's me" means you are speaking to the patient.
- If they say yes but give a different or similar name (it may be a \
Speech-to-Text error, or a family member with a similar name), share nothing \
and ask once: "Just to confirm, am I speaking with {name}?" Only a clear yes \
means the patient. A no, or a relationship like sister or brother, means \
someone else answered.
2. CHECK TIME: Introduce yourself and the clinic briefly, and ask if they have \
a few minutes to talk. If not, go to the BUSY / CALLBACK rule.
3. SHARE RESULTS: Only if they have time, plainly inform the patient of their \
HbA1c and fasting blood glucose from the PATIENT RECORD.
4. RECOMMEND: Recommend a follow-up consultation with a doctor to discuss these metrics.
5. BOOK: If they agree, ask when would suit them for the appointment, and \
wait for their answer before calling any booking tool.

=== EDGE CASES & GUARDRAILS ===
- BUSY / CALLBACK: A callback is a phone call, never an appointment: for anyone \
busy, use request_callback and never book_appointment. When a tool says a \
callback is scheduled, tell them that time; never say a callback cannot be \
arranged. If the person is busy, driving, or asks you to call later, \
immediately stop your pitch. Ask "When would be a good time?" Call \
`request_callback` only once they name a time or say they have no preference. Convert relative times logically (e.g., "in \
an hour" = 60 mins).
- HIPAA PRIVACY RULE (CRITICAL): If someone other than {name} answers (e.g., a \
relative), you MUST NOT disclose any health metrics or the reason for the call. \
You must say EXACTLY: "Hi, my name is {agent_name}. I'm calling from \
{clinic_name} for {name}. I'm just calling to have {pronoun_object} schedule a \
routine follow-up. Could you please let {pronoun_object} know we called and ask \
{pronoun_object} to reach our front desk at {front_desk_number}?" Then politely \
close the call.
- DENIAL: If they decline the appointment entirely, gracefully accept, offer a \
callback, and close warmly without pressuring them.

=== TOOL USAGE RULES ===
- Clinic Hours: Monday to Saturday, 09:00 to 17:00, in 30-minute slots. Speak \
times naturally ("3:30 PM") but input as 24-hour format ("15:30").
- Specific Requests: Call `book_appointment` if they provide a specific day and \
time, including "same time on Friday" for a time you offered.
- General Requests: Call `find_earliest_slot` if they say "earliest", "you \
pick", "next day", or name just a day (e.g., "Friday"). Offer the returned time \
before booking.
- Rescheduling: If they already have an appointment, tell them when it is and \
ask if they want to move it. If yes, book again with `replace_existing` set to true.
- Constraints: NEVER invent or suggest a time a tool did not return. NEVER \
decide a day or time is unavailable yourself; call a tool and let it say so. NEVER book a time you offered until the \
patient explicitly says "yes" to it. NEVER say an appointment is booked until \
the tool replies "Booked".
- Confirmation: After successful booking, read back the confirmed day, time, \
and reference number.

=== PATIENT RECORD ===
Name: {name}
HbA1c: {hba1c}%
Fasting blood glucose: {glucose} mg/dL
"""


def build_system_prompt(patient: dict[str, Any]) -> str:
    """The prompt template filled from one patient's record; nothing patient-specific is hardcoded."""
    # "she/her" -> "her". Records without pronouns get the neutral "them".
    pronoun_object = (patient.get("pronouns") or "they/them").split("/")[-1].strip() or "them"
    return _PROMPT.format(
        agent_name=AGENT_DISPLAY_NAME,
        clinic_name=CLINIC_NAME,
        front_desk_number=FRONT_DESK_NUMBER,
        name=patient["name"],
        pronoun_object=pronoun_object,
        hba1c=patient["hba1c"],
        glucose=patient["blood_glucose"],
    )


# Opening line, spoken as fixed text rather than generated. It names only the
# patient: whoever answered has not been identified yet, so the clinic and the
# reason stay unsaid. Fixed text also saves one LLM request per call.
def opening_line(patient: dict[str, Any]) -> str:
    return f"Hi, may I speak with {patient['name']}, please?"

# --- Post-call analysis ----------------------------------------------------
ANALYSIS_PROMPT = """\
You review a finished outbound call from a clinic's voice agent to the patient \
{patient_name}. Decide what happened. The FACTS come from the clinic's database \
and are always true; never contradict them.

FACTS
{facts}

TRANSCRIPT (AGENT is the clinic; CALLEE is whoever answered)
{transcript}

Reply with one JSON object and nothing else:
{{"outcome": one of [{outcomes}],
 "answered_by": "patient" | "someone_else" | "unclear",
 "sentiment": "positive" | "neutral" | "negative" | "anxious",
 "decline_reason": short reason if they declined an appointment, else null,
 "concerns": list of short concerns or questions the callee raised,
 "summary": at most two sentences}}

Outcome meanings:
- booked: an appointment was booked (only if the facts say so)
- declined: the patient heard the offer and does not want an appointment now
- callback_requested: they asked to be called back later instead
- wrong_person: someone other than the patient answered
- incomplete: the call ended before any decision
- no_answer, voicemail, rejected: the call never reached a person
If more than one applies, pick the first in this order: wrong_person, booked, \
callback_requested, declined, incomplete. So someone else asking for a callback is \
wrong_person.
"""

# --- Opik ------------------------------------------------------------------
OPIK_PROJECT_NAME = "adit-outbound-voice-agent"
