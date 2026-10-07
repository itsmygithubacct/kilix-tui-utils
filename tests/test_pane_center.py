"""The pane center's joined state, bounded input, and agent-facing CLI."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_rollout import codex, grok, omp  # noqa: E402
from kilix_rollout.model import Session  # noqa: E402
from kilix_tui import kitty_rc, pane_center  # noqa: E402


SESSION = "0123456789abcdef0123456789abcdef"
CODEX_ID = "12345678-1234-4234-8234-123456789abc"


def load_tool():
    path = ROOT / "tools" / "switcher" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_pane_center", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def tree() -> kitty_rc.Tree:
    return kitty_rc.parse([{
        "id": 1,
        "is_focused": True,
        "tabs": [{
            "id": 2,
            "title": "work",
            "is_active": True,
            "windows": [{
                "id": 9,
                "pid": 70,
                "title": "agent",
                "cwd": "/tmp/project",
                "is_focused": True,
                "env": {"KITTY_PTY_BROKER_SESSION": SESSION},
                "foreground_processes": [{
                    "pid": 77,
                    "cmdline": ["node", "/usr/local/bin/codex", "--yolo"],
                    "cwd": "/tmp/project",
                }, {
                    "pid": 78,
                    "cmdline": ["/opt/vendor/codex", "--yolo"],
                    "cwd": "/tmp/project",
                }],
            }],
        }],
    }])


def coding(*, status: str = "idle") -> Session:
    return Session(
        provider="codex", session_id=CODEX_ID, path="/tmp/rollout.jsonl",
        cwd="/tmp/project", title="build pane center", updated=100,
        state="live", pids=(78,), live_status=status,
        last_user_message="build pane center",
    )


def snapshot(*, activity: str = "idle", self_id: int = 0) -> pane_center.Snapshot:
    pane = tree().panes[0]
    info = pane_center.PaneInfo(
        pane=pane, page_index=1, activity=activity,
        doing="build pane center", coding=coding(status=activity),
    )
    return pane_center.Snapshot(panes=[info], self_pane_id=self_id)


class KittyModelTests(unittest.TestCase):
    def test_terminal_payload_keeps_process_and_broker_identity(self):
        pane = tree().panes[0]
        self.assertEqual(pane.child_pid, 70)
        self.assertEqual(pane.pids, (77, 78))
        self.assertEqual(pane.process, "codex")
        self.assertEqual(pane.broker_session, SESSION)

    def test_opaque_process_command_does_not_become_a_huge_label(self):
        process = kitty_rc.Process(
            pid=3, argv=("/opt/chrome/chrome --type=renderer --flag=yes",))
        self.assertEqual(process.name, "chrome")

    def test_long_unicode_input_is_split_on_policy_boundaries(self):
        pane = tree().panes[0]
        calls = []

        def run(args, *, input_text=None):
            calls.append((args, input_text))
            return ""

        value = "é" * 700                 # 1400 UTF-8 bytes
        with mock.patch.object(kitty_rc, "_run", side_effect=run):
            self.assertEqual(kitty_rc.send_text(pane, value), 1400)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(text.encode("utf-8")) <= 1024
                            for _args, text in calls))
        self.assertTrue(all(
            f"env:KITTY_PTY_BROKER_SESSION={SESSION}" in args
            and "id:9" not in args
            for args, _text in calls
        ))

    def test_input_without_a_broker_identity_is_refused(self):
        with self.assertRaises(kitty_rc.Unavailable):
            kitty_rc.send_text(kitty_rc.Pane(9), "hello")

    def test_scrollback_is_only_read_when_explicit(self):
        with mock.patch.object(kitty_rc, "_run", return_value="one\ntwo\n") as run:
            self.assertEqual(
                kitty_rc.pane_text(9, lines=1, scrollback=True), "two")
        self.assertEqual(
            run.call_args.args[0],
            ["get-text", "--match", "id:9", "--extent", "all"],
        )


class CodexStateTests(unittest.TestCase):
    @staticmethod
    def write_rollout(path: Path, boundary: str) -> None:
        records = [
            {"type": "session_meta", "payload": {
                "id": CODEX_ID, "cwd": "/tmp/project"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "turn_context", "payload": {"cwd": "/tmp/project"}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "real task"}]}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text":
                             "<codex_internal_context>ignore me</codex_internal_context>"}]}},
        ]
        if boundary == "approval":
            # Shape emitted by Codex 0.157.1's rollout serializer for an exec
            # approval (field names verified from the installed client).
            records.append({"type": "event_msg", "payload": {
                "type": "exec_approval_request", "approval_id": "approval-1",
                "command": ["make", "test"], "cwd": "/tmp/project",
                "reason": "run the project test suite",
                "available_decisions": ["approved", "denied"]}})
        elif boundary == "approval-resolved":
            records.extend([
                {"type": "event_msg", "payload": {
                    "type": "exec_approval_request", "approval_id": "approval-1",
                    "command": ["make", "test"], "cwd": "/tmp/project"}},
                {"type": "event_msg", "payload": {
                    "type": "exec_command_begin", "call_id": "approval-1",
                    "command": ["make", "test"], "cwd": "/tmp/project"}},
            ])
        elif boundary == "task_complete":
            records.append({"type": "event_msg", "payload": {
                "type": "task_complete"}})
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_live_rollout_distinguishes_completed_and_started_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            complete = Path(temporary) / f"rollout-{CODEX_ID}.jsonl"
            self.write_rollout(complete, "task_complete")
            idle = codex.session_from_path(str(complete), pids=(77,))
            self.assertIsNotNone(idle)
            self.assertEqual(idle.state, "live")
            self.assertEqual(idle.live_status, "idle")
            self.assertEqual(idle.last_user_message, "real task")

            working_path = Path(temporary) / "next" / f"rollout-{CODEX_ID}.jsonl"
            self.write_rollout(working_path, "task_started")
            working = codex.session_from_path(str(working_path), pids=(78,))
            self.assertEqual(working.live_status, "working")

    def test_live_rollout_reports_a_pending_codex_approval_as_waiting(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending_path = Path(temporary) / f"rollout-{CODEX_ID}.jsonl"
            self.write_rollout(pending_path, "approval")
            pending = codex.session_from_path(str(pending_path), pids=(77,))
            self.assertEqual(pending.live_status, "waiting")
            self.assertEqual(pending.pending_tool, "command approval")

            resolved_path = Path(temporary) / "resolved" / f"rollout-{CODEX_ID}.jsonl"
            self.write_rollout(resolved_path, "approval-resolved")
            resolved = codex.session_from_path(str(resolved_path), pids=(78,))
            self.assertEqual(resolved.live_status, "working")
            self.assertEqual(resolved.pending_tool, "")

    def test_a_codex_pane_is_agent_even_when_it_holds_a_rollout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rollout = root / "sessions" / f"rollout-{CODEX_ID}.jsonl"
            self.write_rollout(rollout, "task_complete")
            descriptors = root / "proc" / "78" / "fd"
            descriptors.mkdir(parents=True)
            os.symlink(rollout, descriptors / "4")
            with mock.patch.object(
                pane_center, "_broker_statuses", return_value=({}, False, "")
            ), mock.patch.object(
                pane_center.Inspector, "_claude_by_pid", return_value={}
            ):
                got = pane_center.Inspector(
                    proc_root=str(root / "proc")).snapshot(tree())
            self.assertEqual(got.panes[0].activity, "agent")
            self.assertEqual(got.panes[0].coding.provider, "codex")
            self.assertEqual(got.panes[0].coding.live_status, "unknown")


def agent_tree(argv: list[str], pid: int = 91) -> kitty_rc.Tree:
    return kitty_rc.parse([{"id": 1, "is_focused": True, "tabs": [{
        "id": 2, "title": "work", "is_active": True, "windows": [{
            "id": 9, "pid": 70, "title": "agent", "cwd": "/tmp/project",
            "is_focused": True, "env": {"KITTY_PTY_BROKER_SESSION": SESSION},
            "foreground_processes": [{"pid": pid, "cmdline": argv, "cwd": "/tmp/project"}],
    }]}]}])


def two_agent_tree(first, second) -> kitty_rc.Tree:
    return kitty_rc.parse([{"id": 1, "is_focused": True, "tabs": [{
        "id": 2, "title": "work", "is_active": True, "windows": [
            {"id": 9, "pid": 70, "title": "one", "cwd": "/tmp/project",
             "foreground_processes": [{"pid": 91, "cmdline": first,
                                        "cwd": "/tmp/project"}]},
            {"id": 10, "pid": 71, "title": "two", "cwd": "/tmp/project",
             "foreground_processes": [{"pid": 92, "cmdline": second,
                                        "cwd": "/tmp/project"}]},
        ]}]}])


class GrokAndOmpStateTests(unittest.TestCase):
    """kilix-needle's agents job: idle detection for grok and qwen-omp panes."""

    def inspect(self, argv, proc_root):
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value={}):
            return pane_center.Inspector(proc_root=proc_root).snapshot(agent_tree(argv)).panes[0]

    def test_a_live_grok_pane_reports_its_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(grok, "active", return_value={"sid": (91, "/tmp/project")}), \
                    mock.patch.object(grok, "activity", side_effect=["idle", "working", "waiting"]):
                for want in ("idle", "working", "waiting"):
                    got = self.inspect(["/home/u/.grok/bin/grok"], temporary)
                    self.assertEqual((got.activity, got.coding.session_id), (want, "sid"))

    def test_a_live_omp_pane_uses_the_newest_session_since_it_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Session(provider="omp", session_id="osid", path="/p", cwd="/tmp/project",
                              title="t", updated=5.0)
            with mock.patch.object(pane_center.liveness, "start_time", return_value=4.0), \
                    mock.patch.object(omp, "newest_in", return_value=session) as newest, \
                    mock.patch.object(omp, "activity", return_value="idle"):
                got = self.inspect(["omp", "--model", "qwen3.8-max"], temporary)
            newest.assert_called_once_with("/tmp/project", after=4.0)
            self.assertEqual((got.activity, got.coding.session_id), ("idle", "osid"))

    def test_two_omp_panes_in_one_directory_do_not_share_the_newest_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                    mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value={}), \
                    mock.patch.object(pane_center.liveness, "start_time", return_value=4.0), \
                    mock.patch.object(omp, "session_for_pid", return_value=None), \
                    mock.patch.object(omp, "newest_in") as newest:
                got = pane_center.Inspector(proc_root=temporary).snapshot(
                    two_agent_tree(["omp"], ["omp"]))
            newest.assert_not_called()
            self.assertEqual([item.activity for item in got.panes], ["agent", "agent"])

    def test_a_fresh_omp_breadcrumb_never_falls_back_to_another_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                    mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value={}), \
                    mock.patch.object(pane_center.liveness, "start_time", return_value=4.0), \
                    mock.patch.object(omp, "session_for_pid", return_value=None), \
                    mock.patch.object(omp, "has_terminal_marker", return_value=True), \
                    mock.patch.object(omp, "newest_in") as newest:
                got = self.inspect(["omp", "--model", "qwen3.8-max"], temporary)
            newest.assert_not_called()
            self.assertEqual(got.activity, "agent")

    def test_outer_claude_owns_a_pane_with_an_omp_child(self):
        nested = kitty_rc.parse([{"id": 1, "tabs": [{"id": 2, "windows": [{
            "id": 9, "pid": 70, "title": "claude", "cwd": "/tmp/project",
            "foreground_processes": [
                {"pid": 91, "cmdline": ["claude"], "cwd": "/tmp/project"},
                {"pid": 92, "cmdline": ["omp", "-p", "review"],
                 "cwd": "/tmp/project"},
            ],
        }]}]}])
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                    mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value={
                        91: ("claude-sid", {"status": "busy", "cwd": "/tmp/project"})
                    }), mock.patch.object(omp, "newest_in") as newest:
                got = pane_center.Inspector(proc_root=temporary).snapshot(nested).panes[0]
            newest.assert_not_called()
            self.assertEqual((got.coding.provider, got.activity), ("claude", "working"))

    def test_without_a_session_record_the_pane_is_only_an_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(grok, "active", return_value={}), \
                    mock.patch.object(pane_center.liveness, "start_time", return_value=0.0), \
                    mock.patch.object(omp, "newest_in") as newest:
                self.assertEqual(self.inspect(["grok"], temporary).activity, "agent")
                self.assertEqual(self.inspect(["omp"], temporary).activity, "agent")
            newest.assert_not_called()


class SnapshotTests(unittest.TestCase):
    def test_json_contract_and_session_prefix_resolution(self):
        got = snapshot()
        self.assertEqual(got.resolve(CODEX_ID[:10]).pane.id, 9)
        payload = got.to_dict()
        self.assertEqual(payload["schema"], "kilix.panes/v1")
        self.assertEqual(payload["counts"], {"pages": 1, "panes": 1})
        self.assertEqual(
            payload["panes"][0]["coding_session"]["live_status"], "idle")

    def test_cli_dump_is_a_small_machine_readable_envelope(self):
        tool = load_tool()
        output = io.StringIO()
        with mock.patch.object(tool, "_live", return_value=snapshot()), \
             mock.patch.object(kitty_rc, "pane_text", return_value="one\ntwo"), \
             redirect_stdout(output):
            self.assertEqual(tool.cli(["dump", "9", "-n", "2", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], "kilix.panes.dump/v1")
        self.assertEqual(payload["text"], "one\ntwo")

    def test_cli_refuses_to_inject_into_its_own_pane_by_default(self):
        tool = load_tool()
        error = io.StringIO()
        with mock.patch.object(tool, "_live", return_value=snapshot(self_id=9)), \
             mock.patch.object(kitty_rc, "send_text") as send, \
             redirect_stderr(error):
            self.assertEqual(tool.cli(["send", "9", "hello"]), 1)
        send.assert_not_called()
        self.assertIn("refusing to type into this pane", error.getvalue())

    def test_cli_send_can_submit_one_resolved_agent_prompt(self):
        tool = load_tool()
        output = io.StringIO()
        with mock.patch.object(tool, "_live", return_value=snapshot()), \
             mock.patch.object(kitty_rc, "send_text", return_value=6) as send, \
             redirect_stdout(output):
            self.assertEqual(
                tool.cli(["send", "9", "--enter", "hello"]), 0)
        self.assertEqual(send.call_args_list, [
            mock.call(tree().panes[0], "hello"),
            mock.call(tree().panes[0], "\r")])
        self.assertIn("accepted 12 bytes and Enter", output.getvalue())

    def test_cli_send_enter_alone_sends_one_carriage_return(self):
        tool = load_tool()
        with mock.patch.object(tool, "_live", return_value=snapshot()), \
             mock.patch.object(kitty_rc, "send_text", return_value=1) as send, \
             mock.patch.object(tool.sys.stdin, "isatty", return_value=True), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(tool.cli(["send", "9", "--enter"]), 0)
        send.assert_called_once_with(tree().panes[0], "\r")

    def test_cli_close_says_what_it_closed(self):
        tool = load_tool()
        output = io.StringIO()
        pane = tree().panes[0]
        with mock.patch.object(tool, "_live", return_value=snapshot()), \
             mock.patch.object(kitty_rc, "close_pane") as close, \
             redirect_stdout(output):
            self.assertEqual(tool.cli(["close", "9"]), 0)
        close.assert_called_once_with(pane.id)
        self.assertEqual(output.getvalue(),
                         f"closed pane {pane.id} {pane.title!r}\n")

    def test_bare_command_without_a_terminal_lists_instead_of_refusing(self):
        tool = load_tool()
        with mock.patch.object(tool.sys.stdin, "isatty", return_value=False), \
             mock.patch.object(tool, "cli", return_value=0) as cli, \
             mock.patch.object(tool, "_tui") as tui:
            self.assertEqual(tool.main([]), 0)
        cli.assert_called_once_with(["list"])
        tui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
