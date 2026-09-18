"""Retrying a call nobody took, and the cases that must never be retried."""

import unittest

import callback_queue
import config
import telephony
from tests.helpers import TempDatabase, at

NOW = at(14, 10)   # Monday 14 September 2026, 10:00 IST


class WhichCallsAreRetried(unittest.TestCase):
    def test_bad_timing_is_retried(self):
        self.assertEqual(telephony.retry_reason(486), "busy")
        self.assertEqual(telephony.retry_reason(600), "busy")
        for code in (408, 480, 487):
            self.assertEqual(telephony.retry_reason(code), "no_answer")

    def test_a_decision_is_not_retried(self):
        # They rejected the call. Calling back would be harassment.
        self.assertIsNone(telephony.retry_reason(603))

    def test_an_unreachable_number_is_not_retried_but_is_flagged(self):
        for code in (404, 604):
            self.assertIsNone(telephony.retry_reason(code))
            self.assertTrue(telephony.needs_front_desk(code))
        self.assertFalse(telephony.needs_front_desk(486))

    def test_an_unclear_status_is_not_retried(self):
        self.assertIsNone(telephony.retry_reason(503))
        self.assertIsNone(telephony.retry_reason(None))


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.t = TempDatabase()
        self.priya = self.t.patient("p-001")

    def tearDown(self):
        self.t.close()

    def retry(self, reason, now=NOW):
        return callback_queue.schedule_retry(self.t.conn, patient=self.priya, now=now,
                                             reason=reason, source_room="call-1")

    def test_busy_is_retried_sooner_than_no_answer(self):
        self.assertEqual(self.retry("busy")["in_minutes"], config.CALL_RETRY_MINUTES["busy"])
        self.assertEqual(self.retry("no_answer")["in_minutes"], config.CALL_RETRY_MINUTES["no_answer"])

    def test_a_retry_is_queued_as_a_callback(self):
        decided = self.retry("no_answer")
        self.assertTrue(decided["retried"])
        rows = [dict(r) for r in self.t.conn.execute(
            "SELECT * FROM callbacks WHERE status = 'pending'")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference"], decided["reference"])
        self.assertEqual(rows[0]["requested_by"], config.RETRY_REQUESTED_BY)

    def test_an_unretried_reason_queues_nothing(self):
        decided = callback_queue.schedule_retry(self.t.conn, patient=self.priya, now=NOW,
                                                reason="declined")
        self.assertFalse(decided["retried"])
        self.assertEqual(self.t.conn.execute("SELECT COUNT(*) FROM callbacks").fetchone()[0], 0)

    def test_the_daily_cap_stops_endless_dialling(self):
        # The first call counts, so a cap of 3 allows two retries.
        self.assertTrue(self.retry("no_answer")["retried"])
        self.assertTrue(self.retry("no_answer")["retried"])
        third = self.retry("no_answer")
        self.assertFalse(third["retried"])
        self.assertIn("limit", third["why_not"])

    def test_patient_asked_callbacks_do_not_count_towards_the_cap(self):
        callback_queue.schedule(self.t.conn, patient=self.priya, now=NOW, phrase="in an hour",
                                requested_by="patient", in_minutes=60)
        self.assertEqual(callback_queue.attempts_today(self.t.conn, self.priya["id"], NOW), 0)
        self.assertTrue(self.retry("busy")["retried"])


if __name__ == "__main__":
    unittest.main()
