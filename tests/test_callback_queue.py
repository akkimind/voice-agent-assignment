"""Callback rules (pure) and the database-backed queue with 10-minute slots."""

import sqlite3
import unittest
from datetime import timezone

import callback_queue as cq
import db
from scheduling import SchedulingError, to_utc_iso
from tests.helpers import TempDatabase, at


class EarliestAllowed(unittest.TestCase):
    def test_inside_window_kept(self):
        _, slot, moved = cq.earliest_allowed(at(13, 14), in_minutes=60)
        self.assertEqual((slot, moved), (at(13, 15), []))

    def test_late_evening_moves_to_next_morning(self):
        requested, slot, moved = cq.earliest_allowed(at(13, 21, 50), in_minutes=60)
        self.assertEqual(requested, at(13, 22, 50))
        self.assertEqual(slot, at(14, 9))
        self.assertEqual(moved, [cq.WINDOW_REASON])

    def test_minimum_lead(self):
        _, slot, moved = cq.earliest_allowed(at(13, 14), in_minutes=2)
        self.assertEqual(slot, at(13, 14, 10))
        self.assertEqual(len(moved), 1)

    def test_lead_and_grid_can_push_past_window(self):
        _, slot, moved = cq.earliest_allowed(at(13, 19, 55), in_minutes=1)
        self.assertEqual(slot, at(14, 9))
        self.assertEqual(len(moved), 2)

    def test_off_grid_time_rounds_up_silently(self):
        _, slot, moved = cq.earliest_allowed(at(13, 14), in_minutes=63)
        self.assertEqual((slot, moved), (at(13, 15, 10), []))

    def test_no_preference_uses_default_delay(self):
        _, slot, _ = cq.earliest_allowed(at(13, 14), no_time_given=True)
        self.assertEqual(slot, at(13, 16))

    def test_no_time_at_all_rejected(self):
        with self.assertRaises(SchedulingError):
            cq.earliest_allowed(at(13, 14))

    def test_too_far_rejected(self):
        with self.assertRaises(SchedulingError):
            cq.earliest_allowed(at(13, 14), in_minutes=60 * 24 * 20)

    def test_never_earlier_than_requested(self):
        now = at(13, 6, 30)
        for minutes in (1, 15, 90, 300, 800, 1500, 4000):
            requested, slot, _ = cq.earliest_allowed(now, in_minutes=minutes)
            self.assertGreaterEqual(slot, requested, minutes)


class Queue(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()
        self.priya, self.arjun = self.t.patient("p-001"), self.t.patient("p-002")

    def tearDown(self):
        self.t.close()

    def _schedule(self, patient, now, **kw):
        return cq.schedule(self.t.conn, patient=patient, now=now, phrase="test", **kw)

    def test_second_caller_for_same_time_gets_next_slot(self):
        first = self._schedule(self.priya, at(13, 14), time="16:00")
        second = self._schedule(self.arjun, at(13, 14), time="16:00")
        self.assertEqual(first["scheduled"], at(13, 16))
        self.assertEqual(second["scheduled"], at(13, 16, 10))
        self.assertIn("already taken", " ".join(second["moved_because"]))

    def test_rerequest_by_same_patient_keeps_its_slot(self):
        first = self._schedule(self.priya, at(13, 14), time="16:00")
        again = self._schedule(self.priya, at(13, 14), time="16:00")
        self.assertEqual(again["scheduled"], at(13, 16))
        self.assertEqual(db.get_callback(self.t.conn, first["reference"])["status"], "superseded")

    def test_walking_past_window_end_moves_to_next_morning(self):
        self._schedule(self.priya, at(13, 14), time="19:50")
        second = self._schedule(self.arjun, at(13, 14), time="19:50")
        self.assertEqual(second["scheduled"], at(14, 9))
        self.assertIn(cq.WINDOW_REASON, second["moved_because"])

    def test_database_forbids_two_calls_in_one_slot(self):
        rec = self._schedule(self.priya, at(13, 14), time="16:00")
        dup = {**rec, "reference": "CB-DUPLICATE", "patient_id": "p-002"}
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert_callback(self.t.conn, dup)

    def test_due_returns_only_arrived_entries(self):
        self._schedule(self.priya, at(13, 14), time="15:00")
        self.assertEqual(cq.due(self.t.conn, at(13, 14, 59).astimezone(timezone.utc)), [])
        ready = cq.due(self.t.conn, at(13, 15).astimezone(timezone.utc))
        self.assertEqual([r["patient_id"] for r in ready], ["p-001"])
        self.assertEqual(ready[0]["due_utc"], to_utc_iso(at(13, 15)))


if __name__ == "__main__":
    unittest.main()
