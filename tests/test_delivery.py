import tempfile
import unittest
from pathlib import Path

from delivery import Delivery


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "delivery.db"
        self.delivery = Delivery(self.path)
        self.id = self.delivery.create("transfer-1", "user-1", "Перевод выполнен")

    def tearDown(self):
        self.delivery.close()
        self.directory.cleanup()

    def restart(self):
        self.delivery.close()
        self.delivery = Delivery(self.path)

    def test_same_event_returns_same_id_after_restart(self):
        self.restart()
        repeated = self.delivery.create("transfer-1", "user-1", "Перевод выполнен")
        self.assertEqual(self.id, repeated)

    def test_same_key_with_different_content_is_rejected(self):
        with self.assertRaises(ValueError):
            self.delivery.create("transfer-1", "user-1", "Другой перевод")

    def test_event_can_notify_other_user_or_come_from_other_producer(self):
        other_user = self.delivery.create("transfer-1", "user-2", "Перевод выполнен")
        other_producer = self.delivery.create("transfer-1", "user-1", "Перевод выполнен",
                                              producer="cards")
        self.assertEqual(3, len({self.id, other_user, other_producer}))

    def test_acceptance_is_not_delivery(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "accepted", now=0)
        self.assertEqual("WAITING", self.delivery.get(self.id)["state"])
        self.assertIsNone(self.delivery.next_attempt(self.id, now=1.99))
        self.delivery.record(push.key, "delivered", now=1.99)
        self.assertIsNone(self.delivery.next_attempt(self.id, now=100))
        self.assertEqual(1, len(self.delivery.get(self.id)["attempts"]))

    def test_timeout_starts_sms_at_deadline_and_survives_restart(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "accepted", now=0)
        self.restart()
        self.assertIsNone(self.delivery.next_attempt(self.id, now=1.99))
        sms = self.delivery.next_attempt(self.id, now=2)
        self.assertEqual("sms", sms.channel)
        self.delivery.record(sms.key, "delivered", now=3)
        self.assertEqual("DELIVERED", self.delivery.get(self.id)["state"])

    def test_explicit_failure_does_not_wait_for_timeout(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "failed", now=0.1)
        self.assertEqual("sms", self.delivery.next_attempt(self.id, now=0.1).channel)

    def test_unknown_result_waits_until_deadline(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "unknown", now=0.5)
        self.assertIsNone(self.delivery.next_attempt(self.id, now=1))
        self.assertEqual("sms", self.delivery.next_attempt(self.id, now=2).channel)

    def test_known_failure_after_unknown_starts_failover_immediately(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "unknown", now=0.2)
        self.delivery.record(push.key, "failed", now=0.5)
        self.assertEqual("FAILED", self.delivery.get(self.id)["attempts"][0]["state"])
        self.assertEqual("sms", self.delivery.next_attempt(self.id, now=0.5).channel)

    def test_late_failure_of_previous_channel_does_not_accelerate_current_one(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "accepted", now=0)
        sms = self.delivery.next_attempt(self.id, now=2)
        self.delivery.record(sms.key, "accepted", now=2)
        self.delivery.record(push.key, "failed", now=2.1)
        self.assertEqual("FAILED", self.delivery.get(self.id)["attempts"][0]["state"])
        self.assertIsNone(self.delivery.next_attempt(self.id, now=6.99))
        self.assertEqual("email", self.delivery.next_attempt(self.id, now=7).channel)

    def test_crash_before_result_reuses_provider_key(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.restart()
        repeated = self.delivery.next_attempt(self.id, now=0.5)
        self.assertEqual(push.key, repeated.key)
        self.assertEqual(1, len(self.delivery.get(self.id)["attempts"]))

    def test_full_timeout_chain_ends_in_attention_after_17_seconds(self):
        for second, channel in ((0, "push"), (2, "sms"), (7, "email")):
            attempt = self.delivery.next_attempt(self.id, now=second)
            self.assertEqual(channel, attempt.channel)
            self.delivery.record(attempt.key, "accepted", now=second)
        self.assertIsNone(self.delivery.next_attempt(self.id, now=16.99))
        self.assertIsNone(self.delivery.next_attempt(self.id, now=17))
        self.assertEqual("NEEDS_ATTENTION", self.delivery.get(self.id)["state"])

    def test_receipt_before_failover_prevents_sms(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "accepted", now=0)
        self.delivery.record(push.key, "delivered", now=2)
        self.assertIsNone(self.delivery.next_attempt(self.id, now=2))

    def test_late_receipt_after_sms_stops_email_but_cannot_retract_sms(self):
        push = self.delivery.next_attempt(self.id, now=0)
        self.delivery.record(push.key, "accepted", now=0)
        sms = self.delivery.next_attempt(self.id, now=2)
        self.delivery.record(sms.key, "accepted", now=2)
        self.delivery.record(push.key, "delivered", now=3)
        self.delivery.record(sms.key, "delivered", now=4)
        self.assertIsNone(self.delivery.next_attempt(self.id, now=100))
        history = self.delivery.get(self.id)["attempts"]
        self.assertEqual(["DELIVERED", "DELIVERED"], [a["state"] for a in history])

    def test_duplicate_and_out_of_order_receipts_do_not_undo_success(self):
        push = self.delivery.next_attempt(self.id, now=0)
        for result in ("delivered", "delivered", "failed", "accepted"):
            self.delivery.record(push.key, result, now=1)
        self.assertEqual("DELIVERED", self.delivery.get(self.id)["state"])
        self.assertEqual("DELIVERED", self.delivery.get(self.id)["attempts"][0]["state"])

    def test_all_failures_are_kept_for_attention_and_late_success_is_allowed(self):
        attempts = []
        for second in range(3):
            attempt = self.delivery.next_attempt(self.id, now=second)
            attempts.append(attempt)
            self.delivery.record(attempt.key, "failed", now=second)
        self.assertIsNone(self.delivery.next_attempt(self.id, now=3))
        self.restart()
        self.assertEqual("NEEDS_ATTENTION", self.delivery.get(self.id)["state"])
        self.assertEqual(3, len(self.delivery.get(self.id)["attempts"]))
        self.delivery.record(attempts[0].key, "delivered", now=10)
        self.assertEqual("DELIVERED", self.delivery.get(self.id)["state"])

    def test_preferred_channel_and_missing_contacts(self):
        for channel in ("email", "sms"):
            notification_id = self.delivery.create(channel, "user-1", "Статус",
                                                   preferred=channel)
            self.assertEqual(channel, self.delivery.next_attempt(notification_id, 0).channel)
        notification_id = self.delivery.create("no-push", "user-1", "Статус",
                                               available=("sms", "email"))
        self.assertEqual("sms", self.delivery.next_attempt(notification_id, 0).channel)

    def test_optional_opt_out_and_critical_opt_out_rejection(self):
        notification_id = self.delivery.create("offer", "user-1", "Акция",
                                               kind="marketing", disabled=True)
        self.assertEqual("SUPPRESSED", self.delivery.get(notification_id)["state"])
        self.assertIsNone(self.delivery.next_attempt(notification_id, 0))
        with self.assertRaises(ValueError):
            self.delivery.create("critical", "user-1", "Перевод", disabled=True)

    def test_optional_messages_do_not_start_paid_failover(self):
        notification_id = self.delivery.create("status", "user-1", "Статус",
                                               kind="service")
        push = self.delivery.next_attempt(notification_id, 0)
        self.delivery.record(push.key, "failed", 0)
        self.assertIsNone(self.delivery.next_attempt(notification_id, 0))
        self.assertEqual(1, len(self.delivery.get(notification_id)["attempts"]))

    def test_invalid_request_and_untrusted_attempt_are_rejected(self):
        for options in ({"kind": "other"}, {"preferred": "fax"}, {"available": ()}):
            with self.assertRaises(ValueError):
                self.delivery.create("bad", "user-1", "Текст", **options)
        with self.assertRaises(KeyError):
            self.delivery.record("unknown-key", "delivered", 0)
        with self.assertRaises(KeyError):
            self.delivery.next_attempt("missing-notification", 0)


if __name__ == "__main__":
    unittest.main()
