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


class OfferMustBeAnswered(unittest.TestCase):
    """A slot the agent offered is only bookable after the patient has spoken since."""

    def _ctx(self, *user_lines):
        from types import SimpleNamespace
        items = [SimpleNamespace(type="message", role="user", text_content=t) for t in user_lines]
        return SimpleNamespace(session=SimpleNamespace(history=SimpleNamespace(items=items)))

    def test_offer_then_book_in_same_breath_is_not_trusted(self):
        import agent
        from tests.helpers import at
        a = agent.HealthcareAgent({"id": "p", "name": "Priya Sharma", "pronouns": "she/her",
                                   "hba1c": 8.2, "blood_glucose": 186})
        ctx = self._ctx("Yes, it's Priya", "Morning")
        a._remember_offered(ctx, [at(14, 11, 30)])
        self.assertFalse(a._patient_answered_offer(ctx, at(14, 11, 30)))
        self.assertTrue(a._patient_answered_offer(self._ctx("Yes, it's Priya", "Morning", "Yes"), at(14, 11, 30)))

    def test_same_time_on_another_day_trusts_the_offered_clock(self):
        import agent
        from tests.helpers import at
        a = agent.HealthcareAgent({"id": "p", "name": "Priya Sharma", "pronouns": "she/her",
                                   "hba1c": 8.2, "blood_glucose": 186})
        a._remember_offered(self._ctx("Yes"), [at(14, 9)])
        ctx = self._ctx("Yes", "Can we do Friday at that time instead?")
        self.assertTrue(agent.day_grounded("friday", agent._user_text(ctx)))
        self.assertTrue(a._offered_clock_answered(ctx, at(18, 9)))
        self.assertFalse(a._offered_clock_answered(ctx, at(18, 10)))
