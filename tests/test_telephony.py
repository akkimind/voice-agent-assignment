"""Placing a phone call, without a phone network."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import telephony
from post_call import CallRecord


class FakeSipError(Exception):
    def __init__(self, code, status):
        super().__init__(f"sip error {code}")
        self.sip_status_code, self.sip_status = code, status


class FakeSip:
    def __init__(self, error=None):
        self.error, self.request = error, None

    async def create_sip_participant(self, request):
        self.request = request
        if self.error:
            raise self.error
        return SimpleNamespace(participant_id="PA_1")


class FakeApi:
    def __init__(self, error=None):
        self.sip = FakeSip(error)
        self.deleted = []
        self.room = SimpleNamespace(delete_room=self._delete)

    async def _delete(self, request):
        self.deleted.append(request.room)


REQUEST = telephony.DialRequest(room="call-1", phone="+919876543210", patient_id="p-001", trunk_id="ST_1")


def run(coro):
    return asyncio.run(coro)


class StatusCodes(unittest.TestCase):
    def test_busy_and_declined_are_rejections(self):
        for code in (486, 600, 603):
            self.assertEqual(telephony.outcome_for_status(code), "rejected")

    def test_ring_out_and_unreachable_are_no_answer(self):
        for code in (408, 480, 487, 404, 604):
            self.assertEqual(telephony.outcome_for_status(code), "no_answer")

    def test_anything_else_is_incomplete(self):
        self.assertEqual(telephony.outcome_for_status(503), "incomplete")
        self.assertEqual(telephony.outcome_for_status(None), "incomplete")


class Dialling(unittest.TestCase):
    def test_answered_call_returns_no_failure(self):
        api = FakeApi()
        self.assertIsNone(run(telephony.dial(api, REQUEST)))
        self.assertEqual(api.sip.request.sip_call_to, "+919876543210")
        self.assertEqual(api.sip.request.room_name, "call-1")

    def test_waits_for_the_answer_before_returning(self):
        # Otherwise the agent would greet the ringtone.
        self.assertTrue(telephony.dial_request(REQUEST).wait_until_answered)

    def test_declined_call_becomes_an_outcome(self):
        failure = run(telephony.dial(FakeApi(FakeSipError(603, "Declined")), REQUEST))
        self.assertEqual(failure.outcome, "rejected")
        self.assertEqual(failure.status_code, 603)

    def test_unanswered_call_becomes_an_outcome(self):
        self.assertEqual(run(telephony.dial(FakeApi(FakeSipError(487, "Terminated")), REQUEST)).outcome,
                         "no_answer")

    def test_a_broken_trunk_is_not_raised_at_the_caller(self):
        failure = run(telephony.dial(FakeApi(RuntimeError("no such trunk")), REQUEST))
        self.assertEqual(failure.outcome, "incomplete")
        self.assertIn("no such trunk", failure.detail)

    def test_hanging_up_deletes_the_room(self):
        api = FakeApi()
        run(telephony.hang_up(api, "call-1"))
        self.assertEqual(api.deleted, ["call-1"])

    def test_trunk_id_must_be_configured(self):
        with mock.patch.dict("os.environ", {"SIP_OUTBOUND_TRUNK_ID": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                telephony.trunk_id()


class UnansweredCallRecord(unittest.TestCase):
    """A call nobody answered still gets an outcome, with no model involved."""

    def setUp(self):
        from tests.helpers import TempDatabase
        self.t = TempDatabase()
        self.patient = self.t.patient("p-001")

    def tearDown(self):
        self.t.close()

    def analyse(self, outcome):
        import post_call
        call = CallRecord(room="call-1", patient=self.patient, history=[], transport="sip",
                          dial_failure={"outcome": outcome, "status_code": 486, "status": "Busy", "detail": ""})

        async def never(*_a, **_kw):
            raise AssertionError("no model should be asked about a call nobody answered")

        with mock.patch.object(post_call, "_complete", never):
            return asyncio.run(post_call.analyze(call, conn=self.t.conn))

    def test_rejected_call(self):
        result = self.analyse("rejected")
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(result["booking_successful"])
        self.assertEqual(result["transport"], "sip")
        self.assertIsNone(result["error"])

    def test_unanswered_call(self):
        self.assertEqual(self.analyse("no_answer")["outcome"], "no_answer")


class PhoneFromMetadata(unittest.TestCase):
    def _ctx(self, metadata):
        return SimpleNamespace(job=SimpleNamespace(metadata=metadata))

    def test_browser_job_has_no_number(self):
        import agent
        patient = {"phone": "+919876543210"}
        self.assertEqual(agent._phone_to_dial(self._ctx(""), patient), "")
        self.assertEqual(agent._phone_to_dial(self._ctx('{"patient_id": "p-001"}'), patient), "")

    def test_phone_job_dials(self):
        import agent
        patient = {"phone": "+919876543210"}
        self.assertEqual(agent._phone_to_dial(self._ctx('{"transport": "sip"}'), patient), "+919876543210")
        self.assertEqual(agent._phone_to_dial(self._ctx('{"phone": "+15550000"}'), patient), "+15550000")

    def test_numbers_are_masked_in_the_log(self):
        import agent
        self.assertEqual(agent._masked("+919876543210"), "******3210")

class Transport(unittest.TestCase):
    """How the agent knows whether it is on a phone, and who dialled."""

    def _ctx(self, metadata):
        return SimpleNamespace(job=SimpleNamespace(metadata=metadata))

    def test_browser(self):
        import agent
        ctx = self._ctx('{"patient_id": "p-001"}')
        self.assertEqual(agent._transport(ctx, agent._phone_to_dial(ctx, {"phone": "+91..."})), "webrtc")

    def test_agent_dials_out(self):
        import agent
        ctx = self._ctx('{"transport": "sip"}')
        self.assertEqual(agent._transport(ctx, agent._phone_to_dial(ctx, {"phone": "+919876543210"})), "sip")

    def test_call_arrives_already_bridged(self):
        import agent
        ctx = self._ctx('{"patient_id": "p-001", "transport": "sip_inbound"}')
        dial_to = agent._phone_to_dial(ctx, {"phone": "+919876543210"})
        self.assertEqual(dial_to, "")            # nothing for us to dial
        self.assertEqual(agent._transport(ctx, dial_to), "sip_inbound")

class SimulatedRefusals(unittest.TestCase):
    """The paths a browser cannot reach, exercised without a phone."""

    def setUp(self):
        from tests.helpers import TempDatabase
        self.t = TempDatabase()
        self.patient = self.t.patient("p-001")

    def tearDown(self):
        self.t.close()

    def test_status_becomes_an_outcome_and_is_labelled(self):
        failure = telephony.simulated_failure(486)
        self.assertEqual(failure.outcome, "rejected")
        self.assertEqual(failure.status, "simulated")
        self.assertIn("simulated", failure.detail)

    def test_record_says_simulated_so_it_cannot_pass_for_a_real_call(self):
        import post_call
        failure = telephony.simulated_failure(487)
        call = CallRecord(room="call-1", patient=self.patient, history=[], transport="sip",
                          dial_failure={"outcome": failure.outcome, "status_code": failure.status_code,
                                        "status": failure.status, "detail": failure.detail,
                                        "simulated": True})
        result = asyncio.run(post_call.analyze(call, conn=self.t.conn))
        self.assertEqual(result["outcome"], "no_answer")
        self.assertTrue(result["simulated"])

    def test_a_real_refusal_is_not_labelled_simulated(self):
        import post_call
        call = CallRecord(room="call-1", patient=self.patient, history=[], transport="sip",
                          dial_failure={"outcome": "rejected", "status_code": 603, "status": "Declined",
                                        "detail": "", "simulated": False})
        self.assertFalse(asyncio.run(post_call.analyze(call, conn=self.t.conn))["simulated"])

    def test_opik_trace_is_tagged(self):
        import opik_integration as oi
        analysis = {"outcome": "rejected", "booking_successful": False, "simulated": True,
                    "facts": {"booking": {"booked": False}, "callback": {"queued": False}, "guards": []},
                    "analysis_tokens": {}, "judgement": None}
        payload = oi.build_payload(CallRecord(room="call-1", patient=self.patient, history=[],
                                              transport="sip"), analysis)
        self.assertIn("simulated", payload["tags"])
        self.assertTrue(payload["metadata"]["simulated"])


class Recording(unittest.TestCase):
    def test_nothing_to_save_without_a_session(self):
        import agent
        self.assertIsNone(agent._save_recording("call-1", {"ctx": None, "session": None}))

    def test_a_missing_file_is_not_an_error(self):
        import agent
        ctx = SimpleNamespace(make_session_report=lambda _s: SimpleNamespace(audio_recording_path="/no/such.ogg"))
        self.assertIsNone(agent._save_recording("call-1", {"ctx": ctx, "session": object()}))


if __name__ == "__main__":
    unittest.main()
