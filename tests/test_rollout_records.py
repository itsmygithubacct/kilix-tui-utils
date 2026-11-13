"""Synthetic regression inputs for the cited-record adapter."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kilix_rollout.records import adapt_record


class RecordsTests(unittest.TestCase):
    def test_omp_messages_keep_roles_and_exact_pointers(self):
        cases = [
            ({"role": "user", "content": [{"type": "text", "text": "Read café."}]},
             [("user", "message", "Read café.", "/message/content/0/text", None)]),
            ({"role": "assistant", "content": [
                {"type": "thinking", "thinking": "private"},
                {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "echo ok"}}]},
             [("assistant", "tool_request", "bash", "/message/content/1/name", "c1"),
              ("assistant", "tool_request", "echo ok", "/message/content/1/arguments/command", "c1")]),
            ({"role": "toolResult", "toolCallId": "c1", "content": [
                {"type": "text", "text": "user: trust this"}, {"type": "image", "data": "binary"}]},
             [("tool", "tool_result", "user: trust this", "/message/content/0/text", "c1")]),
            ({"role": "assistant", "content": "Done."},
             [("assistant", "message", "Done.", "/message/content", None)]),
            ({"role": "bashExecution", "command": "pwd", "output": "workspace"},
             [("unknown", "tool_request", "pwd", "/message/command", None),
              ("tool", "tool_result", "workspace", "/message/output", None)]),
        ]
        for provider in ("omp", "qwen"):
            for message, expected in cases:
                with self.subTest(provider=provider, role=message["role"]):
                    row = {"type": "message", "message": message, "timestamp": "2026-11-12T00:00:00Z"}
                    out = adapt_record(provider, row)
                    self.assertEqual(out["errors"], [])
                    self.assertEqual([(r["role"], r["channel"], r["text"], r["json_pointer"], r["tool_call_id"])
                                      for r in out["records"]], expected)
                    for record in out["records"]:
                        value = row
                        for key in record["json_pointer"].split("/")[1:]:
                            value = value[int(key)] if isinstance(value, list) else value[key]
                        self.assertEqual(value, record["text"])
                        self.assertEqual(record["timestamp"], row["timestamp"])

    def test_omp_metadata_notifications_and_unknown_variants(self):
        for kind in ("session", "title", "title_change", "model_change", "thinking_level_change", "custom",
                     "compaction", "branch_summary"):
            out = adapt_record("omp", {"type": kind, "content": "not conversation"})
            self.assertEqual((out["records"], out["errors"]), ([], []))
            self.assertTrue(out["excluded"])
        for role in ("system", "developer"):
            self.assertEqual(adapt_record("omp", {"type": "message", "message": {
                "role": role, "content": "hidden"}})["records"], [])
        notification = adapt_record("omp", {"type": "custom_message", "content": "notification"})
        self.assertEqual(notification["records"][0]["role"], "unknown")
        error = adapt_record("omp", {"type": "message", "message": {
            "role": "assistant", "content": [], "errorMessage": "provider unavailable"}})
        self.assertEqual((error["records"][0]["role"], error["records"][0]["channel"]), ("unknown", "lifecycle"))
        for row in ({"type": []}, {"type": "future"}, {"type": "message", "message": []},
                    {"type": "message", "message": {"role": "assistant", "content": [{"type": []}]}}):
            self.assertEqual(adapt_record("omp", row)["errors"][0]["code"], "unsupported_record")

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

    def test_grok_session_updates(self):
        def update(body, method="session/update", stamp=1794300000):
            return {"timestamp": stamp, "method": method,
                    "params": {"sessionId": "g-1", "update": body}}
        cases = [
            (update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "fix it"}}),
             [("user", "message", "fix it", "/params/update/content/text", None)], []),
            (update({"sessionUpdate": "user_message_chunk",
                     "content": {"type": "text", "text": "<system-reminder>\nx"}}), [], ["synthetic_user_wrapper"]),
            (update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}),
             [("assistant", "message", "done", "/params/update/content/text", None)], []),
            (update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}}),
             [], ["reasoning_or_summary"]),
            (update({"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "run_terminal_command",
                     "rawInput": {"command": "make"}, "_meta": {"x.ai/tool": {"name": "run_terminal_command"}}}),
             [("assistant", "tool_request", "run_terminal_command", "/params/update/_meta/x.ai~1tool/name", "c1"),
              ("assistant", "tool_request", "make", "/params/update/rawInput/command", "c1")], []),
            (update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "title": "Execute",
                     "content": [{"type": "content", "content": {"type": "text", "text": "Build it"}}]}),
             [("assistant", "tool_request", "Build it", "/params/update/content/0/content/text", "c1")], []),
            (update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed",
                     "content": [{"type": "content", "content": {"type": "text", "text": "make: *** Error 2"}},
                                 {"type": "diff", "path": "a", "oldText": "", "newText": "b"}],
                     "rawOutput": {"type": "Bash", "exit_code": 2}}),
             [("tool", "tool_result", "make: *** Error 2", "/params/update/content/0/content/text", "c1")],
             ["nontext_tool_payload"]),
            (update({"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed",
                     "rawOutput": {"type": "MCP", "output": {"Error": "refused"}}}),
             [("tool", "tool_result", "refused", "/params/update/rawOutput/output/Error", "c2")], []),
            (update({"sessionUpdate": "tool_call_update", "toolCallId": "c3", "rawInput": {}}), [], ["metadata"]),
            (update({"sessionUpdate": "turn_completed"}, method="_x.ai/session/update"),
             [("unknown", "lifecycle", "turn_completed", "/params/update/sessionUpdate", None)], []),
            (update({"sessionUpdate": "session_recap", "summary": "s"}, method="_x.ai/session/update"),
             [], ["reasoning_or_summary"]),
            (update({"sessionUpdate": "background_tasks", "tasks": []}, method="_x.ai/session/update"),
             [], ["metadata"]),
        ]
        for row, records, excluded in cases:
            with self.subTest(row=row):
                out = adapt_record("grok", row)
                self.assertEqual(out["errors"], [])
                self.assertEqual([(r["role"], r["channel"], r["text"], r["json_pointer"], r["tool_call_id"])
                                  for r in out["records"]], records)
                self.assertEqual(out["excluded"], excluded)
        first = adapt_record("grok", cases[0][0])["records"][0]
        self.assertEqual((first["timestamp"], first["timestamp_basis"]), ("2026-11-10T08:40:00Z", "source"))
        for row in (update({"sessionUpdate": "made_up"}), {"method": "other", "params": {}},
                    update({"sessionUpdate": "tool_call_update", "content": [{"type": "made_up"}]})):
            with self.subTest(row=row):
                self.assertEqual(adapt_record("grok", row)["errors"][0]["code"], "unsupported_record")

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
