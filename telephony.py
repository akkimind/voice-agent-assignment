"""Placing a real phone call, and reading what the phone network says back.

The agent itself is transport-agnostic: the conversation is identical whether
the audio arrives over a browser or a phone line. Only this module knows about
SIP, so the rest of the code stays testable without a telephone.

Two rules live here, both learned from the documentation's warnings:

- Dial and wait for an answer BEFORE the session starts, so the agent never
  talks over the ringtone.
- A call that never reaches a person still has an outcome. Busy, declined and
  unanswered come back as SIP status codes, which map onto the outcomes the
  post-call record already uses.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("adit-agent.telephony")

RINGING_TIMEOUT_SECONDS = 30
MAX_CALL_MINUTES = 15

# SIP status codes, as the phone network reports the end of an attempt.
# https://datatracker.ietf.org/doc/html/rfc3261#section-21
STATUS_OUTCOMES = {
    486: "rejected",     # Busy here
    600: "rejected",     # Busy everywhere
    603: "rejected",     # Declined
    604: "no_answer",    # Does not exist anywhere
    404: "no_answer",    # Not found
    408: "no_answer",    # Request timeout
    480: "no_answer",    # Temporarily unavailable
    487: "no_answer",    # Request terminated: nobody picked up
}


# Why a call ended, for deciding whether to try again. Declining is a decision;
# a wrong number is a data problem; the rest are bad timing.
RETRY_REASONS = {
    486: "busy", 600: "busy",
    408: "no_answer", 480: "no_answer", 487: "no_answer",
}
NO_RETRY_REASONS = {
    603: "declined",          # they rejected the call: calling back is harassment
    404: "unknown_number",    # the number does not exist
    604: "unknown_number",
}


def retry_reason(code: int | None) -> str | None:
    """Why this call should be retried, or None when it should not be."""
    return RETRY_REASONS.get(code) if code is not None else None


def needs_front_desk(code: int | None) -> bool:
    """A number that cannot be reached at all is for a human to sort out."""
    return NO_RETRY_REASONS.get(code) == "unknown_number"


@dataclass
class DialRequest:
    room: str
    phone: str
    patient_id: str
    trunk_id: str
    identity: str = "patient"


@dataclass
class DialFailure:
    """A call that never became a conversation."""
    outcome: str            # rejected, no_answer, or incomplete
    status_code: int | None
    status: str
    detail: str


def outcome_for_status(code: int | None) -> str:
    """What a SIP status code means for the call's outcome."""
    if code is None:
        return "incomplete"
    return STATUS_OUTCOMES.get(code, "incomplete")


def trunk_id() -> str:
    trunk = os.environ.get("SIP_OUTBOUND_TRUNK_ID", "")
    if not trunk:
        raise RuntimeError("SIP_OUTBOUND_TRUNK_ID is not set; see telephony/README.md")
    return trunk


def dial_request(request: DialRequest) -> Any:
    """The LiveKit request that makes the phone ring."""
    from livekit import api
    return api.CreateSIPParticipantRequest(
        sip_trunk_id=request.trunk_id,
        sip_call_to=request.phone,
        room_name=request.room,
        participant_identity=request.identity,
        participant_name="patient",
        participant_metadata=request.patient_id,
        # The agent must hear a real person before it speaks, so the dial call
        # returns only once the callee answers.
        wait_until_answered=True,
        play_dialtone=False,
        ringing_timeout=_duration(seconds=RINGING_TIMEOUT_SECONDS),
        max_call_duration=_duration(seconds=MAX_CALL_MINUTES * 60),
    )


def _duration(*, seconds: int) -> Any:
    from google.protobuf.duration_pb2 import Duration
    return Duration(seconds=seconds)


async def dial(api_client: Any, request: DialRequest) -> DialFailure | None:
    """Ring the patient and wait for an answer. None means they answered."""
    logger.info("dialling %s for %s", request.phone, request.patient_id)
    try:
        await api_client.sip.create_sip_participant(dial_request(request))
        return None
    except Exception as exc:
        # Anything carrying a SIP status came from the phone network (busy,
        # declined, nobody home). Anything else is ours: a missing trunk, wrong
        # credentials. Both end the attempt; only the first has an outcome.
        code = getattr(exc, "sip_status_code", None)
        if code is None and not hasattr(exc, "sip_status"):
            logger.exception("dialling failed")
            return DialFailure("incomplete", None, "", f"{type(exc).__name__}: {exc}"[:300])
        failure = DialFailure(outcome_for_status(code), code,
                              str(getattr(exc, "sip_status", "") or ""), str(exc)[:300])
        logger.info("call not answered: %s (%s)", failure.outcome, failure.status_code)
        return failure


def simulated_failure(code: int) -> DialFailure:
    """A refused call without a phone, for exercising the paths a browser cannot.

    Always marked simulated, so no record can pass for a real call.
    """
    return DialFailure(outcome_for_status(code), code, "simulated",
                       f"simulated SIP {code}: no call was placed")


async def hang_up(api_client: Any, room: str) -> None:
    """End the call from our side, so a finished call does not sit silent."""
    from livekit import api
    try:
        await api_client.room.delete_room(api.DeleteRoomRequest(room=room))
        logger.info("hung up room %s", room)
    except Exception:
        logger.exception("could not hang up room %s", room)
