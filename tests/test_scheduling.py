"""Pure time helpers: structured request -> datetime, grids, speech."""

import unittest
from datetime import datetime

import scheduling as s
from tests.helpers import at


class RequestedDatetime(unittest.TestCase):
    def test_relative_minutes(self):
        self.assertEqual(s.requested_datetime(at(13, 14), in_minutes=90), at(13, 15, 30))

    def test_nothing_given_returns_none(self):
        self.assertIsNone(s.requested_datetime(at(13, 14)))

    def test_part_of_day(self):
        self.assertEqual(s.requested_datetime(at(13, 14), day="tomorrow", part_of_day="evening"), at(14, 18))

    def test_day_alone_means_morning(self):
        self.assertEqual(s.requested_datetime(at(13, 14), day="tomorrow"), at(14, 10))

    def test_time_already_passed_today_means_tomorrow(self):
        self.assertEqual(s.requested_datetime(at(13, 18), time="16:00"), at(14, 16))

    def test_weekday_is_next_occurrence(self):
        self.assertEqual(s.requested_datetime(at(13, 14), day="monday", time="11:00"), at(14, 11))

    def test_same_weekday_means_next_week(self):
        self.assertEqual(s.requested_datetime(at(13, 9), day="sunday", time="15:00"), at(20, 15))

    def test_day_after_tomorrow(self):
        self.assertEqual(s.requested_datetime(at(13, 14), day="day_after_tomorrow", time="09:30"), at(15, 9, 30))

    def test_bad_clock_rejected(self):
        with self.assertRaises(s.SchedulingError):
            s.requested_datetime(at(13, 14), time="5pm")

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            s.requested_datetime(datetime(2026, 9, 13, 14), in_minutes=5)


class Grid(unittest.TestCase):
    def test_ceil_keeps_boundary(self):
        self.assertEqual(s.ceil_to_grid(at(13, 14, 30), 10), at(13, 14, 30))

    def test_ceil_rounds_up(self):
        self.assertEqual(s.ceil_to_grid(at(13, 14, 31), 10), at(13, 14, 40))
        self.assertEqual(s.ceil_to_grid(at(13, 14, 55), 30), at(13, 15))

    def test_on_grid(self):
        self.assertTrue(s.on_grid(at(13, 9, 30), 30))
        self.assertFalse(s.on_grid(at(13, 9, 10), 30))


class Speech(unittest.TestCase):
    def test_describe(self):
        now = at(13, 14)
        self.assertEqual(s.describe(at(13, 14, 30), now), "today at 2:30 PM")
        self.assertEqual(s.describe(at(14, 9), now), "tomorrow at 9:00 AM")
        self.assertEqual(s.describe(at(15, 18), now), "Tuesday 15 September at 6:00 PM")

    def test_hour_phrase(self):
        self.assertEqual([s.hour_phrase(h) for h in (0, 9, 12, 20)], ["12 AM", "9 AM", "12 PM", "8 PM"])

    def test_storage_form_is_canonical_utc(self):
        self.assertEqual(s.to_utc_iso(at(13, 14)), "2026-09-13T08:30:00+00:00")
        with self.assertRaises(ValueError):
            s.to_utc_iso(datetime(2026, 9, 13, 14))


if __name__ == "__main__":
    unittest.main()
