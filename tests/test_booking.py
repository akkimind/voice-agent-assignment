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

    def test_taken_slot_offers_earliest_free_time_after_it(self):
        out = self.book(day="monday", time="16:00")
        self.assertEqual(out.status, "unavailable")
        self.assertIn("already booked", out.reason)
        # 16:30 is taken too and 17:00 is closing, so the next real option is Tuesday 9:00.
        self.assertEqual(out.alternatives, [at(15, 9)])

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
        self.assertEqual(out.alternatives, [at(14, 11, 30)])

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

    def test_earliest_skips_taken_times(self):
        start = booking.search_start(NOW, day="monday", time="16:00", part_of_day=None)
        self.assertEqual(booking.earliest_free_slot(self.t.conn, now=NOW, not_before=start), at(15, 9))


if __name__ == "__main__":
    unittest.main()


class Ranking(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()

    def tearDown(self):
        self.t.close()

    def test_closed_day_offers_earliest_after_it(self):
        out = booking.request_appointment(self.t.conn, patient=self.t.patient("p-003"), now=NOW,
                                          day="sunday", time="11:00")
        self.assertIn("closed on Sundays", out.reason)
        self.assertEqual(out.alternatives, [at(21, 9)])

    def test_no_preference_means_earliest_overall(self):
        start = booking.search_start(NOW, day=None, time=None, part_of_day=None)
        self.assertEqual(booking.earliest_free_slot(self.t.conn, now=NOW, not_before=start), at(14, 9))

    def test_day_only_means_earliest_that_day(self):
        start = booking.search_start(NOW, day="tuesday", time=None, part_of_day=None)
        self.assertEqual(booking.earliest_free_slot(self.t.conn, now=NOW, not_before=start), at(15, 9))

    def test_part_of_day_starts_the_search_there(self):
        start = booking.search_start(NOW, day="monday", time=None, part_of_day="afternoon")
        self.assertEqual(booking.earliest_free_slot(self.t.conn, now=NOW, not_before=start), at(14, 12))


class EarliestFlow(unittest.TestCase):
    """Seed has Monday 14th 10:00 taken."""

    def setUp(self):
        self.t = TempDatabase()

    def tearDown(self):
        self.t.close()

    def book(self, **kw):
        return booking.request_appointment(self.t.conn, patient=self.t.patient("p-001"), now=NOW, **kw)

    def test_quarter_past_offers_the_next_slot(self):
        out = self.book(day="monday", time="11:15")
        self.assertEqual(out.status, "unavailable")
        self.assertEqual(out.alternatives, [at(14, 11, 30)])

    def test_quarter_past_skips_a_taken_neighbour(self):
        self.assertEqual(self.book(day="monday", time="10:15").alternatives, [at(14, 10, 30)])

    def test_day_without_time_offers_one_earliest_slot(self):
        out = self.book(day="monday", part_of_day="afternoon")
        self.assertEqual(out.status, "needs_time")
        self.assertEqual(out.alternatives, [at(14, 12)])

    def test_offer_says_when_the_day_was_full(self):
        import agent
        text = agent._offer(at(15, 9), at(14, 16), NOW, "monday")
        self.assertIn("Nothing free on that day", text)
        self.assertIn("9:00 AM", text)
