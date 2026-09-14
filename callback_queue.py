"""Callback queue: turn a spoken callback request into a scheduled outbound call.

A callback is scheduled for when the caller asked, then only ever moved LATER:
requested time -> minimum lead -> calling window -> next free callback slot.
An idle agent never pulls a callback earlier.

We dial one call at a time, so each callback owns a 10-minute slot. A second
caller asking for 9:00 is told 9:10, and the database refuses two pending calls
in one slot. Each patient has at most one pending callback; a new request
supersedes the old one inside the same transaction.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import config
import db
import scheduling
from scheduling import Day, PartOfDay, SchedulingError

SLOT = timedelta(minutes=config.CALLBACK_SLOT_MINUTES)
START, END = config.CALLBACK_WINDOW_START_HOUR, config.CALLBACK_WINDOW_END_HOUR
WINDOW_REASON = f"we only call between {scheduling.hour_phrase(START)} and {scheduling.hour_phrase(END)}"


def patient_now(patient: dict[str, Any]) -> datetime:
    """The current time in the patient's own timezone."""
    return datetime.now(ZoneInfo(patient["timezone"]))


def _into_window(moment: datetime) -> tuple[datetime, bool]:
    """Push a time forward into the calling window. Returns (time, was_moved)."""
    if moment.hour < START:
        return moment.replace(hour=START, minute=0, second=0, microsecond=0), True
    if moment.hour >= END:
        nxt = (moment + timedelta(days=1)).date()
        return datetime(nxt.year, nxt.month, nxt.day, START, 0, tzinfo=moment.tzinfo), True
    return moment, False


def earliest_allowed(
    now: datetime,
    *,
    in_minutes: int | None = None,
    day: Day | None = None,
    time: str | None = None,
    part_of_day: PartOfDay | None = None,
    no_time_given: bool = False,
) -> tuple[datetime, datetime, list[str]]:
    """Pure rules, no database: (requested, earliest_allowed_slot, reasons_moved).

    The result is on the callback slot grid and inside the calling window, but
    not yet checked against other callbacks.
    """
    requested = scheduling.requested_datetime(
        now, in_minutes=in_minutes, day=day, time=time, part_of_day=part_of_day)
    if requested is None:
        if not no_time_given:
            raise SchedulingError("no callback time was given; ask when would suit them")
        requested = now + timedelta(minutes=config.CALLBACK_DEFAULT_DELAY_MINUTES)

    reasons: list[str] = []
    candidate = requested
    lead = now + timedelta(minutes=config.CALLBACK_MIN_LEAD_MINUTES)
    if candidate < lead:
        candidate = lead
        reasons.append(f"we need at least {config.CALLBACK_MIN_LEAD_MINUTES} minutes")

    candidate, moved = _into_window(scheduling.ceil_to_grid(candidate, config.CALLBACK_SLOT_MINUTES))
    if moved:
        reasons.append(WINDOW_REASON)

    if candidate - now > timedelta(days=config.CALLBACK_MAX_DAYS_AHEAD):
        raise SchedulingError(
            f"that is more than {config.CALLBACK_MAX_DAYS_AHEAD} days away; ask for something sooner")
    return requested, candidate, reasons


def schedule(
    conn: sqlite3.Connection,
    *,
    patient: dict[str, Any],
    now: datetime,
    phrase: str,
    requested_by: str = "",
    in_minutes: int | None = None,
    day: Day | None = None,
    time: str | None = None,
    part_of_day: PartOfDay | None = None,
    no_time_given: bool = False,
    source_room: str = "",
) -> dict[str, Any]:
    """Queue a callback in the first free slot at or after the allowed time.

    Returns the stored record plus `requested`, `scheduled` and `moved_because`.
    Raises SchedulingError when the request cannot be scheduled.
    """
    requested, slot, reasons = earliest_allowed(
        now, in_minutes=in_minutes, day=day, time=time,
        part_of_day=part_of_day, no_time_given=no_time_given)
    reference = f"CB-{secrets.token_hex(3).upper()}"

    with db.transaction(conn):
        # Free this patient's old slot first, so re-requesting the same time keeps it.
        db.supersede_pending_callbacks(conn, patient["id"], reference)

        walked_past_taken = False
        while db.callback_slot_taken(conn, scheduling.to_utc_iso(slot)):
            walked_past_taken = True
            slot, moved = _into_window(slot + SLOT)
            if moved and WINDOW_REASON not in reasons:
                reasons.append(WINDOW_REASON)
            if slot - now > timedelta(days=config.CALLBACK_MAX_DAYS_AHEAD):
                raise SchedulingError("no callback slot is free soon enough; ask for another day")
        if walked_past_taken:
            reasons.append("the earlier callback times were already taken")

        record = {
            "reference": reference,
            "patient_id": patient["id"],
            "due_utc": scheduling.to_utc_iso(slot),
            "requested_utc": scheduling.to_utc_iso(requested),
            "timezone": str(now.tzinfo),
            "phrase": phrase.strip(),
            "requested_by": requested_by.strip() or "unknown",
            "moved_because": reasons,
            "source_room": source_room,
            "created_utc": scheduling.to_utc_iso(db.utc_now()),
        }
        db.insert_callback(conn, record)

    return {**record, "requested": requested, "scheduled": slot}


def due(conn: sqlite3.Connection, now_utc: datetime | None = None) -> list[dict[str, Any]]:
    """Pending callbacks whose time has arrived, oldest first. For the Phase 7 dispatcher."""
    return db.due_callbacks(conn, scheduling.to_utc_iso(now_utc or db.utc_now()))
