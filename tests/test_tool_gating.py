import unittest

from livekit.agents import llm

import config
from datetime import timedelta

import booking
from agent import booking_unlocked, normalize_day

PATIENT = config.load_seed_patients()[0]


def ctx(*turns: tuple[str, str]) -> llm.ChatContext:
    chat = llm.ChatContext()
    for role, text in turns:
        chat.add_message(role=role, content=text)
    return chat


class BookingToolGate(unittest.TestCase):
    def test_locked_during_greeting(self):
        self.assertFalse(booking_unlocked(ctx(
            ("assistant", f"Hi, may I speak with {PATIENT['name']}, please?"),
            ("user", "yes speaking"),
            ("assistant", "I'm Alex from Adit Health Clinic. Do you have a few minutes?"),
        ), PATIENT))

    def test_someone_else_asking_for_callback_stays_locked(self):
        self.assertFalse(booking_unlocked(ctx(
            ("user", "she's not here, call back at 5"),
        ), PATIENT))

    def test_unlocked_once_results_shared(self):
        self.assertTrue(booking_unlocked(ctx(
            ("user", "yes I have time"),
            ("assistant", f"Your HbA1c is {PATIENT['hba1c']} percent."),
        ), PATIENT))

    def test_unlocked_when_caller_asks_to_book(self):
        self.assertTrue(booking_unlocked(ctx(("user", "can I just book an appointment for Friday"),), PATIENT))


class DayNormalization(unittest.TestCase):
    def test_case_and_spaces(self):
        self.assertEqual(normalize_day("Friday"), "friday")
        self.assertEqual(normalize_day("Day After Tomorrow"), "day_after_tomorrow")
        self.assertEqual(normalize_day("next Monday"), "monday")

    def test_iso_dates_near_today(self):
        today = booking.clinic_now().date()
        self.assertEqual(normalize_day(today.isoformat()), "today")
        self.assertEqual(normalize_day((today + timedelta(days=1)).isoformat()), "tomorrow")
        three = today + timedelta(days=3)
        self.assertEqual(normalize_day(three.isoformat()), three.strftime("%A").lower())

    def test_wrong_year_uses_month_and_day(self):
        today = booking.clinic_now().date()
        three = today + timedelta(days=3)
        wrong = three.replace(year=three.year - 2).isoformat()
        self.assertEqual(normalize_day(wrong), three.strftime("%A").lower())

    def test_far_dates_and_non_strings_untouched(self):
        far = (booking.clinic_now().date() + timedelta(days=20)).isoformat()
        self.assertEqual(normalize_day(far), far)
        self.assertIsNone(normalize_day(None))


if __name__ == "__main__":
    unittest.main()
