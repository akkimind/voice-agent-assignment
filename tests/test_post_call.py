"""Post-call analysis: facts from the database, reconciliation, and failure handling."""

import asyncio
import json
import unittest
from unittest import mock

import booking
import callback_queue
import post_call
from post_call import CallRecord, Judgement
from tests.helpers import TempDatabase, at

ROOM = "call-room"


def history(*turns, calls=()):
    items = [{"type": "message", "role": role, "content": [text]} for role, text in turns]
    for name, output in calls:
        items.append({"type": "function_call", "name": name, "arguments": "{}"})
        items.append({"type": "function_call_output", "name": name, "output": output})
    return items


def judgement(**kw):
    base = {"outcome": "declined", "answered_by": "patient", "sentiment": "neutral", "summary": "x"}
    return Judgement(**{**base, **kw})


class FakeModel:
    """Stands in for the LLM: returns fixed text, or raises."""

    def __init__(self, text="", exc=None, delay=0.0):
        self.text, self.exc, self.delay = text, exc, delay


class Facts(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()
        self.priya = self.t.patient("p-001")

    def tearDown(self):
        self.t.close()

    def call(self, hist, **kw):
        return CallRecord(room=ROOM, patient=self.priya, history=hist, **kw)

    def test_booking_comes_from_the_database_not_the_transcript(self):
        # The agent claims a booking, but nothing is in the database.
        hist = history(("user", "yes book it"), ("assistant", "You're booked for Monday at 11."))
        f = post_call.facts(self.t.conn, self.call(hist))
        self.assertFalse(f["booking"]["booked"])

        booking.request_appointment(self.t.conn, patient=self.priya, now=at(13, 14), day="monday",
                                    time="11:00", source_room=ROOM)
        f = post_call.facts(self.t.conn, self.call(hist))
        self.assertTrue(f["booking"]["booked"])
        self.assertEqual(f["booking"]["slot_local"], "2026-09-14T11:00+05:30")

    def test_bookings_from_other_calls_are_ignored(self):
        booking.request_appointment(self.t.conn, patient=self.priya, now=at(13, 14), day="monday",
                                    time="11:00", source_room="another-room")
        self.assertFalse(post_call.facts(self.t.conn, self.call(history(("user", "hi"))))["booking"]["booked"])

    def test_callback_results_and_errors(self):
        callback_queue.schedule(self.t.conn, patient=self.priya, now=at(13, 14), phrase="in an hour",
                                in_minutes=60, source_room=ROOM)
        hist = history(("assistant", "Your HbA1c is 8.2%."), ("user", "call me later"),
                       calls=[("book_appointment", "Error parsing arguments for `book_appointment`")])
        f = post_call.facts(self.t.conn, self.call(hist, tool_results=[{"tool": "request_callback", "ok": False,
                                                                           "error": "no time"}]))
        self.assertTrue(f["callback"]["queued"])
        self.assertTrue(f["results_disclosed"])
        self.assertEqual(f["patient_turns"], 1)
        self.assertEqual(len(f["tool_errors"]), 2)


def known(booked=False, callback=False, disclosed=False, turns=3):
    return {"booking": {"booked": booked}, "callback": {"queued": callback},
            "results_disclosed": disclosed, "patient_turns": turns}


class Reconcile(unittest.TestCase):
    def test_database_booking_wins(self):
        outcome, overrides, _ = post_call.reconcile(known(booked=True), judgement(outcome="declined"))
        self.assertEqual(outcome, "booked")
        self.assertEqual(len(overrides), 1)

    def test_model_cannot_claim_a_booking(self):
        self.assertEqual(post_call.reconcile(known(), judgement(outcome="booked"))[0], "incomplete")
        self.assertEqual(post_call.reconcile(known(callback=True), judgement(outcome="booked"))[0],
                         "callback_requested")

    def test_someone_else_is_wrong_person_and_disclosure_is_flagged(self):
        outcome, _, flags = post_call.reconcile(known(disclosed=True, callback=True),
                                                judgement(outcome="callback_requested", answered_by="someone_else"))
        self.assertEqual(outcome, "wrong_person")
        self.assertIn("results_disclosed_to_non_patient", flags)

    def test_sip_only_outcomes_need_sip(self):
        self.assertEqual(post_call.reconcile(known(), judgement(outcome="voicemail"))[0], "incomplete")
        self.assertEqual(post_call.reconcile(known(), judgement(outcome="voicemail"), "sip")[0], "voicemail")

    def test_agreement_is_left_alone(self):
        self.assertEqual(post_call.reconcile(known(), judgement(outcome="declined")), ("declined", [], []))


class Analyze(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()
        self.priya = self.t.patient("p-001")
        self.hist = history(("assistant", "Hi"), ("user", "not interested, thanks"))

    def tearDown(self):
        self.t.close()

    def run_with(self, model):
        call = CallRecord(room=ROOM, patient=self.priya, history=self.hist)
        original = post_call._complete

        async def fake_complete(m, prompt):
            m.calls = getattr(m, "calls", 0) + 1
            if m.delay:
                await asyncio.sleep(m.delay)
            if m.exc:
                raise m.exc
            return m.text, [100, 20]

        post_call._complete = fake_complete
        try:
            return asyncio.run(post_call.analyze(call, conn=self.t.conn, model=model, timeout=0.2))
        finally:
            post_call._complete = original

    def test_valid_reply(self):
        reply = json.dumps({"outcome": "declined", "answered_by": "patient", "sentiment": "neutral",
                            "decline_reason": "not interested", "concerns": [], "summary": "Declined."})
        out = self.run_with(FakeModel(text=f"Here you go: {reply}"))
        self.assertEqual(out["outcome"], "declined")
        self.assertIsNone(out["error"])

    def test_model_failure_keeps_the_facts(self):
        model = FakeModel(exc=RuntimeError("provider down"))
        with mock.patch.object(post_call.asyncio, "sleep", new=mock.AsyncMock()):
            out = self.run_with(model)
        self.assertEqual(model.calls, 2)  # retried once
        self.assertEqual(out["outcome"], "unknown")
        self.assertIn("provider down", out["error"])
        self.assertFalse(out["booking_successful"])

    def test_timeout(self):
        out = self.run_with(FakeModel(text="{}", delay=1.0))
        self.assertIn("TimeoutError", out["error"])

    def test_nobody_spoke_needs_no_model(self):
        self.hist = history(("assistant", "Hi, may I speak with Priya Sharma, please?"))
        out = self.run_with(FakeModel(exc=AssertionError("model must not be called")))
        self.assertEqual(out["outcome"], "incomplete")
        self.assertIsNone(out["error"])


if __name__ == "__main__":
    unittest.main()
