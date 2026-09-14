"""Pure time helpers shared by appointment booking and callbacks.

Nothing here touches the database or the clock; callers pass `now`. That keeps
every rule deterministic and unit-testable.

The language model converts speech into the structured fields these functions
take ("tomorrow evening" -> day="tomorrow", part_of_day="evening"). Code does
the arithmetic, because models are unreliable with dates and timezones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

import config

Day = Literal[
    "today", "tomorrow", "day_after_tomorrow",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]
PartOfDay = Literal["morning", "afternoon", "evening"]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class SchedulingError(Exception):
    """A request that cannot be turned into a time; the message is safe to relay."""


def parse_clock(value: str) -> tuple[int, int]:
    """'17:30' -> (17, 30). Rejects anything that is not 24-hour HH:MM."""
    try:
        hour_s, minute_s = value.strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
    except ValueError as exc:
        raise SchedulingError(f"time {value!r} is not HH:MM") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise SchedulingError(f"time {value!r} is out of range")
    return hour, minute


def requested_datetime(
    now: datetime,
    *,
    in_minutes: int | None = None,
    day: Day | None = None,
    time: str | None = None,
    part_of_day: PartOfDay | None = None,
) -> datetime | None:
    """What the caller literally asked for, in `now`'s timezone, before any rules.

    Returns None when no timing information was given at all.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    if in_minutes is not None:
        if in_minutes <= 0:
            raise SchedulingError("in_minutes must be positive")
        return now + timedelta(minutes=in_minutes)

    if day is None and time is None and part_of_day is None:
        return None

    if time is not None:
        hour, minute = parse_clock(time)
    elif part_of_day is not None:
        hour, minute = config.PART_OF_DAY_TIMES[part_of_day]
    else:
        # A day with no time, e.g. "tomorrow": that day's morning.
        hour, minute = config.PART_OF_DAY_TIMES["morning"]

    if day is None or day == "today":
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # "At 4pm" said at 6pm means tomorrow, not a time already gone.
        if day is None and target <= now:
            target += timedelta(days=1)
        return target

    if day in ("tomorrow", "day_after_tomorrow"):
        offset = 1 if day == "tomorrow" else 2
    else:
        # The next occurrence of that weekday, never today.
        offset = (WEEKDAYS.index(day) - now.weekday()) % 7 or 7

    target_date = (now + timedelta(days=offset)).date()
    return datetime(target_date.year, target_date.month, target_date.day,
                    hour, minute, tzinfo=now.tzinfo)


def ceil_to_grid(moment: datetime, minutes: int) -> datetime:
    """Round up to the next slot boundary; a moment already on the grid is kept."""
    moment = moment.replace(second=0, microsecond=0) + (
        timedelta(minutes=1) if moment.second or moment.microsecond else timedelta()
    )
    remainder = (moment.hour * 60 + moment.minute) % minutes
    return moment if remainder == 0 else moment + timedelta(minutes=minutes - remainder)


def on_grid(moment: datetime, minutes: int) -> bool:
    return moment.second == 0 and moment.microsecond == 0 and \
        (moment.hour * 60 + moment.minute) % minutes == 0


def hour_phrase(hour: int) -> str:
    """12 -> '12 PM', 20 -> '8 PM': how a person says it."""
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def describe(moment: datetime, now: datetime) -> str:
    """Speakable local time: 'today at 2:30 PM', 'Tuesday 15 September at 9:00 AM'."""
    moment = moment.astimezone(now.tzinfo)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    days = (moment.date() - now.date()).days
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B')} at {clock}"


def to_utc_iso(moment: datetime) -> str:
    """Canonical storage form. One format everywhere, so equal instants compare equal."""
    if moment.tzinfo is None:
        raise ValueError("refusing to store a naive datetime")
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
