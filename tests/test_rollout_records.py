"""Synthetic regression inputs for the cited-record adapter."""
import unittest

from kilix_rollout.records import adapt_record


class RecordsTests(unittest.TestCase):
    def test_codex_channels(self):
        def row(channel):
            return {"type": "response_item", "payload": {"type": "message",
                    "role": "assistant", "channel": channel,
                    "content": [{"type": "output_text", "text": channel}]}}
        self.assertEqual(adapt_record("codex", row("analysis"))["records"], [])
        self.assertIn("reasoning", adapt_record("codex", row("analysis"))["excluded"])
        self.assertEqual(adapt_record("codex", row("final"))["records"][0]["text"], "final")
        self.assertEqual(adapt_record("codex", row("made_up"))["errors"][0]["code"], "unsupported_record")

    def test_unhashable_discriminators_are_reported(self):
        cases = [("claude", {"type": []}), ("claude", {"type": {}}),
                 ("claude", {"type": "user", "message": {"role": [], "content": "x"}}),
                 ("claude", {"type": "assistant", "message": {"content": [{"type": []}]}}),
                 ("codex", {"type": "response_item", "payload": {"type": []}}),
                 ("codex", {"type": "response_item", "payload": {"type": "message", "role": {}, "content": []}}),
                 ("codex", {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": []}]}}),
                 ("codex", {"type": "event_msg", "payload": {"type": []}})]
        for provider, row in cases:
            with self.subTest(provider=provider, row=row):
                out = adapt_record(provider, row)
                self.assertEqual(out["records"], [])
                self.assertEqual(out["errors"][0]["code"], "unsupported_record")


if __name__ == "__main__":
    unittest.main()
