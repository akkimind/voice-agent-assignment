"""Slot-based booking. The seed has Monday 14th 10:00, 16:00, 16:30 and Tuesday 15th 11:00 taken."""

import sqlite3
import unittest
from unittest import mock

import booking
import config
import db
from scheduling import to_utc_iso
from tests.helpers import TempDatabase, at

NOW = at(13, 14)  # Sunday afternoon


class Booking(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()
        self.priya, self.arjun = self.t.patient("p-001"), self.t.patient("p-002")

    def tearDown(self):
        self.t.close()

    def book(self, patient=None, now=NOW, **kw):
        return booking.request_appointment(self.t.conn, patient=patient or self.priya, now=now, **kw)

    def test_free_slot_is_booked(self):
        out = self.book(day="monday", time="11:00")
        self.assertEqual(out.status, "booked")
        self.assertTrue(db.slot_is_booked(self.t.conn, config.DOCTOR["id"], to_utc_iso(at(14, 11))))

    def test_taken_slot_offers_nearest_free_alternatives(self):
        out = self.book(day="monday", time="16:00")
        self.assertEqual(out.status, "unavailable")
        self.assertIn("already booked", out.reason)
        self.assertEqual(len(out.alternatives), config.ALTERNATIVE_SLOTS_OFFERED)
        self.assertIn(at(14, 15, 30), out.alternatives)
        self.assertNotIn(at(14, 16, 30), out.alternatives)
        self.assertEqual(out.alternatives, sorted(out.alternatives))

    def test_two_patients_cannot_take_one_slot(self):
        self.assertEqual(self.book(day="monday", time="11:00").status, "booked")
        self.assertEqual(self.book(patient=self.arjun, day="monday", time="11:00").status, "unavailable")

    def test_closed_day(self):
        out = self.book(day="sunday", time="11:00")
        self.assertEqual(out.status, "unavailable")
        self.assertIn("closed on Sundays", out.reason)
        self.assertTrue(out.alternatives)

    def test_outside_clinic_hours(self):
        out = self.book(day="monday", time="17:00")
        self.assertIn("9 AM to 5 PM", out.reason)

    def test_between_slots_is_not_rounded(self):
        out = self.book(day="monday", time="11:10")
        self.assertEqual(out.status, "unavailable")
        self.assertIn(at(14, 11), out.alternatives)
        self.assertIn(at(14, 11, 30), out.alternatives)

    def test_too_soon(self):
        out = self.book(now=at(14, 9, 40), day="today", time="10:30")
        self.assertIn("too soon", out.reason)

    def test_day_without_time_offers_free_times_on_that_day(self):
        out = self.book(day="monday", part_of_day="morning")
        self.assertEqual(out.status, "needs_time")
        self.assertIn(at(14, 9), out.alternatives)
        self.assertNotIn(at(14, 10), out.alternatives)

    def test_existing_appointment_is_not_silently_replaced(self):
        self.book(day="monday", time="11:00")
        out = self.book(day="tuesday", time="10:00")
        self.assertEqual(out.status, "has_existing")
        self.assertEqual(out.existing["slot_start_utc"], to_utc_iso(at(14, 11)))

    def test_replace_existing_moves_the_appointment(self):
        first = self.book(day="monday", time="11:00")
        out = self.book(day="tuesday", time="10:00", replace_existing=True)
        self.assertEqual(out.status, "booked")
        self.assertEqual(out.record["replaced"], first.record["reference"])
        self.assertFalse(db.slot_is_booked(self.t.conn, config.DOCTOR["id"], to_utc_iso(at(14, 11))))

    def test_same_slot_again_is_idempotent(self):
        self.book(day="monday", time="11:00")
        out = self.book(day="monday", time="11:00")
        self.assertEqual((out.status, out.reason), ("booked", "already booked for that time"))

    def test_database_forbids_double_booking(self):
        rec = self.book(day="monday", time="11:00").record
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert_appointment(self.t.conn, {**rec, "reference": "ADT-DUP", "patient_id": "p-002"})

    def test_lost_race_rolls_back_the_cancellation(self):
        # Priya holds Monday 11:00 and asks to move to Monday 16:00, which is taken.
        # Pretend the availability check missed it, as if another call booked it
        # between our check and our insert. The unique index must reject the insert,
        # and her original appointment must survive the rollback.
        self.book(day="monday", time="11:00")
        with mock.patch.object(db, "slot_is_booked", return_value=False):
            out = self.book(day="monday", time="16:00", replace_existing=True)
        self.assertEqual(out.status, "unavailable")
        self.assertIn("just booked", out.reason)
        self.assertTrue(db.slot_is_booked(self.t.conn, config.DOCTOR["id"], to_utc_iso(at(14, 11))))

    def test_available_slots_skip_taken_times(self):
        slots, reason = booking.available_slots(self.t.conn, now=NOW, day="monday", part_of_day="afternoon", limit=20)
        self.assertIsNone(reason)
        self.assertIn(at(14, 15, 30), slots)
        self.assertNotIn(at(14, 16), slots)
        self.assertNotIn(at(14, 16, 30), slots)


if __name__ == "__main__":
    unittest.main()


class Ranking(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()

    def tearDown(self):
        self.t.close()

    def test_closed_day_offers_similar_time_on_nearest_open_days(self):
        # Sunday 20th at 11:00 is closed. Saturday 19th and Monday 21st at 11:00 are
        # one day away at the same time of day, so they rank first.
        out = booking.request_appointment(self.t.conn, patient=self.t.patient("p-003"), now=NOW,
                                          day="sunday", time="11:00")
        self.assertIn(at(19, 11), out.alternatives)
        self.assertIn(at(21, 11), out.alternatives)
        self.assertTrue(all(9 <= s.hour <= 12 for s in out.alternatives), out.alternatives)

    def test_no_day_means_first_day_with_free_slots(self):
        slots, reason = booking.available_slots(self.t.conn, now=NOW, day=None, part_of_day=None)
        self.assertIsNone(reason)
        self.assertEqual(slots[0], at(14, 9))  # Sunday is closed, Monday 9:00 is free


class PreferenceFlow(unittest.TestCase):
    """Seed has Monday 14th 10:00 taken."""

    def setUp(self):
        self.t = TempDatabase()

    def tearDown(self):
        self.t.close()

    def book(self, **kw):
        return booking.request_appointment(self.t.conn, patient=self.t.patient("p-001"), now=NOW, **kw)

    def test_quarter_past_offers_the_slot_either_side(self):
        out = self.book(day="monday", time="11:15")
        self.assertEqual(out.status, "unavailable")
        self.assertEqual(out.alternatives, [at(14, 11), at(14, 11, 30)])

    def test_quarter_past_skips_a_taken_neighbour(self):
        self.assertEqual(self.book(day="monday", time="10:15").alternatives, [at(14, 10, 30)])

    def test_morning_ranges_group_consecutive_slots(self):
        slots, _ = booking.available_slots(self.t.conn, now=NOW, day="monday", part_of_day="morning", limit=None)
        self.assertEqual(booking.free_ranges(slots), [(at(14, 9), at(14, 9, 30)), (at(14, 10, 30), at(14, 11, 30))])

    def test_ranges_are_spoken_per_day(self):
        import agent
        text = agent._say_ranges([at(14, 9), at(14, 9, 30), at(14, 11, 30)], NOW)
        self.assertEqual(text, "tomorrow: from 9:00 AM to 9:30 AM, or at 11:30 AM")
