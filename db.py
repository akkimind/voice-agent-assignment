"""SQLite storage for patients, the clinic calendar and the callback queue.

Every SQL statement in the project lives in this module, so moving to Postgres
later means rewriting one file.

Correctness is enforced by the database, not only by application checks:
- a partial unique index forbids two active bookings for one doctor and slot,
- another forbids two pending or dialing callbacks in the same callback slot,
- another allows at most one pending callback per patient.
Application code checks first so it can offer alternatives, but if two calls
race for the same slot the second INSERT fails regardless.

Several agent worker processes share the file. WAL mode lets readers proceed
during a write, and `BEGIN IMMEDIATE` takes the write lock before reading, so a
check-then-insert cannot interleave with another process.

Run directly to create or reset the database:
    python db.py            create tables and seed if empty
    python db.py --reset    delete the database and reseed
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import config
import scheduling

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    pronouns      TEXT,
    phone         TEXT NOT NULL,          -- not unique: families share numbers
    timezone      TEXT NOT NULL,
    hba1c         REAL,
    blood_glucose REAL,
    last_visit    TEXT,
    is_demo_seed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS doctors (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    reference      TEXT PRIMARY KEY,
    patient_id     TEXT NOT NULL REFERENCES patients(id),
    doctor_id      TEXT NOT NULL REFERENCES doctors(id),
    slot_start_utc TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('booked', 'cancelled')),
    notes          TEXT NOT NULL DEFAULT '',
    source_room    TEXT NOT NULL DEFAULT '',
    created_utc    TEXT NOT NULL,
    cancelled_utc  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_doctor_slot
    ON appointments (doctor_id, slot_start_utc) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS appointments_by_patient
    ON appointments (patient_id, status, slot_start_utc);

CREATE TABLE IF NOT EXISTS callbacks (
    reference      TEXT PRIMARY KEY,
    patient_id     TEXT NOT NULL REFERENCES patients(id),
    due_utc        TEXT NOT NULL,
    requested_utc  TEXT NOT NULL,
    timezone       TEXT NOT NULL,
    phrase         TEXT NOT NULL,
    requested_by   TEXT NOT NULL,
    moved_because  TEXT NOT NULL DEFAULT '[]',   -- JSON list of reasons
    status         TEXT NOT NULL
                   CHECK (status IN ('pending', 'dialing', 'done', 'failed', 'superseded')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    superseded_by  TEXT,
    source_room    TEXT NOT NULL DEFAULT '',
    created_utc    TEXT NOT NULL
);
-- One outbound call per callback slot, because we dial one call at a time.
CREATE UNIQUE INDEX IF NOT EXISTS one_call_per_callback_slot
    ON callbacks (due_utc) WHERE status IN ('pending', 'dialing');
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_callback_per_patient
    ON callbacks (patient_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS callbacks_by_due
    ON callbacks (status, due_utc);
"""

# Other patients whose bookings occupy the calendar, so the demo can show the
# "that slot is taken" path. They have no phone number and are never dialed.
_DEMO_PATIENTS = [
    {"id": "demo-01", "name": "Rohan Verma"},
    {"id": "demo-02", "name": "Meera Nair"},
    {"id": "demo-03", "name": "Sanjay Kulkarni"},
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- Connections ---------------------------------------------------------------

def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A connection that is always closed."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Take the write lock up front so check-then-write cannot race another process."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- Setup ---------------------------------------------------------------------

def _next_open_days(start: datetime, count: int) -> list[datetime]:
    days, cursor = [], start
    while len(days) < count:
        cursor += timedelta(days=1)
        if cursor.weekday() in config.CLINIC_OPEN_WEEKDAYS:
            days.append(cursor)
    return days


def init_db(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Create tables, upsert seed patients and the doctor, seed demo bookings once."""
    conn.executescript(SCHEMA)
    with transaction(conn):
        for p in config.load_seed_patients():
            conn.execute(
                """INSERT INTO patients (id, name, pronouns, phone, timezone, hba1c, blood_glucose, last_visit)
                   VALUES (:id, :name, :pronouns, :phone, :timezone, :hba1c, :blood_glucose, :last_visit)
                   ON CONFLICT (id) DO UPDATE SET
                     name = excluded.name, pronouns = excluded.pronouns, phone = excluded.phone,
                     timezone = excluded.timezone, hba1c = excluded.hba1c,
                     blood_glucose = excluded.blood_glucose, last_visit = excluded.last_visit""",
                {**{"pronouns": None, "last_visit": None}, **p},
            )
        for d in _DEMO_PATIENTS:
            conn.execute(
                """INSERT OR IGNORE INTO patients (id, name, phone, timezone, is_demo_seed)
                   VALUES (?, ?, '', ?, 1)""",
                (d["id"], d["name"], config.CLINIC_TIMEZONE),
            )
        conn.execute("INSERT OR IGNORE INTO doctors (id, name) VALUES (?, ?)",
                     (config.DOCTOR["id"], config.DOCTOR["name"]))

        already = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        if already:
            return
        clinic_now = (now or utc_now()).astimezone(ZoneInfo(config.CLINIC_TIMEZONE))
        first, second = _next_open_days(clinic_now, 2)
        taken = [(first, 10, 0), (first, 16, 0), (first, 16, 30), (second, 11, 0)]
        for (day, hour, minute), demo in zip(taken, _DEMO_PATIENTS * 2):
            slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            conn.execute(
                """INSERT INTO appointments (reference, patient_id, doctor_id, slot_start_utc, status, notes, created_utc)
                   VALUES (?, ?, ?, ?, 'booked', 'demo seed', ?)""",
                (f"SEED-{slot:%m%d%H%M}", demo["id"], config.DOCTOR["id"],
                 scheduling.to_utc_iso(slot), scheduling.to_utc_iso(utc_now())),
            )


# --- Patients ------------------------------------------------------------------

def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def get_patient(conn: sqlite3.Connection, patient_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if row is None:
        raise KeyError(f"no patient with id {patient_id!r}")
    return dict(row)


# A patient with an upcoming booked appointment has nothing to be called about.
_NOT_BOOKED = """NOT EXISTS (SELECT 1 FROM appointments a
                             WHERE a.patient_id = {alias}.id AND a.status = 'booked'
                             AND a.slot_start_utc > ?)"""


def list_callable_patients(conn: sqlite3.Connection, now_utc: str | None = None) -> list[dict[str, Any]]:
    """Real patients who may be called: demo calendar fillers are excluded, and so
    is anyone who already has an upcoming appointment."""
    now_utc = now_utc or scheduling.to_utc_iso(utc_now())
    rows = conn.execute(
        f"SELECT * FROM patients p WHERE is_demo_seed = 0 AND {_NOT_BOOKED.format(alias='p')} ORDER BY id",
        (now_utc,)).fetchall()
    return [dict(r) for r in rows]


def is_booked(conn: sqlite3.Connection, patient_id: str, now_utc: str | None = None) -> bool:
    return upcoming_appointment(conn, patient_id, now_utc or scheduling.to_utc_iso(utc_now())) is not None


# --- Appointments --------------------------------------------------------------

def booked_slots(conn: sqlite3.Connection, doctor_id: str, start_utc: str, end_utc: str) -> set[str]:
    rows = conn.execute(
        """SELECT slot_start_utc FROM appointments
           WHERE doctor_id = ? AND status = 'booked' AND slot_start_utc >= ? AND slot_start_utc < ?""",
        (doctor_id, start_utc, end_utc),
    ).fetchall()
    return {r[0] for r in rows}


def slot_is_booked(conn: sqlite3.Connection, doctor_id: str, slot_utc: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM appointments WHERE doctor_id = ? AND slot_start_utc = ? AND status = 'booked'",
        (doctor_id, slot_utc),
    ).fetchone() is not None


def upcoming_appointment(conn: sqlite3.Connection, patient_id: str, now_utc: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM appointments
           WHERE patient_id = ? AND status = 'booked' AND slot_start_utc > ?
           ORDER BY slot_start_utc LIMIT 1""",
        (patient_id, now_utc),
    ).fetchone()
    return _row(row)


def insert_appointment(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Raises sqlite3.IntegrityError if the slot was taken concurrently."""
    conn.execute(
        """INSERT INTO appointments (reference, patient_id, doctor_id, slot_start_utc, status, notes, source_room, created_utc)
           VALUES (:reference, :patient_id, :doctor_id, :slot_start_utc, 'booked', :notes, :source_room, :created_utc)""",
        record,
    )


def cancel_appointment(conn: sqlite3.Connection, reference: str) -> None:
    conn.execute(
        "UPDATE appointments SET status = 'cancelled', cancelled_utc = ? WHERE reference = ?",
        (scheduling.to_utc_iso(utc_now()), reference),
    )


# --- Callbacks -----------------------------------------------------------------

def supersede_pending_callbacks(conn: sqlite3.Connection, patient_id: str, new_reference: str) -> None:
    conn.execute(
        "UPDATE callbacks SET status = 'superseded', superseded_by = ? WHERE patient_id = ? AND status = 'pending'",
        (new_reference, patient_id),
    )


def callback_slot_taken(conn: sqlite3.Connection, due_utc: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM callbacks WHERE due_utc = ? AND status IN ('pending', 'dialing')", (due_utc,)
    ).fetchone() is not None


def insert_callback(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO callbacks (reference, patient_id, due_utc, requested_utc, timezone, phrase,
                                  requested_by, moved_because, status, source_room, created_utc)
           VALUES (:reference, :patient_id, :due_utc, :requested_utc, :timezone, :phrase,
                   :requested_by, :moved_because, 'pending', :source_room, :created_utc)""",
        {**record, "moved_because": json.dumps(record["moved_because"])},
    )


def due_callbacks(conn: sqlite3.Connection, now_utc: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT c.*, p.name AS patient_name, p.phone FROM callbacks c
           JOIN patients p ON p.id = c.patient_id
           WHERE c.status = 'pending' AND c.due_utc <= ?
             AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id = p.id
                             AND a.status = 'booked' AND a.slot_start_utc > ?)
           ORDER BY c.due_utc""",
        (now_utc, now_utc),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["moved_because"] = json.loads(d["moved_because"])
        out.append(d)
    return out


def get_callback(conn: sqlite3.Connection, reference: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM callbacks WHERE reference = ?", (reference,)).fetchone()
    return _row(row)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{config.DB_PATH}{suffix}").unlink(missing_ok=True)
    with session() as conn:
        init_db(conn)
        n_p = conn.execute("SELECT COUNT(*) FROM patients WHERE is_demo_seed = 0").fetchone()[0]
        n_a = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'booked'").fetchone()[0]
    print(f"database ready at {config.DB_PATH}: {n_p} patients, {n_a} booked appointments")
