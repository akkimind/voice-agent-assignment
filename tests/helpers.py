"""Shared fixtures: a throwaway database seeded at a fixed moment."""

import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import db

IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in September 2026, IST. 13 September 2026 is a Sunday."""
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


# Seeding at this moment books Monday 14th at 10:00, 16:00, 16:30 and Tuesday 15th at 11:00.
SEED_NOW = at(13, 14)


class TempDatabase:
    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "clinic.db"
        self.conn = db.connect(self.path)
        db.init_db(self.conn, now=SEED_NOW)

    def patient(self, patient_id: str) -> dict:
        return db.get_patient(self.conn, patient_id)

    def close(self):
        self.conn.close()
        self._dir.cleanup()
