"""Synthetic regression inputs for the cited-record adapter."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

    def test_current_bookkeeping_rows_are_excluded_without_errors(self):
        cases = [("claude", {"type": t, "sessionId": "s"}, "system_or_metadata") for t in (
                    "session-owner", "atis-latch", "tools-used", "cost-state", "continued-in",
                    "frame-link", "artifact-comment-monitor", "artifact-autoreact-ledger")]
        cases += [("claude", {"type": t, "sessionId": "s", "message": {"role": "user", "content": "copy"}},
                   "api_request_copy") for t in ("api-request", "api-request-blob", "api-request-shape")]
        cases += [("codex", {"type": t, "payload": {}}, "system_or_metadata") for t in (
                    "token_usage_record", "world_state", "inter_agent_communication_metadata")]
        cases += [("codex", {"type": "event_msg", "payload": {"type": t}}, "metadata") for t in (
                    "thread_settings_applied", "thread_goal_updated")]
        cases += [("codex", {"type": "event_msg", "payload": {"type": "item_completed",
                   "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": "x"}]}}},
                   "event_item_mirror"),
                  ("codex", {"type": "compacted", "payload": {"message": ""}}, "reasoning_or_summary"),
                  ("codex", {"type": "retained_context", "payload": {"type": "verified_answer"}},
                   "reasoning_or_summary"),
                  ("codex", {"type": "response_item", "payload": {"type": "additional_tools",
                   "role": "developer", "tools": []}}, "system_or_developer"),
                  ("codex", {"type": "response_item", "payload": {"type": "configuration_update"}},
                   "metadata"),
                  ("codex", {"type": "realtime_item", "payload": {"type": "realtime_session_started"}},
                   "metadata")]
        for provider, row, category in cases:
            with self.subTest(provider=provider, row=row):
                out = adapt_record(provider, row)
                self.assertEqual((out["records"], out["errors"]), ([], []))
                self.assertEqual(out["excluded"], [category])

    def test_codex_tool_output_parts_are_exported(self):
        row = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
               "output": [{"type": "input_text", "text": "Exit code: 0"},
                          {"type": "input_image", "image_url": "data:"},
                          {"type": "input_text", "text": "done"}]}}
        out = adapt_record("codex", row)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["excluded"], ["binary_payload"])
        self.assertEqual([(r["role"], r["channel"], r["text"], r["json_pointer"], r["tool_call_id"])
                          for r in out["records"]],
                         [("tool", "tool_result", "Exit code: 0", "/payload/output/0/text", "c1"),
                          ("tool", "tool_result", "done", "/payload/output/2/text", "c1")])
        row["payload"]["output"] = [{"type": "made_up"}]
        self.assertEqual(adapt_record("codex", row)["errors"][0]["code"], "unsupported_record")

    def test_codex_agent_and_voice_text_is_searchable_but_unattributed(self):
        agent = {"type": "response_item", "payload": {"type": "agent_message", "author": "/root/w",
                 "recipient": "/root", "content": [{"type": "input_text", "text": "report"},
                                                   {"type": "encrypted_content", "data": "x"}]}}
        voice = {"type": "realtime_item", "payload": {"type": "transcript_segment",
                 "role": "user", "text": "spoken"}}
        for row, text, pointer in ((agent, "report", "/payload/content/0/text"),
                                   (voice, "spoken", "/payload/text")):
            with self.subTest(row=row):
                out = adapt_record("codex", row)
                self.assertEqual(out["errors"], [])
                self.assertEqual([(r["role"], r["channel"], r["text"], r["json_pointer"])
                                  for r in out["records"]], [("unknown", "message", text, pointer)])
        self.assertEqual(adapt_record("codex", agent)["excluded"], ["encrypted_payload"])
        voice["payload"]["type"] = "made_up"
        self.assertEqual(adapt_record("codex", voice)["errors"][0]["code"], "unsupported_record")

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
