"""kilix rollout log --pane: one Kilix pane number, the log that pane is writing."""
import io
import json
import os
from contextlib import redirect_stdout
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_rollout.errors import ConflictError, NotFoundError, UsageError  # noqa: E402
from kilix_rollout.pane_log import Pane, PaneLog, Process, pane_from_window, resolve  # noqa: E402

CODEX_ID = "22222222-2222-4222-8222-222222222222"
CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
BROKER = "05377862f40eea71"


def load_tool():
    path = ROOT / "tools" / "rollout_resume" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_rollout_log", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_stat(proc: Path, pid: int, start: str = "100", ppid: int = 0) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    numbers = ["0"] * 19
    numbers[0] = str(ppid)
    numbers[18] = start
    (directory / "stat").write_text(f"{pid} (name) S " + " ".join(numbers) + "\n")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, pane, **homes):
        return resolve(pane, proc_root=str(self.proc), **homes)

    def test_codex_uses_the_rollout_the_process_has_open(self):
        rollout = self.root / f"rollout-2026-10-28T03-19-41-{CODEX_ID}.jsonl"
        rollout.write_text("{}\n")
        write_stat(self.proc, 42)
        (self.proc / "42" / "fd").mkdir()
        os.symlink(rollout, self.proc / "42" / "fd" / "4")
        transcript = self.root / "transcripts" / f"{BROKER}.log"
        transcript.parent.mkdir()
        transcript.write_text("terminal\n")
        pane = Pane("12", broker_session=BROKER, processes=[
            Process(["codex", "resume", CODEX_ID], pid=42),
        ])
        found = self.resolve(pane, transcript_directory=str(transcript.parent))
        self.assertEqual((found.kind, found.path), ("codex", str(rollout.resolve())))

    def test_codex_uses_the_resume_id_when_the_file_is_not_open(self):
        rollout = (self.root / "sessions" / "2026" / "10" / "28"
                   / f"rollout-2026-10-28T03-19-41-{CODEX_ID}.jsonl")
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        pane = Pane("12", processes=[Process(["codex", "--resume", CODEX_ID], pid=7)])
        found = self.resolve(pane, codex_home=str(self.root))
        self.assertEqual(found.path, str(rollout))

    def test_a_missing_codex_rollout_is_not_replaced_by_the_kilix_transcript(self):
        transcript = self.root / "transcripts" / f"{BROKER}.log"
        transcript.parent.mkdir()
        transcript.write_text("terminal\n")
        pane = Pane("12", broker_session=BROKER, processes=[Process(["codex"], pid=7)])
        with self.assertRaises(NotFoundError) as caught:
            self.resolve(pane, codex_home=str(self.root),
                         transcript_directory=str(transcript.parent))
        self.assertIn("codex", str(caught.exception))

    def test_a_shared_codex_daemon_keeps_the_rollout_this_process_started(self):
        from datetime import datetime
        btime = 1_700_000_000
        ticks = 500
        clk = os.sysconf("SC_CLK_TCK")
        started = btime + ticks / clk
        (self.proc / "stat").write_text(f"btime {btime}\n")
        write_stat(self.proc, 10, start=str(ticks), ppid=1)
        write_stat(self.proc, 11, start=str(ticks + clk), ppid=10)
        old_name = "rollout-2020-01-01T00-00-00-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
        new_stamp = datetime.fromtimestamp(started + 4).strftime("%Y-%m-%dT%H-%M-%S")
        new_name = f"rollout-{new_stamp}-{CODEX_ID}.jsonl"
        old = self.root / old_name
        new = self.root / new_name
        old.write_text("{}\n")
        new.write_text("{}\n")
        fd = self.proc / "11" / "fd"
        fd.mkdir()
        os.symlink(old, fd / "3")
        os.symlink(new, fd / "4")
        pane = Pane("2", processes=[Process(["codex", "--yolo"], pid=10)])
        found = self.resolve(pane, codex_home=str(self.root))
        self.assertEqual(found.path, str(new.resolve()))

    def test_two_open_codex_rollouts_are_a_conflict(self):
        first = self.root / "rollout-a.jsonl"
        second = self.root / "rollout-b.jsonl"
        first.write_text("{}\n")
        second.write_text("{}\n")
        write_stat(self.proc, 42)
        fd = self.proc / "42" / "fd"
        fd.mkdir()
        os.symlink(first, fd / "3")
        os.symlink(second, fd / "4")
        pane = Pane("12", processes=[Process(["codex"], pid=42)])
        with self.assertRaises(ConflictError):
            self.resolve(pane, codex_home=str(self.root))

    def test_claude_uses_the_registry_session_file(self):
        write_stat(self.proc, 9)
        registry = self.root / "sessions"
        registry.mkdir()
        (registry / "9.json").write_text(json.dumps({
            "sessionId": CLAUDE_ID, "pid": 9,
        }))
        log = self.root / "projects" / "-home-pleb-gpu-terminal" / f"{CLAUDE_ID}.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{}\n")
        pane = Pane("47", processes=[Process(["claude"], pid=9)])
        found = self.resolve(pane, claude_home=str(self.root))
        self.assertEqual((found.kind, found.path), ("claude", str(log)))

    def test_grok_uses_events_jsonl_for_the_resumed_session(self):
        cwd = "/home/pleb/gpu_terminal"
        from kilix_rollout import grok
        folder = Path(grok.session_dir(CODEX_ID, cwd, str(self.root)))
        folder.mkdir(parents=True)
        log = folder / "events.jsonl"
        log.write_text("{}\n")
        pane = Pane("49", cwd=cwd, processes=[
            Process(["grok", "--resume", CODEX_ID], pid=3, cwd=cwd),
        ])
        found = self.resolve(pane, grok_home=str(self.root))
        self.assertEqual((found.kind, found.path), ("grok", str(log)))

    def test_kimi_uses_the_wire_log_named_by_the_index(self):
        session_dir = self.root / "work" / "abc"
        log = session_dir / "agents" / "main" / "wire.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{}\n")
        with (self.root / "session_index.jsonl").open("w") as handle:
            handle.write(json.dumps({
                "sessionId": CLAUDE_ID, "sessionDir": str(session_dir),
            }) + "\n")
        pane = Pane("8", processes=[Process(["kimi", "--resume", CLAUDE_ID])])
        found = self.resolve(pane, kimi_home=str(self.root))
        self.assertEqual(found.path, str(log))

    def test_tmux_uses_the_live_capture_for_its_pane(self):
        capture = "12a70efdb24c41b595f5920fbfa19b66"
        logs = self.root / "logs"
        (logs / "metadata").mkdir(parents=True)
        (logs / "live").mkdir()
        (logs / "metadata" / f"{capture}.json").write_text(json.dumps({
            "capture_id": capture,
            "pane_id": "%3",
            "started_utc": "2026-10-29T01:00:00Z",
            "ended_utc": None,
            "archive_directory": str(logs / "archive"),
            "archive_prefix": f"stamp--name--p3--{capture}",
        }))
        live = logs / "live" / f"{capture}--000001.log"
        live.write_text("live\n")
        archive = logs / "archive"
        archive.mkdir()
        (archive / f"stamp--name--p3--{capture}--000000.log.zst").write_bytes(b"old")
        pane = Pane("4", env={"TMUX_PANE": "%3"}, processes=[Process(["tmux", "attach"])])
        found = self.resolve(pane, tmux_log_root=str(logs))
        self.assertEqual((found.kind, found.path), ("tmux", str(live)))

    def test_an_inherited_tmux_variable_does_not_hide_the_kilix_transcript(self):
        transcript = self.root / "transcripts" / f"{BROKER}.log"
        transcript.parent.mkdir()
        transcript.write_text("shell\n")
        pane = Pane(
            "3", broker_session=BROKER, env={"TMUX_PANE": "%9"},
            processes=[Process(["bash"])],
        )
        found = self.resolve(pane, transcript_directory=str(transcript.parent))
        self.assertEqual((found.kind, found.path), ("kilix", str(transcript)))

    def test_a_shell_pane_uses_the_kilix_transcript(self):
        transcript = self.root / "transcripts" / "recent" / f"{BROKER}.log.zst"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(b"archived")
        pane = Pane("5", broker_session=BROKER, processes=[Process(["bash", "--posix"])])
        found = self.resolve(pane, transcript_directory=str(self.root / "transcripts"))
        self.assertEqual(found.path, str(transcript))

    def test_codex_wins_when_the_pane_also_has_tmux(self):
        rollout = self.root / "sessions" / "2026" / "10" / "28" / f"rollout-x-{CODEX_ID}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        pane = Pane("12", env={"TMUX_PANE": "%1"}, processes=[
            Process(["tmux", "attach"]),
            Process(["codex", "--resume", CODEX_ID]),
        ])
        found = self.resolve(pane, codex_home=str(self.root))
        self.assertEqual(found.kind, "codex")

    def test_a_window_object_keeps_the_shell_when_it_is_the_program(self):
        window = {
            "id": 41,
            "cwd": "/home/pleb",
            "pid": 15,
            "cmdline": ["codex", "--resume", CODEX_ID],
            "foreground_processes": [],
            "env": {"KITTY_PTY_BROKER_SESSION": BROKER},
        }
        pane = pane_from_window(window, "41")
        self.assertEqual(pane.processes[0].argv[0], "codex")
        self.assertEqual(pane.broker_session, BROKER)


class CommandTests(unittest.TestCase):
    def test_log_prints_the_path_and_json_names_the_kind(self):
        tool = load_tool()
        found = PaneLog("41", "codex", "/logs/rollout.jsonl")
        pane = Pane("41")
        text = io.StringIO()
        with mock.patch.object(tool.pane_log, "load_live_pane", return_value=pane), \
                mock.patch.object(tool.pane_log, "resolve", return_value=found), \
                redirect_stdout(text):
            self.assertEqual(tool.main(["log", "--pane", "41"]), 0)
        self.assertEqual(text.getvalue().strip(), "/logs/rollout.jsonl")
        encoded = io.StringIO()
        with mock.patch.object(tool.pane_log, "load_live_pane", return_value=pane), \
                mock.patch.object(tool.pane_log, "resolve", return_value=found), \
                redirect_stdout(encoded):
            self.assertEqual(tool.main(["--json", "log", "--pane", "41"]), 0)
        self.assertEqual(json.loads(encoded.getvalue()), {
            "pane": 41, "kind": "codex", "path": "/logs/rollout.jsonl",
        })

    def test_log_requires_a_numeric_pane(self):
        tool = load_tool()
        with self.assertRaises(UsageError):
            tool.main(["log"])
        with self.assertRaises(UsageError):
            tool.main(["log", "--pane", "pane-41"])

    def test_help_names_the_command(self):
        tool = load_tool()
        text = io.StringIO()
        with redirect_stdout(text):
            self.assertEqual(tool.main(["log", "--help"]), 0)
        self.assertIn("log --pane PANE_ID", text.getvalue())


if __name__ == "__main__":
    unittest.main()
