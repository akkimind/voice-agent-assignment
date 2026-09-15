"""Appointment booking against the clinic calendar.

A booking is a concrete 30-minute slot with one doctor, never free text. The
flow for every request:

1. Resolve the patient's structured request into a slot in clinic time.
2. Under the database write lock, check the slot is valid and free, and that
   the patient has no other upcoming appointment.
3. If anything fails, book nothing and return the earliest free slot after
   the requested time so the agent can offer it. Nothing is ever silently rounded or moved.

The unique index in db.py is the final guard: even if two processes raced past
the checks, the second INSERT would fail.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Literal
from zoneinfo import ZoneInfo

import config
import db
import scheduling
from scheduling import Day, PartOfDay, SchedulingError

Status = Literal["booked", "unavailable", "needs_time", "has_existing"]

SLOT = timedelta(minutes=config.APPOINTMENT_SLOT_MINUTES)
LEAD = timedelta(minutes=config.APPOINTMENT_MIN_LEAD_MINUTES)
HORIZON = timedelta(days=config.APPOINTMENT_MAX_DAYS_AHEAD)
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class BookingOutcome:
    status: Status
    requested: datetime | None = None
    reason: str = ""
    record: dict[str, Any] | None = None
    alternatives: list[datetime] = field(default_factory=list)
    existing: dict[str, Any] | None = None


def clinic_now() -> datetime:
    return datetime.now(ZoneInfo(config.CLINIC_TIMEZONE))


def slot_problem(slot: datetime, now: datetime) -> str | None:
    """Why a slot cannot be booked, or None if it is a bookable time."""
    if not scheduling.on_grid(slot, config.APPOINTMENT_SLOT_MINUTES):
        return "appointments start on the hour or the half hour"
    if slot.weekday() not in config.CLINIC_OPEN_WEEKDAYS:
        return f"the clinic is closed on {DAY_NAMES[slot.weekday()]}s"
    opens = slot.replace(hour=config.CLINIC_OPEN_HOUR, minute=0)
    closes = slot.replace(hour=config.CLINIC_CLOSE_HOUR, minute=0)
    if slot < opens or slot + SLOT > closes:
        return (f"the clinic sees patients from {scheduling.hour_phrase(config.CLINIC_OPEN_HOUR)} "
                f"to {scheduling.hour_phrase(config.CLINIC_CLOSE_HOUR)}")
    if slot < now + LEAD:
        return "that is too soon to arrange"
    if slot - now > HORIZON:
        return f"we only book up to {config.APPOINTMENT_MAX_DAYS_AHEAD} days ahead"
    return None


def _valid_slots(now: datetime, *, on_date=None, hours: tuple[int, int] | None = None) -> Iterator[datetime]:
    """Every bookable slot, optionally limited to one date and an hour range."""
    first = on_date or now.date()
    last = on_date or (now + HORIZON).date()
    day = first
    while day <= last:
        start_h, end_h = hours or (config.CLINIC_OPEN_HOUR, config.CLINIC_CLOSE_HOUR)
        cursor = datetime(day.year, day.month, day.day, start_h, 0, tzinfo=now.tzinfo)
        stop = datetime(day.year, day.month, day.day, 0, 0, tzinfo=now.tzinfo) + timedelta(hours=end_h)
        while cursor < stop:
            if slot_problem(cursor, now) is None:
                yield cursor
            cursor += SLOT
        day += timedelta(days=1)


def _free(conn: sqlite3.Connection, slots: list[datetime], now: datetime) -> list[datetime]:
    if not slots:
        return []
    booked = db.booked_slots(conn, config.DOCTOR["id"],
                             scheduling.to_utc_iso(now), scheduling.to_utc_iso(now + HORIZON + SLOT))
    return [s for s in slots if scheduling.to_utc_iso(s) not in booked]


def earliest_free_slot(conn: sqlite3.Connection, *, now: datetime, not_before: datetime) -> datetime | None:
    """The first bookable, unbooked slot starting at or after not_before.

    One rule covers every "when can I come?" case: no preference searches from
    now, "Friday" from Friday's opening, and a taken or impossible time from
    that time onward, so the patient always hears the next real option after
    what they asked for.
    """
    start = max(not_before, now)
    candidates = [s for s in _valid_slots(now) if s >= start]
    free = _free(conn, candidates, now)
    return free[0] if free else None


def search_start(now: datetime, *, day: Day | None, time: str | None,
                 part_of_day: PartOfDay | None) -> datetime:
    """Where a search for the earliest slot begins, from the patient's words."""
    if time is not None:
        return scheduling.requested_datetime(now, day=day, time=time)
    if part_of_day is not None:
        start_hour = config.PART_OF_DAY_RANGES[part_of_day][0]
        return scheduling.requested_datetime(now, day=day or "today", time=f"{start_hour:02d}:00")
    if day is not None:
        return scheduling.requested_datetime(now, day=day, time="00:00")
    return now


def request_appointment(
    conn: sqlite3.Connection,
    *,
    patient: dict[str, Any],
    now: datetime,
    day: Day | None = None,
    time: str | None = None,
    part_of_day: PartOfDay | None = None,
    notes: str = "",
    replace_existing: bool = False,
    source_room: str = "",
) -> BookingOutcome:
    """Book the requested slot, or explain why not and offer alternatives."""
    if time is None:
        # A day or part of day alone is not a choice of slot. Offer the earliest
        # real one instead of booking on the patient's behalf.
        start = search_start(now, day=day, time=None, part_of_day=part_of_day)
        slot = earliest_free_slot(conn, now=now, not_before=start)
        return BookingOutcome("needs_time", requested=start, alternatives=[slot] if slot else [])

    requested = scheduling.requested_datetime(now, day=day, time=time, part_of_day=part_of_day)
    requested_utc = scheduling.to_utc_iso(requested)

    try:
        with db.transaction(conn):
            existing = db.upcoming_appointment(conn, patient["id"], scheduling.to_utc_iso(now))
            # Checked before availability: the patient's own booking occupies the
            # slot, and must not be reported back to them as someone else's.
            if existing and existing["slot_start_utc"] == requested_utc:
                return BookingOutcome("booked", requested, "already booked for that time", record=existing)

            problem = slot_problem(requested, now)
            if problem is None and db.slot_is_booked(conn, config.DOCTOR["id"], requested_utc):
                problem = "that time is already booked"
            if problem:
                return BookingOutcome("unavailable", requested, problem,
                                      alternatives=_next_after(conn, requested, now))

            if existing and not replace_existing:
                return BookingOutcome("has_existing", requested, existing=existing)
            if existing:
                db.cancel_appointment(conn, existing["reference"])

            record = {
                "reference": f"ADT-{secrets.token_hex(3).upper()}",
                "patient_id": patient["id"],
                "doctor_id": config.DOCTOR["id"],
                "slot_start_utc": requested_utc,
                "notes": notes.strip(),
                "source_room": source_room,
                "created_utc": scheduling.to_utc_iso(db.utc_now()),
            }
            db.insert_appointment(conn, record)
            return BookingOutcome("booked", requested, record={**record, "doctor_name": config.DOCTOR["name"],
                                                               "replaced": existing["reference"] if existing else None})
    except sqlite3.IntegrityError:
        # Another call took the slot between our check and our insert. The
        # transaction rolled back, so any cancellation above was undone too.
        return BookingOutcome("unavailable", requested, "someone has just booked that time",
                              alternatives=_next_after(conn, requested, now))


def _next_after(conn: sqlite3.Connection, requested: datetime, now: datetime) -> list[datetime]:
    slot = earliest_free_slot(conn, now=now, not_before=requested)
    return [slot] if slot else []
