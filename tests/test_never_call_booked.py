"""A patient who already has an appointment is never called again."""

import asyncio
import unittest
from unittest import mock

import booking
import callback_queue
import config
import db
from scheduling import to_utc_iso
from tests.helpers import TempDatabase, at

NOW = at(13, 14)  # Sunday afternoon; the seed's first open day is Monday 14th


class Callable(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()
        self.patient = self.t.patient("p-001")

    def tearDown(self):
        self.t.close()

    def book(self):
        out = booking.request_appointment(self.t.conn, patient=self.patient, now=NOW, day="monday", time="11:00")
        self.assertEqual(out.status, "booked")
        return out

    def callable_ids(self):
        return [p["id"] for p in db.list_callable_patients(self.t.conn, to_utc_iso(NOW))]

    def test_a_booked_patient_is_not_callable(self):
        self.assertIn("p-001", self.callable_ids())
        self.book()
        self.assertNotIn("p-001", self.callable_ids())
        self.assertIn("p-002", self.callable_ids())

    def test_a_past_appointment_does_not_block_a_new_call(self):
        self.book()
        after_it = at(15, 9)
        self.assertIn("p-001", [p["id"] for p in db.list_callable_patients(self.t.conn, to_utc_iso(after_it))])

    def test_booking_clears_queued_callbacks(self):
        callback_queue.schedule(self.t.conn, patient=self.patient, now=NOW, phrase="later", in_minutes=120)
        self.book()
        pending = self.t.conn.execute(
            "SELECT COUNT(*) FROM callbacks WHERE patient_id = ? AND status = 'pending'", ("p-001",)).fetchone()[0]
        self.assertEqual(pending, 0)

    def test_no_retry_is_queued_for_a_booked_patient(self):
        self.book()
        decided = callback_queue.schedule_retry(self.t.conn, patient=self.patient, now=NOW, reason="no_answer")
        self.assertFalse(decided["retried"])
        self.assertIn("appointment", decided["why_not"])

    def test_due_callbacks_skip_booked_patients(self):
        callback_queue.schedule(self.t.conn, patient=self.patient, now=NOW, phrase="later", in_minutes=30)
        # Booked by another route after the callback was queued.
        self.t.conn.execute(
            "INSERT INTO appointments (reference, patient_id, doctor_id, slot_start_utc, status, notes, created_utc)"
            " VALUES ('ADT-OTHER', 'p-001', ?, ?, 'booked', '', ?)",
            (config.DOCTOR["id"], to_utc_iso(at(15, 9)), to_utc_iso(NOW)))
        self.assertEqual(db.due_callbacks(self.t.conn, to_utc_iso(at(14, 12))), [])

    def test_a_second_booking_in_one_call_is_refused(self):
        self.book()
        out = booking.request_appointment(self.t.conn, patient=self.patient, now=NOW, day="tuesday", time="10:00")
        self.assertEqual(out.status, "has_existing")

    def test_dispatch_refuses_a_booked_patient(self):
        import dispatch_outbound
        from datetime import timedelta
        from livekit import api
        # Booked relative to the real clock, since the dispatcher checks "now".
        tomorrow = to_utc_iso(db.utc_now() + timedelta(days=1))
        self.t.conn.execute(
            "INSERT INTO appointments (reference, patient_id, doctor_id, slot_start_utc, status, notes, created_utc)"
            " VALUES ('ADT-SOON', 'p-001', ?, ?, 'booked', '', ?)",
            (config.DOCTOR["id"], tomorrow, to_utc_iso(db.utc_now())))
        # Any attempt to reach LiveKit fails the test instead of placing a call.
        with mock.patch.object(config, "DB_PATH", self.t.path), \
                mock.patch.object(api, "LiveKitAPI", side_effect=AssertionError("tried to dispatch a call")):
            code = asyncio.run(dispatch_outbound.dispatch("p-001", None, browser=True))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
