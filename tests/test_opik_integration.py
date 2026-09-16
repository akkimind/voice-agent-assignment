"""What we send to Opik, checked without touching the network."""

import unittest
from datetime import datetime, timezone

import opik_integration as oi
from post_call import CallRecord


def at(seconds: int) -> str:
    return datetime(2026, 9, 16, 12, 0, seconds, tzinfo=timezone.utc).isoformat(timespec="milliseconds")


PATIENT = {"id": "p-001", "name": "Priya Sharma", "phone": "+919876543210", "hba1c": 8.2, "blood_glucose": 186}

LOG_ROWS = [
    {"event": "call_start", "at": at(0)},
    {"event": "user_turn_committed", "at": at(3), "text": "Yes, speaking."},
    {"event": "llm_request", "at": at(3), "turn": 1, "tool_names": ["request_callback"], "items": 4},
    {"event": "llm_response", "at": at(4), "turn": 1, "text": "Hi Priya.", "prompt_tokens": 1200,
     "completion_tokens": 30, "total_ms": 900},
    {"event": "tts_done", "at": at(5), "text": "Hi Priya."},
    {"event": "user_turn_committed", "at": at(9), "text": "Tomorrow at ten thirty."},
    {"event": "tool_call", "at": at(10), "seq": 1, "tool": "book_appointment",
     "args": {"day": "tomorrow", "time": "10:30"}},
    {"event": "tool_result", "at": at(11), "seq": 1, "tool": "book_appointment",
     "result": "Booked for tomorrow at 10:30 AM. Reference ADT-1.", "duration_ms": 12.5},
    {"event": "tts_done", "at": at(12), "text": "Booked, reference ADT-1."},
    {"event": "guard_ungrounded_time", "at": at(12), "sentence": "around 9 AM"},
]

ANALYSIS = {
    "version": 1, "outcome": "booked", "booking_successful": True, "model": "openai/gpt-oss-20b",
    "facts": {"booking": {"booked": True, "reference": "ADT-1", "slot_local": "2026-09-17T10:30+05:30"},
              "callback": {"queued": False}, "results_disclosed": True, "duration_seconds": 12.0,
              "agent_tokens": {"input": 1200, "output": 30}, "tool_errors": [],
              "guards": ["guard_ungrounded_time"]},
    "judgement": {"outcome": "booked", "answered_by": "patient", "sentiment": "positive",
                  "concerns": [], "summary": "Booked for tomorrow."},
    "overridden": [], "flags": [], "analysis_tokens": {"input": 900, "output": 120}, "error": None,
}


def a_call(**kw):
    return CallRecord(room="call-room", patient=PATIENT, history=[], log_rows=LOG_ROWS, **kw)


class FakeTrace:
    def __init__(self, payload):
        self.id = "trace-1"
        self.payload, self.spans, self.scores, self.ended = payload, [], [], False

    def span(self, **kw):
        self.spans.append(kw)

    def log_feedback_score(self, **kw):
        self.scores.append(kw)

    def end(self, **kw):  # never used: the trace is created complete
        self.ended = True


class FakeClient:
    def __init__(self, fail=False):
        self.fail, self.trace_obj, self.flushed = fail, None, False

    def trace(self, **kw):
        if self.fail:
            raise RuntimeError("Opik is down")
        self.trace_obj = FakeTrace(kw)
        return self.trace_obj

    def flush(self, timeout=None):
        self.flushed = True


class Payload(unittest.TestCase):
    def setUp(self):
        self.payload = oi.build_payload(a_call(), ANALYSIS)

    def test_phone_is_masked(self):
        self.assertEqual(self.payload["input"]["patient"]["phone"], "******3210")
        self.assertNotIn("9876543210", str(self.payload))

    def test_transcript_is_on_the_trace_for_the_online_rule(self):
        # An Opik rule reads trace fields, never attachments.
        call = CallRecord(room="call-room", patient=PATIENT, log_rows=LOG_ROWS, history=[
            {"type": "message", "role": "assistant", "content": ["Hi Priya."]},
            {"type": "message", "role": "user", "content": ["Tomorrow at ten thirty."]},
        ])
        text = oi.build_payload(call, ANALYSIS)["input"]["transcript"]
        self.assertIn("AGENT: Hi Priya.", text)
        self.assertIn("CALLEE: Tomorrow at ten thirty.", text)

    def test_outcome_and_booking_are_the_trace_output(self):
        self.assertEqual(self.payload["output"]["outcome"], "booked")
        self.assertEqual(self.payload["output"]["booking"]["reference"], "ADT-1")
        self.assertIn("booked", self.payload["tags"])

    def test_one_span_per_turn_tool_and_model_request(self):
        names = [s["name"] for s in self.payload["spans"]]
        self.assertEqual(names, ["turn 1", "turn 2", "book_appointment", "llm request 1", "post-call analysis"])
        turn = self.payload["spans"][0]
        self.assertEqual(turn["input"], {"patient": "Yes, speaking."})
        self.assertEqual(turn["output"], {"agent": "Hi Priya."})

    def test_tool_span_carries_arguments_and_result(self):
        tool = next(s for s in self.payload["spans"] if s["type"] == "tool")
        self.assertEqual(tool["input"]["time"], "10:30")
        self.assertIn("ADT-1", tool["output"]["result"])

    def test_llm_span_carries_usage_for_pricing(self):
        llm = next(s for s in self.payload["spans"] if s["name"] == "llm request 1")
        self.assertEqual(llm["usage"]["total_tokens"], 1230)

    def test_scores_come_from_the_facts(self):
        scores = {s["name"]: s["value"] for s in self.payload["scores"]}
        self.assertEqual(scores["booking_successful"], 1.0)
        self.assertEqual(scores["results_leaked_to_non_patient"], 0.0)
        self.assertEqual(scores["guards_fired"], 1.0)

    def test_audio_reference_says_there_is_no_recording(self):
        audio = self.payload["metadata"]["audio"]
        self.assertFalse(audio["recorded"])
        self.assertEqual(audio["livekit_room"], "call-room")

    def test_leak_flag_becomes_a_score(self):
        leaked = {**ANALYSIS, "flags": ["results_disclosed_to_non_patient"]}
        scores = {s["name"]: s["value"] for s in oi.build_payload(a_call(), leaked)["scores"]}
        self.assertEqual(scores["results_leaked_to_non_patient"], 1.0)


class Sending(unittest.TestCase):
    def test_sends_trace_spans_and_scores_then_flushes(self):
        client = FakeClient()
        trace_id = oi.send_call(a_call(), ANALYSIS, client=client)
        self.assertEqual(trace_id, "trace-1")
        self.assertEqual(len(client.trace_obj.spans), 5)
        self.assertEqual(len(client.trace_obj.scores), 3)
        self.assertFalse(client.trace_obj.ended)
        self.assertEqual(client.trace_obj.payload["name"], "outbound-call")
        self.assertIsNotNone(client.trace_obj.payload["end_time"])
        self.assertTrue(client.flushed)

    def test_failure_is_swallowed(self):
        self.assertIsNone(oi.send_call(a_call(), ANALYSIS, client=FakeClient(fail=True)))

    def test_disabled_without_a_key(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"OPIK_API_KEY": ""}, clear=False):
            self.assertIsNone(oi.send_call(a_call(), ANALYSIS))


if __name__ == "__main__":
    unittest.main()


class OnlineRule(unittest.TestCase):
    """The rule Opik runs on its own server, checked without network access."""

    def setUp(self):
        import opik_rules
        self.rule = opik_rules.definition("project-1")
        self.code = self.rule["code"]

    def test_scores_the_three_things_we_asked_for(self):
        names = [s["name"] for s in self.code["schema"]]
        self.assertEqual(names, ["booking_achieved", "privacy_respected", "professionalism"])

    def test_runs_on_every_call_with_the_free_judge(self):
        self.assertEqual(self.rule["sampling_rate"], 1.0)
        self.assertTrue(self.rule["enabled"])
        # Any other model needs a provider key added to the workspace.
        self.assertEqual(self.code["model"]["name"], "opik-free-model")

    def test_every_variable_is_a_field_we_actually_send(self):
        payload = oi.build_payload(a_call(), ANALYSIS)
        for path in self.code["variables"].values():
            section, *keys = path.split(".")
            value = payload[section]
            for key in keys:
                self.assertIn(key, value, f"{path} is not in the trace")
                value = value[key]

    def test_prompt_uses_those_variables(self):
        for name in self.code["variables"]:
            self.assertIn("{{" + name + "}}", self.code["messages"][0]["content"])
