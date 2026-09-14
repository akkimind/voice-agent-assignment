"""Appointment booking against the clinic calendar.

A booking is a concrete 30-minute slot with one doctor, never free text. The
flow for every request:

1. Resolve the patient's structured request into a slot in clinic time.
2. Under the database write lock, check the slot is valid and free, and that
   the patient has no other upcoming appointment.
3. If anything fails, book nothing and return the nearest free alternatives
   so the agent can offer them. Nothing is ever silently rounded or moved.

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


def nearest_free_slots(conn: sqlite3.Connection, *, requested: datetime, now: datetime,
                       limit: int = config.ALTERNATIVE_SLOTS_OFFERED) -> list[datetime]:
    """The free slots closest to what was asked for, returned in time order.

    Closeness is judged the way a person would: nearest day first, then nearest
    time of day. Someone who wanted Sunday at 11 hears Saturday or Monday around
    11, not Saturday at 4:30 PM just because it is fewer raw hours away.
    """
    free = _free(conn, list(_valid_slots(now)), now)
    wanted_minute = requested.hour * 60 + requested.minute
    free.sort(key=lambda s: (
        abs((s.date() - requested.date()).days),
        abs(s.hour * 60 + s.minute - wanted_minute),
        s,
    ))
    return sorted(free[:limit])


def neighbouring_free_slots(conn: sqlite3.Connection, *, requested: datetime, now: datetime) -> list[datetime]:
    """For a time between slots, the free slot either side of it.

    "10:15" should hear "10:00 or 10:30", not three times ranked across the day.
    Falls back to the nearest free slots when neither neighbour can be booked.
    """
    minute_of_day = requested.hour * 60 + requested.minute
    floor = requested.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=minute_of_day - minute_of_day % config.APPOINTMENT_SLOT_MINUTES)
    either_side = [s for s in (floor, floor + SLOT) if slot_problem(s, now) is None]
    return _free(conn, either_side, now) or nearest_free_slots(conn, requested=requested, now=now)


def free_ranges(slots: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Group consecutive slots into (first start, last start) runs.

    9:00, 9:30, 10:00, 11:30 becomes [(9:00, 10:00), (11:30, 11:30)], which the
    agent can say as "from 9 to 10 AM, or at 11:30 AM".
    """
    ranges: list[tuple[datetime, datetime]] = []
    for slot in sorted(slots):
        if ranges and slot - ranges[-1][1] == SLOT:
            ranges[-1] = (ranges[-1][0], slot)
        else:
            ranges.append((slot, slot))
    return ranges


def available_slots(conn: sqlite3.Connection, *, now: datetime, day: Day | None,
                    part_of_day: PartOfDay | None, limit: int | None = 4) -> tuple[list[datetime], str | None]:
    """Free slots on one day, optionally within a part of the day.

    Returns (slots, reason_if_none) so the agent can say why nothing is free.
    """
    hours = config.PART_OF_DAY_RANGES[part_of_day] if part_of_day else None

    if day is None:
        # No day named: the first day that has anything free.
        cursor = now.date()
        while cursor <= (now + HORIZON).date():
            slots = _free(conn, list(_valid_slots(now, on_date=cursor, hours=hours)), now)
            if slots:
                return slots[:limit], None
            cursor += timedelta(days=1)
        return [], "nothing is free in the next few weeks"

    target = scheduling.requested_datetime(now, day=day, time="12:00")
    if target.weekday() not in config.CLINIC_OPEN_WEEKDAYS:
        return [], f"the clinic is closed on {DAY_NAMES[target.weekday()]}s"
    slots = _free(conn, list(_valid_slots(now, on_date=target.date(), hours=hours)), now)
    if not slots:
        return [], "nothing is free then"
    return slots[:limit], None


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
        # A day or part of day alone is not a choice of slot. Offer real free
        # times instead of picking one on the patient's behalf.
        slots, reason = available_slots(conn, now=now, day=day, part_of_day=part_of_day, limit=None)
        if not slots:
            anchor = scheduling.requested_datetime(now, day=day, part_of_day=part_of_day) or now
            slots = nearest_free_slots(conn, requested=anchor, now=now)
        return BookingOutcome("needs_time", reason=reason or "", alternatives=slots)

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
                off_grid = not scheduling.on_grid(requested, config.APPOINTMENT_SLOT_MINUTES)
                finder = neighbouring_free_slots if off_grid else nearest_free_slots
                return BookingOutcome("unavailable", requested, problem,
                                      alternatives=finder(conn, requested=requested, now=now))

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
                              alternatives=nearest_free_slots(conn, requested=requested, now=now))
