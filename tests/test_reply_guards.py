import unittest

from livekit.agents import llm

from agent import (PREFERENCE_WORDS, _SPOKEN_CLOCK, DotCollapser, ReplyCutter, TimeGate, diagnoses_condition,
                   known_times, normalize_day, normalize_part_of_day, slot_grounded, tool_results_this_turn)


def run(pieces):
    cutter = ReplyCutter()
    out = "".join(cutter.feed(p) for p in pieces) + cutter.finish()
    return out, cutter


class InventedReply(unittest.TestCase):
    def test_cuts_answer_written_after_question(self):
        out, cutter = run(["Tomorrow at 10:30 AM is free. Would you like", " to confirm?Yes, that", " works for me."])
        self.assertEqual(out, "Tomorrow at 10:30 AM is free. Would you like to confirm?")
        self.assertTrue(cutter.cut)

    def test_keeps_normal_text_after_question(self):
        text = "Does 9 AM work? If not, I can look for another time."
        self.assertEqual(run([text])[0], text)

    def test_keeps_short_question_at_end(self):
        self.assertEqual(run(["Does that work for you?"])[0], "Does that work for you?")

    def test_second_question_is_checked_too(self):
        out, cutter = run(["Is this Priya? I am calling from the clinic. Is now okay? No, not really, sorry."])
        self.assertEqual(out, "Is this Priya? I am calling from the clinic. Is now okay?")
        self.assertTrue(cutter.cut)


class MinutesMustMatch(unittest.TestCase):
    def test_hour_alone_does_not_ground_half_past(self):
        self.assertFalse(slot_grounded("tomorrow", "10:30", None, "Tomorrow at 10 AM."))

    def test_half_past_spoken(self):
        self.assertTrue(slot_grounded("tomorrow", "10:30", None, "tomorrow at 10:30"))
        self.assertTrue(slot_grounded("tomorrow", "10:30", None, "tomorrow half past ten"))
        self.assertTrue(slot_grounded("tomorrow", "10:30", None, "tomorrow ten thirty"))


class DayAfterOffered(unittest.TestCase):
    def setUp(self):
        import booking, config
        from agent import HealthcareAgent
        from datetime import timedelta
        self.agent = HealthcareAgent(config.load_seed_patients()[0])
        self.tomorrow_9 = (booking.clinic_now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    def test_spoken_forms_normalise(self):
        self.assertEqual(normalize_day("Day After Offered"), "day_after_offered")

    def test_adds_one_day_and_keeps_offered_time(self):
        self.agent._last_offered = self.tomorrow_9
        day, time, problem = self.agent._day_after_offered("day_after_offered", None)
        self.assertIsNone(problem)
        self.assertEqual(time, "09:00")
        self.assertEqual(day, ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][
            (self.tomorrow_9.weekday() + 1) % 7])

    def test_repeat_refers_to_the_day_after_offer_itself(self):
        self.agent._last_offered = self.tomorrow_9
        self.agent._last_offer_was_day_after = True
        self.assertEqual(self.agent._day_after_offered("day_after_offered", None)[0], "tomorrow")

    def test_nothing_offered_yet(self):
        self.assertIsNotNone(self.agent._day_after_offered("day_after_offered", None)[2])


def collapse(pieces):
    d = DotCollapser()
    return "".join(d.feed(p) for p in pieces) + d.finish()


class Dots(unittest.TestCase):
    def test_runs_become_a_space(self):
        self.assertEqual(collapse(["for you?....", "........When would"]), "for you? When would")
        self.assertEqual(collapse(["Well\u2026 okay"]), "Well  okay")

    def test_normal_punctuation_kept(self):
        self.assertEqual(collapse(["It is 9.30. Dr. Iyer."]), "It is 9.30. Dr. Iyer.")

    def test_trailing_run_dropped(self):
        self.assertEqual(collapse(["Thank you..."]), "Thank you")


class PreferenceBeforeSearch(unittest.TestCase):
    def test_agreeing_to_book_is_not_a_preference(self):
        for text in ("Okay, yes, I'd like to see the doctor.", "Sure, let's do it.", "Yes please"):
            self.assertIsNone(PREFERENCE_WORDS.search(text), text)

    def test_preferences(self):
        for text in ("Whatever is earliest, you pick.", "Can we do the next day instead?", "Friday",
                     "tomorrow morning", "any time is fine", "it doesn't matter", "at 3"):
            self.assertTrue(PREFERENCE_WORDS.search(text), text)


def gate(allowed, pieces):
    g = TimeGate(allowed)
    return "".join(g.feed(p) for p in pieces) + g.finish(), g


class SpokenTimes(unittest.TestCase):
    def test_known_times_from_tool_output_user_and_hours(self):
        ctx = llm.ChatContext()
        ctx.add_message(role="user", content="Friday at 3 please")
        ctx.items.append(llm.FunctionCallOutput(call_id="c1", name="request_callback", is_error=False,
                                                output="Scheduled for tomorrow at 9:30 AM, not today at 7:52 PM."))
        times = known_times(ctx)
        for t in ("9:30", "7:52", "3:00", "9:00", "5:00"):
            self.assertIn(t, times)
        self.assertNotIn("10:45", times)

    def test_blocks_sentence_with_unknown_time(self):
        out, g = gate({"9:00"}, ["We'll call you back in about an hour, ", "around 10:45 AM. Does that work?"])
        self.assertEqual(out, "")
        self.assertIn("10:45", g.blocked)

    def test_releases_known_times_and_stops_at_first_unknown(self):
        out, g = gate({"9:00"}, ["Tomorrow at 9:00 AM is free. ", "Or 2:30 PM. Which suits you?"])
        self.assertEqual(out, "Tomorrow at 9:00 AM is free.")
        self.assertIn("2:30", g.blocked)

    def test_text_without_times_passes(self):
        text = "Your HbA1c is 8.2%. Would you like to book? Dr. Iyer can see you."
        self.assertEqual(gate(set(), [text])[0], text)


class CutterDecidesEarly(unittest.TestCase):
    def test_finish_judges_short_held_text(self):
        c = ReplyCutter()
        self.assertEqual(c.feed("Does 9 AM work?Yes"), "Does 9 AM work?")
        self.assertTrue(c.holding)
        self.assertEqual(c.finish(), "")
        self.assertTrue(c.cut)


class Diagnosis(unittest.TestCase):
    def test_claims_blocked(self):
        for s in ("It is a sign of diabetes that your doctor can discuss.", "A result of 8.2% suggests diabetes.",
                  "This means you have prediabetes."):
            self.assertTrue(diagnoses_condition(s), s)

    def test_denials_and_deferrals_pass(self):
        for s in ("That does not mean you have diabetes.", "Only your doctor can say whether it means you have diabetes.",
                  "The numbers are above the usual range."):
            self.assertFalse(diagnoses_condition(s), s)

    def test_gate_blocks_diagnosis(self):
        out, g = gate(set(), ["Your HbA1c is 8.2%. It is a sign of diabetes. Shall we book?"])
        self.assertEqual(out, "Your HbA1c is 8.2%.")
        self.assertEqual(g.reason, "diagnosis")


class SpokenDays(unittest.TestCase):
    def test_unknown_day_blocked(self):
        g = TimeGate({"10:00"}, {"tuesday", "wednesday"})
        self.assertEqual(g.feed("Would you like a callback on Monday morning at 10:00 AM? ") + g.finish(), "")
        self.assertEqual(g.reason, "day")

    def test_clinic_week_and_known_days_pass(self):
        g = TimeGate(set(), {"tuesday", "wednesday", "thursday"})
        text = "We are open Monday to Saturday and closed on Sundays. Thursday works."
        self.assertEqual(g.feed(text) + g.finish(), text)

    def test_tomorrow_phrases_ground(self):
        from agent import day_grounded
        self.assertTrue(day_grounded("tomorrow", "next morning around 10"))
        self.assertFalse(day_grounded("tomorrow", "next day please"))


class SmallNormalisers(unittest.TestCase):
    def test_part_of_day(self):
        self.assertEqual(normalize_part_of_day("Evening"), "evening")
        self.assertIsNone(normalize_part_of_day("driving"))

    def test_spoken_clock(self):
        for s in ("tomorrow at 10 AM", "10:30 works", "Friday at 3"):
            self.assertTrue(_SPOKEN_CLOCK.search(s), s)
        self.assertIsNone(_SPOKEN_CLOCK.search("tomorrow morning please"))

    def test_tool_results_counted_since_patient_spoke(self):
        ctx = llm.ChatContext()
        ctx.items.append(llm.FunctionCallOutput(call_id="a", name="x", is_error=False, output="old"))
        ctx.add_message(role="user", content="hi")
        for i in range(2):
            ctx.items.append(llm.FunctionCallOutput(call_id=str(i), name="x", is_error=False, output="new"))
        self.assertEqual(tool_results_this_turn(ctx), 2)


if __name__ == "__main__":
    unittest.main()
