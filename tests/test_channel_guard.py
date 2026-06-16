import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel_guard.state import DEFAULT_MAX_OUTBOUND, ChannelGuardState


class ChannelGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = ChannelGuardState(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def record_message(self, content="记一下：以后把这个助手叫 Helper。", message_id="100"):
        return self.state.record_inbound(
            content,
            {
                "chat_id": "chat-1",
                "user_id": "user-1",
                "message_id": message_id,
                "ts": "2026-06-16T08:00:00Z",
            },
        )

    def test_telegram_outbound_requires_real_inbound(self):
        denied = self.state.allow_telegram_outbound("reply", {"chat_id": "chat-1", "text": "hi"})
        self.assertFalse(denied.allow)
        self.assertEqual(denied.reason, "no_active_grant")

        grant_id = self.record_message("hello")
        allowed = self.state.allow_telegram_outbound("reply", {"chat_id": "chat-1", "text": "hi"})
        self.assertTrue(allowed.allow)
        self.assertEqual(allowed.grant_id, grant_id)

    def test_telegram_outbound_rejects_wrong_reply_to(self):
        self.record_message("hello")
        denied = self.state.allow_telegram_outbound(
            "reply",
            {"chat_id": "chat-1", "reply_to": "999", "text": "hi"},
        )
        self.assertFalse(denied.allow)
        self.assertEqual(denied.reason, "message_id_mismatch")

    def test_telegram_outbound_burst_limit_closes_grant(self):
        self.record_message("hello")
        for _ in range(DEFAULT_MAX_OUTBOUND):
            self.assertTrue(self.state.allow_telegram_outbound("reply", {"chat_id": "chat-1", "text": "x"}).allow)
        denied = self.state.allow_telegram_outbound("reply", {"chat_id": "chat-1", "text": "x"})
        self.assertFalse(denied.allow)
        self.assertEqual(denied.reason, "outbound_count_exceeded")

    def test_memory_write_allows_explicit_grounded_remember(self):
        self.record_message("记一下：以后把这个助手叫 Helper。")
        decision = self.state.allow_memory_write(
            "memory_remember",
            {"content": "用户希望把这个助手称为 Helper。"},
        )
        self.assertTrue(decision.allow)

    def test_memory_write_reviews_active_memory_without_intent(self):
        self.record_message("今天这个结论很重要。")
        decision = self.state.allow_memory_write(
            "memory_remember",
            {"content": "用户认为今天这个结论很重要。"},
        )
        self.assertFalse(decision.allow)
        self.assertTrue(decision.review)
        self.assertEqual(decision.reason, "no_explicit_memory_intent")

    def test_memory_write_reviews_ungrounded_extra_content(self):
        self.record_message("记一下：以后把这个助手叫 Helper。")
        decision = self.state.allow_memory_write(
            "memory_remember",
            {"content": "用户同意了新的亲密关系变化。"},
        )
        self.assertFalse(decision.allow)
        self.assertTrue(decision.review)

    def test_memory_write_delete_is_high_impact(self):
        self.record_message("删掉这条记忆。")
        decision = self.state.allow_memory_write("memory_delete", {"memory_id": 1})
        self.assertFalse(decision.allow)
        self.assertTrue(decision.review)
        self.assertEqual(decision.reason, "high_impact_memory_write_requires_review")

    def test_hash_mismatch_denies_outbound_and_reviews_memory(self):
        self.record_message("记一下：以后把这个助手叫 Helper。")
        self.state.set_health("upstream_status", "upstream_hash_mismatch")
        outbound = self.state.allow_telegram_outbound("reply", {"chat_id": "chat-1", "text": "hi"})
        memory = self.state.allow_memory_write("memory_remember", {"content": "助手 Helper"})
        self.assertFalse(outbound.allow)
        self.assertEqual(outbound.reason, "upstream_hash_mismatch")
        self.assertFalse(memory.allow)
        self.assertTrue(memory.review)

    def test_ledger_does_not_store_plain_content(self):
        self.record_message("记一下：以后把这个助手叫 Helper。")
        ledger = Path(self.tmp.name, "inbound-ledger.jsonl").read_text(encoding="utf-8")
        row = json.loads(ledger.splitlines()[0])
        self.assertIn("content_hash", row)
        self.assertNotIn("以后把这个助手", ledger)

    def test_duplicate_inbound_does_not_replace_active_grant(self):
        first = self.record_message("第一条", message_id="100")
        second = self.record_message("第二条", message_id="101")
        duplicate = self.record_message("重复第一条", message_id="100")

        self.assertIsNone(duplicate)
        decision = self.state.allow_telegram_outbound(
            "reply",
            {"chat_id": "chat-1", "reply_to": "101", "text": "ok"},
        )
        self.assertTrue(decision.allow)
        self.assertEqual(decision.grant_id, second)

        ledger = Path(self.tmp.name, "inbound-ledger.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(ledger.splitlines()), 2)
        self.assertNotEqual(first, second)

    def test_stop_hook_does_not_close_unconsumed_new_inbound_grant(self):
        first = self.record_message("第一条", message_id="100")
        self.assertTrue(
            self.state.allow_telegram_outbound(
                "reply",
                {"chat_id": "chat-1", "reply_to": "100", "text": "ok"},
            ).allow
        )

        second = self.record_message("第二条", message_id="101")
        closed = self.state.close_consumed("stop_hook")

        self.assertEqual(closed, 0)
        decision = self.state.allow_telegram_outbound(
            "reply",
            {"chat_id": "chat-1", "reply_to": "101", "text": "new ok"},
        )
        self.assertTrue(decision.allow)
        self.assertEqual(decision.grant_id, second)
        self.assertNotEqual(first, second)

    def test_stop_hook_closes_consumed_active_grant(self):
        self.record_message("第一条", message_id="100")
        self.assertTrue(
            self.state.allow_telegram_outbound(
                "reply",
                {"chat_id": "chat-1", "reply_to": "100", "text": "ok"},
            ).allow
        )

        closed = self.state.close_consumed("stop_hook")

        self.assertEqual(closed, 1)
        decision = self.state.allow_telegram_outbound(
            "reply",
            {"chat_id": "chat-1", "reply_to": "100", "text": "again"},
        )
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "no_active_grant")


if __name__ == "__main__":
    unittest.main()
