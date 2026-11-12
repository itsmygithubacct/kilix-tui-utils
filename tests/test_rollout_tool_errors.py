"""Synthetic transcripts for the failed-tool-call survey."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "tool_errors"))

from kilix_rollout import tool_errors  # noqa: E402

STAMP = "2026-11-12T10:00:00.000Z"


def _write(path: Path, rows) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(path)


def _first_rows():
    def use(call, name, command):
        return {"type": "assistant", "sessionId": "s-1", "timestamp": STAMP, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": call, "name": name,
                                              "input": {"command": command}}]}}

    def result(call, text, error=True):
        return {"type": "user", "sessionId": "s-1", "timestamp": STAMP, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": call,
                                         "is_error": error, "content": text}]}}
    envelope = json.dumps({"status": "error", "errors": [{"code": "result_too_large",
                                                          "message": "narrow the query"}]})
    return [use("a", "Bash", "git fetch"), result("a", "Exit code 128\nhint: x\nfatal: no remote"),
            use("b", "Bash", "rg -q x"), result("b", "Exit code 1"),
            use("c", "Edit", ""), result("c", "<tool_use_error>File has not been read yet.</tool_use_error>"),
            use("d", "mcp__kilix-needle__kilix_logs_read", ""), result("d", [{"type": "text", "text": envelope}]),
            use("e", "Read", ""), result("e", "fine", error=False)]


class ToolErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_first_provider_kinds_and_telling_lines(self):
        path = _write(self.root / "s.jsonl", _first_rows())
        result = tool_errors.scan("claude", path)
        self.assertEqual(result["calls"], {"Bash": 2, "Edit": 1, "Read": 1,
                                           "mcp__kilix-needle__kilix_logs_read": 1})
        got = [(f["tool"], f["kind"], f["exit_code"], f["telling"]) for f in result["failures"]]
        self.assertEqual(got, [
            ("Bash", "exit_code", 128, "fatal: no remote"),
            ("Bash", "silent_exit_1", 1, ""),
            ("Edit", "tool_use_error", None, "<tool_use_error>File has not been read yet.</tool_use_error>"),
            ("mcp__kilix-needle__kilix_logs_read", "mcp_error", None, "result_too_large: narrow the query")])
        self.assertEqual(result["failures"][0]["command"], "git fetch")
        self.assertEqual(result["failures"][0]["session"], "s-1")

    def test_codex_items_outputs_and_fallback(self):
        def item(body):
            return {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "item_completed", "item": body}}
        rows = [{"type": "session_meta", "payload": {"id": "thread-1"}},
                {"type": "response_item", "timestamp": STAMP, "payload": {
                    "type": "function_call", "name": "exec", "call_id": "x1", "arguments": "{}"}},
                item({"type": "CommandExecution", "status": "completed", "exit_code": 2,
                      "command": ["/bin/bash", "-lc", "sed -n 1p a; rg b missing"],
                      "aggregated_output": "line from a file mentioning error\nrg: missing: No such file or directory"}),
                item({"type": "CommandExecution", "status": "completed", "exit_code": 0,
                      "command": ["/bin/bash", "-lc", "true"], "aggregated_output": ""}),
                item({"type": "McpToolCall", "server": "kilix-needle", "tool": "kilix_action_act",
                      "status": "completed", "arguments": {"request": {}},
                      "result": {"isError": True, "content": [{"type": "text", "text": "blocked"}]}}),
                {"type": "response_item", "timestamp": STAMP, "payload": {
                    "type": "custom_tool_call_output", "call_id": "x1",
                    "output": [{"type": "input_text", "text": "Script failed\nScript error:\nbad image"}]}}]
        result = tool_errors.scan("codex", _write(self.root / "r.jsonl", rows))
        got = [(f["tool"], f["kind"], f["exit_code"], f["telling"], f["session"]) for f in result["failures"]]
        self.assertEqual(got, [
            ("shell", "exit_code", 2, "rg: missing: No such file or directory", "thread-1"),
            ("mcp:kilix-needle/kilix_action_act", "mcp_error", None, "blocked", "thread-1"),
            ("exec", "script_failed", None, "Script error:", "thread-1")])
        self.assertEqual(result["failures"][0]["command"], "sed -n 1p a; rg b missing")
        self.assertEqual(result["calls"]["shell"], 2)
        # Older rollouts without the item stream report shell status in text.
        old = [{"type": "response_item", "timestamp": STAMP, "payload": {
                    "type": "function_call", "name": "shell", "call_id": "y", "arguments": '{"command": ["ls"]}'}},
               {"type": "response_item", "timestamp": STAMP, "payload": {
                    "type": "function_call_output", "call_id": "y",
                    "output": "Process exited with code 2\nls: cannot access 'z': No such file or directory"}}]
        result = tool_errors.scan("codex", _write(self.root / "old.jsonl", old))
        self.assertEqual([(f["tool"], f["exit_code"], f["command"]) for f in result["failures"]],
                         [("shell", 2, "ls")])

    def test_grok_failures_mcp_and_benign_grep(self):
        def call(call_id, name, raw):
            return {"timestamp": 1794300000, "params": {"sessionId": "g-1", "update": {
                "sessionUpdate": "tool_call", "toolCallId": call_id, "title": name, "rawInput": raw,
                "_meta": {"x.ai/tool": {"name": name}}}}}

        def done(call_id, status, output):
            return {"timestamp": 1794300001, "params": {"sessionId": "g-1", "update": {
                "sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status,
                "rawOutput": output}}}
        envelope = json.dumps({"errors": [{"code": "unsupported_provider", "message": "grok"}]})
        rows = [call("1", "read_file", {"target_file": "/nope"}),
                done("1", "failed", {"type": "ReadFile", "FileNotFound": "Error: /nope does not exist."}),
                call("2", "use_tool", {"tool_input": {"session": "11"}}),
                done("2", "failed", {"type": "MCP", "server_name": "kilix-needle", "tool_name": "kilix_logs_read",
                                     "is_error": "True", "output": {"Error": envelope}}),
                call("3", "grep", {"pattern": "x"}),
                done("3", "completed", {"type": "GrepSearch", "exit_code": 1}),
                call("4", "run_terminal_command", {"command": "make"}),
                done("4", "completed", {"type": "Bash", "exit_code": 2, "command": "make"})]
        session = self.root / "sessions" / "cwd" / "g-1"
        _write(session / "updates.jsonl", rows)
        result = tool_errors.scan("grok", str(session))
        got = [(f["tool"], f["kind"], f["telling"]) for f in result["failures"]]
        self.assertEqual(got, [
            ("read_file", "failed:ReadFile/FileNotFound", "Error: /nope does not exist."),
            ("mcp:kilix-needle/kilix_logs_read", "mcp_error", "unsupported_provider: grok"),
            ("run_terminal_command", "exit_code", "")])
        self.assertEqual(result["calls"], {"read_file": 1, "mcp:kilix-needle/kilix_logs_read": 1,
                                           "grep": 1, "run_terminal_command": 1})
        self.assertEqual(result["failures"][1]["command"], "11")

    def test_group_folds_paths_and_numbers_and_filters_kilix(self):
        base = {"provider": "codex", "tool": "shell", "kind": "exit_code", "path": "p", "line": 1,
                "timestamp": STAMP, "exit_code": 2, "message": ""}
        failures = [base | {"session": "a", "command": "kilix @ get-text --match id:4",
                            "telling": "ls: cannot access '/tmp/a/1': No such file or directory"},
                    base | {"session": "b", "command": "ls /x",
                            "telling": "ls: cannot access '/var/b/22': No such file or directory"}]
        rows = tool_errors.group(failures)
        self.assertEqual([(r["count"], r["sessions"], r["signature"]) for r in rows],
                         [(2, 2, "ls: cannot access <q>: No such file or directory")])
        self.assertEqual([tool_errors.is_kilix(f) for f in failures], [True, False])
        self.assertTrue(tool_errors.is_kilix(base | {"tool": "mcp:kilix-needle/x", "command": ""}))
        self.assertFalse(tool_errors.is_kilix(base | {"command": "cd ~/kilix-notes && ls"}))

    def test_window_discovery_and_cli(self):
        rows = _first_rows()
        rows[1] = dict(rows[1], timestamp="2020-01-01T00:00:00Z")
        home = self.root / "first"
        _write(home / "projects" / "p" / "s.jsonl", rows)
        _write(home / "projects" / "p" / "s" / "subagents" / "agent-1.jsonl", _first_rows())
        paths = tool_errors.discover("claude", home=str(home))
        self.assertEqual(sorted(os.path.basename(p) for p in paths), ["agent-1.jsonl", "s.jsonl"])
        recent = tool_errors.scan("claude", paths[0], since=1.7e9)
        self.assertNotIn(("Bash", "exit_code"), [(f["tool"], f["kind"]) for f in recent["failures"]
                                                 if f["exit_code"] == 128 and paths[0].endswith("s.jsonl")])
        import main as cli
        self.assertAlmostEqual(cli.since_epoch("2h", now=10000.0), 2800.0)
        with self.assertRaises(Exception):
            cli.since_epoch("soon")
        env = {"CLAUDE_CONFIG_DIR": str(home), "CODEX_HOME": str(self.root / "none"),
               "GROK_HOME": str(self.root / "none")}
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["--since", "3650d", "--json", "--kilix"]), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["files"], {"claude": 2})
            self.assertEqual({row["tool"] for row in report["signatures"]},
                             {"mcp__kilix-needle__kilix_logs_read"})
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["--since", "3650d"])
            self.assertIn("silent exit-1 results", out.getvalue())
            self.assertIn("fatal: no remote", out.getvalue())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
