"""The check that stops the agent booking a slot the patient never said."""

import unittest

from agent import slot_grounded


class SlotGrounded(unittest.TestCase):
    def test_invented_slot_rejected(self):
        self.assertFalse(slot_grounded("tuesday", "10:00", None, "Yes, this is Priya. Sure, let's book."))

    def test_stated_slot_accepted(self):
        self.assertTrue(slot_grounded("tuesday", "15:00", None, "Sure. Next Tuesday at 3pm."))

    def test_twenty_four_hour_value_matches_spoken_twelve_hour(self):
        self.assertTrue(slot_grounded("tomorrow", "17:00", None, "tomorrow at five"))

    def test_wrong_hour_rejected(self):
        self.assertFalse(slot_grounded("tuesday", "10:00", None, "Tuesday at 3pm"))

    def test_wrong_day_rejected(self):
        self.assertFalse(slot_grounded("wednesday", "15:00", None, "Tuesday at 3pm"))

    def test_day_after_tomorrow_needs_both_words(self):
        self.assertFalse(slot_grounded("day_after_tomorrow", "15:00", None, "tomorrow at 3"))
        self.assertTrue(slot_grounded("day_after_tomorrow", "15:00", None, "day after tomorrow at 3"))

    def test_part_of_day(self):
        self.assertTrue(slot_grounded("tomorrow", None, "morning", "tomorrow morning works"))

    def test_nothing_chosen_rejected(self):
        self.assertFalse(slot_grounded("tomorrow", None, None, "tomorrow"))


if __name__ == "__main__":
    unittest.main()
