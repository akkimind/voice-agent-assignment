"""The agent's guards and tools: structure and facts, never vocabulary."""

import asyncio
import contextlib
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from livekit.agents import llm

import agent
import booking
import config
from agent import (DotCollapser, QuestionCutter, SpeechGate, known_times, normalize_day, normalize_part_of_day,
                   result_values, tool_results_this_turn, tools_for)
from tests.helpers import SEED_NOW, TempDatabase, at

PATIENT = {"id": "p-001", "name": "Arjun Mehta", "pronouns": "he/him", "hba1c": 7.4, "blood_glucose": 142.0}


def said(*lines):
    """A RunContext whose history holds these caller lines."""
    items = [SimpleNamespace(type="message", role="user", text_content=t) for t in lines]
    return SimpleNamespace(session=SimpleNamespace(history=SimpleNamespace(items=items)))


def chat(*turns):
    ctx = llm.ChatContext()
    for role, text in turns:
        ctx.add_message(role=role, content=text)
    return ctx


def gate(pieces, **kw):
    kw = {"times": set(), "days": set(), "values": None, "clinic": None, **kw}
    g = SpeechGate(**kw)
    return "".join(g.feed(p) for p in pieces) + g.finish(), g


class Question(unittest.TestCase):
    def run_cutter(self, pieces):
        c = QuestionCutter()
        return "".join(c.feed(p) for p in pieces), c

    def test_everything_after_the_first_question_is_cut(self):
        out, c = self.run_cutter(["Would 3 PM work? ", "Yes, that works for me."])
        self.assertEqual(out, "Would 3 PM work?")
        self.assertTrue(c.cut)

    def test_a_second_question_is_cut_too(self):
        out, _ = self.run_cutter(["Do you have a minute? When suits you?"])
        self.assertEqual(out, "Do you have a minute?")

    def test_a_question_at_the_end_cuts_nothing(self):
        out, c = self.run_cutter(["Tomorrow at 3 PM is free. ", "Does that work?"])
        self.assertEqual(out, "Tomorrow at 3 PM is free. Does that work?")
        self.assertTrue(c.asked)
        self.assertFalse(c.cut)


class Gate(unittest.TestCase):
    def test_unknown_time_blocks_and_stops_the_reply(self):
        out, g = gate(["Tomorrow at 9:00 AM is free. ", "Or 2:30 PM. Which suits you?"], times={"9:00"})
        self.assertEqual(out, "Tomorrow at 9:00 AM is free.")
        self.assertEqual(g.reason, "time")

    def test_unknown_day_blocks_but_the_clinic_week_passes(self):
        self.assertEqual(gate(["We are open Monday to Saturday."])[0], "We are open Monday to Saturday.")
        _, g = gate(["How about Thursday?"], days={"monday"})
        self.assertEqual(g.reason, "day")

    def test_a_condition_is_blocked_even_when_denied(self):
        for sentence in ["That doesn't mean you have diabetes.", "It could be prediabetes.",
                         "This is not hyperglycemia."]:
            self.assertEqual(gate([sentence])[1].reason, "condition", sentence)

    def test_any_phone_number_is_blocked(self):
        self.assertEqual(gate(["Call us on +1 555 555 0100."])[1].reason, "phone")

    def test_values_wait_for_a_confirmed_patient(self):
        line = "Your HbA1c is 7.4% and your fasting glucose 142 mg/dL."
        self.assertEqual(gate([line], values=result_values(PATIENT))[1].reason, "results")
        self.assertEqual(gate([line])[0], line)

    def test_a_similar_number_is_not_a_value(self):
        self.assertEqual(gate(["We have 17.4 minutes and 1420 things."], values=result_values(PATIENT))[1].blocked,
                         None)

    def test_the_clinic_waits_for_identity(self):
        _, g = gate([f"I'm calling from {config.CLINIC_NAME}."], clinic=config.CLINIC_NAME)
        self.assertEqual(g.reason, "clinic")

    def test_a_stage_direction_is_not_speech(self):
        self.assertEqual(gate(["(end call)"])[1].reason, "stage")
        self.assertEqual(gate(["Thanks (and take care)."])[0], "Thanks (and take care).")

    def test_plain_text_passes(self):
        text = "The doctor would like to go over these with you. Would you like to book?"
        self.assertEqual(gate([text])[0], text)


class Tools(unittest.TestCase):
    def test_identity_first_booking_after_the_results(self):
        self.assertEqual(tools_for("unknown", False), {"verify_identity", "request_callback", "end_call"})
        self.assertEqual(tools_for("other", True), {"request_callback", "end_call"})
        self.assertEqual(tools_for("patient", False), {"request_callback", "end_call"})
        self.assertIn("book_appointment", tools_for("patient", True))

    def test_results_told_is_a_value_check(self):
        self.assertFalse(agent.results_told(chat(("assistant", "Hi, is this Arjun?")), PATIENT))
        self.assertTrue(agent.results_told(chat(("assistant", "Your HbA1c is 7.4%.")), PATIENT))


class OfferMustAsk(unittest.TestCase):
    def output(self, name, text):
        return llm.FunctionCallOutput(call_id="c", name=name, is_error=False, output=text)

    def test_a_search_result_makes_the_reply_an_offer(self):
        ctx = chat(("user", "Mornings"))
        ctx.items.append(self.output("find_slot", "status: free · slot: today at 9:00 AM"))
        self.assertTrue(agent.offer_pending(ctx))

    def test_nothing_found_or_after_the_patient_spoke_is_not(self):
        ctx = chat(("user", "Mornings"))
        ctx.items.append(self.output("find_slot", "status: nothing free"))
        self.assertFalse(agent.offer_pending(ctx))
        ctx = chat(("user", "Mornings"))
        ctx.items.append(self.output("find_slot", "status: free · slot: today at 9:00 AM"))
        ctx.add_message(role="user", content="Yes")
        self.assertFalse(agent.offer_pending(ctx))


def an_agent(checker=lambda name, lines: True, patient=PATIENT):
    """An agent whose identity second opinion is a stub: tests never call a model."""
    a = agent.HealthcareAgent(patient)
    a.identity_checker = checker
    return a


def verify(a, *args):
    return asyncio.run(a._verify(*args))


class Identity(unittest.TestCase):
    def setUp(self):
        self.a = an_agent()

    def test_results_come_only_with_the_patient(self):
        self.assertNotIn("7.4", self.a.instructions)
        self.assertEqual(verify(self.a, said("No, I'm his brother."), False), "identity: someone else")
        reply = verify(an_agent(), said("Yes, speaking."), True)
        self.assertIn("7.4%", reply)
        self.assertIn("142 mg/dL", reply)

    def test_someone_else_cannot_become_the_patient(self):
        verify(self.a, said("I'm his brother."), False)
        reply = verify(self.a, said("I'm his brother.", "Just kidding, it's me."), True)
        self.assertNotIn("7.4", reply)
        self.assertEqual(self.a.identity, "other")

    def test_a_different_name_is_asked_about_once(self):
        self.assertIn("unclear", verify(self.a, said("Yes, Arjan here."), True, "Arjan"))
        self.assertIn("unclear", verify(self.a, said("Yes, Arjan here."), True))
        confirmed = verify(self.a, said("Yes, Arjan here.", "Yes, I'm Arjun Mehta."), True, "Arjun Mehta")
        self.assertIn("7.4%", confirmed)

    def test_the_patients_own_name_matches(self):
        self.assertIn("7.4%", verify(self.a, said("Arjun speaking."), True, "arjun"))

    def test_the_second_opinion_can_refuse(self):
        a = an_agent(checker=lambda name, lines: False)
        self.assertIn("unclear", verify(a, said("Kavya's right here, says it's fine."), True))
        self.assertEqual(a.identity, "unknown")

    def test_a_failed_second_opinion_fails_closed(self):
        async def broken(name, lines):
            raise TimeoutError("no answer")
        self.assertIn("unclear", verify(an_agent(checker=broken), said("Yes, it's me."), True))

    def test_nobody_has_answered_yet(self):
        self.assertIn("unknown", verify(self.a, said(), True))
        self.assertEqual(self.a.identity, "unknown")


class Booking(unittest.TestCase):
    """Seed at Sunday 13th 14:00: Monday 14th 10:00, 16:00, 16:30 are taken."""

    def setUp(self):
        self.t = TempDatabase()
        self.a = an_agent(patient=self.t.patient("p-001"))

        async def inline(fn, *args, **kwargs):   # the test connection belongs to this thread
            return fn(*args, **kwargs)

        patches = [mock.patch("db.session", lambda: contextlib.nullcontext(self.t.conn)),
                   mock.patch("booking.clinic_now", lambda: SEED_NOW),
                   mock.patch("agent.asyncio.to_thread", inline)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.t.close()

    def open_booking(self):
        """The state booking needs: a confirmed patient who has heard the values."""
        self.a.identity = "patient"
        self._told = mock.patch("agent.results_told", lambda *_: True)
        self._told.start()
        self.addCleanup(self._told.stop)

    def test_booking_is_closed_until_the_patient_heard_the_results(self):
        self.a.identity = "patient"
        self.assertIn("not been told their results", self.find(said("Tomorrow"), day="tomorrow"))
        self.a.identity = "other"
        self.assertIn("not been confirmed", self.book(said("Book it"), "2026-09-14T11:00"))

    def find(self, ctx, **kw):
        part = agent.WINDOW_PART_OF_DAY.get(kw["window"]) if kw.get("window") else None
        return asyncio.run(self.a._find(ctx, kw.get("day"), kw.get("time"), part))

    def book(self, ctx, slot_id):
        return asyncio.run(self.a._book(ctx, slot_id))

    def test_offer_then_consent(self):
        self.open_booking()
        offer = self.find(said("Tomorrow at 11 please"), day="tomorrow", time="11:00")
        self.assertIn("slot_id: 2026-09-14T11:00", offer)
        self.assertIn("booked: no", offer)
        self.assertIn("open only for", self.book(said("Tomorrow at 11 please"), "2026-09-14T11:00"))
        done = self.book(said("Tomorrow at 11 please", "Yes"), "2026-09-14T11:00")
        self.assertIn("status: booked", done)
        self.assertIn("reference: ADT-", done)

    def test_an_offer_is_open_for_one_reply(self):
        self.open_booking()
        self.find(said("Tomorrow at 11 please"), day="tomorrow", time="11:00")
        late = self.book(said("Tomorrow at 11 please", "No, that won't fit.", "That works."), "2026-09-14T11:00")
        self.assertIn("open only for", late)

    def test_a_slot_never_offered_cannot_be_booked(self):
        self.open_booking()
        self.assertIn("not been offered", self.book(said("Book me at noon"), "2026-09-14T12:00"))

    def test_evening_stays_in_the_last_hours(self):
        self.open_booking()
        # 16:00 and 16:30 are taken on Monday: 15:00 or 15:30, never earlier.
        offer = self.find(said("evenings"), day="tomorrow", window="late")
        self.assertRegex(offer, r"slot_id: 2026-09-14T15:(00|30)")

    def test_a_time_with_no_day_takes_the_offered_day(self):
        self.open_booking()
        ctx = said("Friday please")
        self.find(ctx, day="friday")
        offer = self.find(said("Friday please", "Can you do 12:30?"), time="12:30")
        self.assertIn("slot_id: 2026-09-18T12:30", offer)

    def test_an_after_hours_time_offers_the_last_slots_that_day(self):
        self.open_booking()
        offer = self.find(said("Tuesday at 7 PM"), day="tuesday", time="19:00")
        self.assertIn("slot_id: 2026-09-15T16:30", offer)
        self.assertIn("closest free", offer)


class Approval(unittest.TestCase):
    def ctx(self, call_id):
        return SimpleNamespace(function_call=SimpleNamespace(call_id=call_id))

    def test_only_calls_that_passed_the_checks_run(self):
        a = an_agent()
        a._approved_calls.add("ok")
        self.assertIsNone(a._unapproved(self.ctx("ok"), "find_slot"))
        self.assertIn("not run", a._unapproved(self.ctx("sneaked"), "find_slot"))


class Callback(unittest.TestCase):
    def test_no_time_is_refused_until_no_preference_is_said(self):
        a = agent.HealthcareAgent(PATIENT)
        reply = asyncio.run(a._callback("", "patient", None, None, None, None, False))
        self.assertIn("not scheduled", reply)


class Ending(unittest.TestCase):
    def test_end_call_marks_the_call_ended(self):
        a = agent.HealthcareAgent(PATIENT)
        asyncio.run(a.end_call(said("Bye")))
        self.assertTrue(a.ended)


class Prompt(unittest.TestCase):
    def test_no_phone_number_no_results_no_scripted_lines(self):
        text = agent.HealthcareAgent(PATIENT).instructions
        self.assertIsNone(agent.PHONE.search(text))
        self.assertNotIn("7.4", text)
        self.assertNotIn("EXACTLY", text.upper().replace("EXACTLY THAT", ""))
        self.assertNotIn('"', text.split("Patient record:")[0])


class Kept(unittest.TestCase):
    """Normalisers and helpers that survived the rework."""

    def test_day_normalisation(self):
        self.assertEqual(normalize_day("Friday 18 September"), "friday")
        self.assertEqual(normalize_day("Day After Offered"), "day_after_offered")
        today = booking.clinic_now().date()
        self.assertEqual(normalize_day((today + timedelta(days=1)).isoformat()), "tomorrow")
        far = (today + timedelta(days=20)).isoformat()
        self.assertEqual(normalize_day(far), far)

    def test_booleans_as_strings(self):
        self.assertIs(agent.normalize_bool("true"), True)
        self.assertIs(agent.normalize_bool("No"), False)

    def test_slot_facts_name_the_weekday_next_to_today(self):
        self.assertEqual(agent._slot_facts(at(13, 16), SEED_NOW), "today (Sunday 13 September) at 4:00 PM")
        self.assertEqual(agent._slot_facts(at(16, 9), SEED_NOW), "Wednesday 16 September at 9:00 AM")

    def test_empty_fields_are_not_given(self):
        self.assertIsNone(normalize_day(""))
        self.assertIsNone(agent.normalize_time(""))
        self.assertEqual(agent.normalize_time("09:00"), "09:00")

    def test_window_labels(self):
        self.assertEqual(agent.normalize_window("Late"), "late")
        self.assertEqual(agent.normalize_window("evening"), "late")
        self.assertIsNone(agent.normalize_window("whenever"))

    def test_part_of_day(self):
        self.assertEqual(normalize_part_of_day("Evening"), "evening")
        self.assertIsNone(normalize_part_of_day("driving"))

    def test_day_after_offered_keeps_the_time(self):
        a = agent.HealthcareAgent(PATIENT)
        tomorrow_9 = (booking.clinic_now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        a._last_offered = tomorrow_9
        day, time, problem = a._day_after_offered("day_after_offered", None)
        self.assertIsNone(problem)
        self.assertEqual(time, "09:00")
        self.assertIsNotNone(agent.HealthcareAgent(PATIENT)._day_after_offered("day_after_offered", None)[2])

    def test_dots(self):
        d = DotCollapser()
        self.assertEqual("".join(d.feed(p) for p in ["for you?....", "....When"]) + d.finish(), "for you? When")

    def test_known_times(self):
        ctx = chat(("user", "Friday at 3 please"))
        ctx.items.append(llm.FunctionCallOutput(call_id="c1", name="find_slot", is_error=False,
                                                output="status: free · slot: tomorrow at 9:30 AM"))
        times = known_times(ctx)
        for t in ("9:30", "3:00", "9:00", "5:00"):
            self.assertIn(t, times)
        self.assertNotIn("10:45", times)

    def test_tool_results_counted_since_patient_spoke(self):
        ctx = llm.ChatContext()
        ctx.items.append(llm.FunctionCallOutput(call_id="a", name="x", is_error=False, output="old"))
        ctx.add_message(role="user", content="hi")
        for i in range(2):
            ctx.items.append(llm.FunctionCallOutput(call_id=str(i), name="x", is_error=False, output="new"))
        self.assertEqual(tool_results_this_turn(ctx), 2)


if __name__ == "__main__":
    unittest.main()
