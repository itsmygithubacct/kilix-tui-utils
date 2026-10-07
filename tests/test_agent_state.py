"""Agent state from structured evidence: an exactly held Codex rollout, Claude registry states.

Synthetic shapes only: fake /proc and fake CODEX_HOME trees under a temporary directory, no real session
content. A state is named only by evidence that is exact and current; anything else stays `agent`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_rollout import claude, codex, grok, liveness  # noqa: E402
from kilix_rollout.model import Session  # noqa: E402
from kilix_tui import kitty_rc, pane_center  # noqa: E402

BTIME = 1_700_000_000
START = BTIME + 5000          # when the fixture Codex started
TICKS = os.sysconf("SC_CLK_TCK")
SESSION = "0123456789abcdef0123456789abcdef"


def uuid(n: int) -> str:
    return f"{n:08x}-1234-4234-8234-123456789abc"


class Home:
    """A fake /proc and a fake CODEX_HOME in a temporary directory."""

    def __init__(self, test: unittest.TestCase):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-state-")
        test.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.proc = self.root / "proc"
        self.codex_home = self.root / "codex"
        self.proc.mkdir()
        (self.proc / "stat").write_text(f"cpu 1 2 3\nbtime {BTIME}\n")
        (self.codex_home / "sessions").mkdir(parents=True)
        self.cwd = self.root / "work"
        self.cwd.mkdir()

    def process(self, pid, argv, *, start=START, cwd=None, environ=None, parent=1):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        ticks = int((start - BTIME) * TICKS)
        (folder / "stat").write_text(
            f"{pid} ({os.path.basename(argv[0])}) S {parent} {pid} {pid} 34816 {pid} 4194304 "
            f"0 0 0 0 0 0 0 0 20 0 1 0 {ticks}\n")
        link = folder / "cwd"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(cwd or self.cwd)
        (folder / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        env = {"HOME": "/srv/home"} if environ is None else environ
        (folder / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        (folder / "fd").mkdir(exist_ok=True)

    def rollout(self, number, *, stamp, cwd=None, events=("task_complete",), originator="codex-tui",
                source="cli", thread_source="user", name=None, extra=()):
        """A rollout whose session_meta is stamped `stamp` (Unix time), under its UTC day folder."""
        moment = datetime.fromtimestamp(stamp, timezone.utc)
        folder = self.codex_home / "sessions" / moment.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or f"rollout-{moment.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid(number)}.jsonl")
        records = [{"timestamp": moment.isoformat().replace("+00:00", "Z"), "type": "session_meta",
                    "payload": {"id": uuid(number), "timestamp": moment.isoformat().replace("+00:00", "Z"),
                                "cwd": str(cwd or self.cwd), "originator": originator, "source": source,
                                "thread_source": thread_source, "cli_version": "0.160.1"}},
                   {"type": "turn_context", "payload": {"cwd": str(cwd or self.cwd)}}]
        for event in events:
            records.append({"type": "event_msg", "payload": {"type": event}})
        for record in extra:
            records.append(record)
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        os.utime(path, (stamp + 60, stamp + 60))
        return path


class CodexStatesFromEvents(unittest.TestCase):
    def state(self, events, *, extra=(), pids=(11,)):
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=events, extra=extra)
        return codex.session_from_path(str(path), pids=pids)

    def test_every_mapped_state(self):
        cases = (
            (("task_started", "task_complete"), "idle"),
            (("task_complete", "task_started"), "working"),
            (("task_started", "turn_aborted"), "idle"),                # Esc ends the turn
            (("task_started", "task_aborted"), "idle"),
            (("task_started", "turn_aborted", "task_started"), "working"),
            (("task_started", "task_complete", "task_started", "turn_aborted"), "idle"),
            ((), "unknown"),                                           # no turn yet: not asserted
            (("token_count",), "unknown"),
        )
        for events, expected in cases:
            with self.subTest(events=events):
                self.assertEqual(self.state(events).live_status, expected)

    def test_a_pending_approval_is_waiting_and_a_resolved_one_is_not(self):
        pending = [{"type": "event_msg", "payload": {"type": "exec_approval_request", "call_id": "x"}}]
        resolved = pending + [{"type": "event_msg", "payload": {"type": "exec_command_begin", "call_id": "x"}}]
        self.assertEqual(self.state(("task_started",), extra=pending).live_status, "waiting")
        self.assertEqual(self.state(("task_started",), extra=resolved).live_status, "working")
        patch = [{"type": "event_msg", "payload": {"type": "apply_patch_approval_request"}}]
        self.assertEqual(self.state(("task_started",), extra=patch).live_status, "waiting")

    def ev(self, kind, call_id=None):
        payload = {"type": kind}
        if call_id:
            payload["call_id"] = call_id
        return {"type": "event_msg", "payload": payload}

    def test_a_partial_or_malformed_newest_record_is_unknown_never_an_older_idle(self):
        home = Home(self)
        for tail in ('{"type": "event_msg", "payload": {"type": "task_sta',       # torn mid-write
                     '{"type": "event_msg", "payload": {"type": "task_started"',  # unterminated object
                     "not json task_started\n",
                     '{"type": "event_msg", "payload": {"type": "task_started"}}}\n',
                     "[1, 2]\n"):
            path = home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
            with open(path, "a") as handle:
                handle.write(tail)
            got = codex.session_from_path(str(path), pids=(11,))
            self.assertEqual((got.live_status, got.last_turn_event), ("unknown", ""), tail)

    def test_an_unreadable_older_record_that_is_not_a_turn_event_does_not_hide_the_state(self):
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=("task_started",))
        text = path.read_text().splitlines()
        text.insert(2, "garbage with no markers")
        path.write_text("\n".join(text) + "\n")
        self.assertEqual(codex.session_from_path(str(path), pids=(11,)).live_status, "working")

    def test_a_marker_bearing_unreadable_record_inside_the_open_turn_is_unknown(self):
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=("task_started",))
        with open(path, "a") as handle:
            handle.write('{"type": "event_msg", "payload": {"type": "exec_approval_request", "call_id": "z"\n')
            handle.write(json.dumps(self.ev("token_count")) + "\n")
        self.assertEqual(codex.session_from_path(str(path), pids=(11,)).live_status, "unknown")

    def test_approvals_are_tracked_by_call_id(self):
        a, b = self.ev("exec_approval_request", "a"), self.ev("exec_approval_request", "b")
        patch = self.ev("apply_patch_approval_request", "c")
        cases = (
            ([a, b], "waiting"),
            ([a, b, self.ev("exec_command_begin", "a")], "waiting"),                      # b still pending
            ([a, b, self.ev("exec_command_begin", "a"), self.ev("exec_command_begin", "b")], "working"),
            ([a, self.ev("exec_command_begin", "other")], "waiting"),                      # unrelated command
            ([a, self.ev("exec_command_end", "other"), self.ev("patch_apply_begin", "x")], "waiting"),
            ([a, patch, self.ev("patch_apply_begin", "c")], "waiting"),
            ([a, patch, self.ev("patch_apply_begin", "c"), self.ev("exec_command_end", "a")], "working"),
            ([a, self.ev("exec_command_begin")], "waiting"),                               # a command with no id resolves nothing
            ([{"type": "event_msg", "payload": {"type": "exec_approval_request", "approval_id": "q"}},
              self.ev("exec_command_begin", "q")], "working"),                             # the serializer's approval id
            ([{"type": "event_msg", "payload": {"type": "exec_approval_request", "approval_id": "q", "call_id": "r"}},
              self.ev("exec_command_begin", "other")], "waiting"),
        )
        for extra, expected in cases:
            with self.subTest(extra=[(e["payload"]["type"], e["payload"].get("call_id")) for e in extra]):
                self.assertEqual(self.state(("task_started",), extra=extra).live_status, expected)

    def test_an_approval_from_a_finished_turn_is_not_pending(self):
        late = [self.ev("exec_approval_request", "a")]           # recorded after the turn ended: no open turn to wait in
        self.assertEqual(self.state(("task_started", "task_complete"), extra=late).live_status, "idle")
        self.assertEqual(self.state(("task_started", "turn_aborted"), extra=late).live_status, "idle")
        old = [self.ev("exec_approval_request", "a")]
        self.assertEqual(self.state(("task_started",), extra=old + [self.ev("task_complete")]).live_status, "idle")
        got = self.state(("task_started", "task_complete", "task_started"), extra=[])
        self.assertEqual(got.live_status, "working")

    def test_a_rollout_with_no_owner_is_not_live(self):
        self.assertEqual(self.state(("task_started",), pids=()).live_status, "")


def tree_for(*panes):
    """panes: (window id, [(pid, argv)], cwd)"""
    return kitty_rc.parse([{"id": 1, "is_focused": True, "tabs": [{"id": 2, "title": "w", "is_active": True, "windows": [
        {"id": wid, "pid": 70 + wid, "title": f"p{wid}", "cwd": cwd, "env": {"KITTY_PTY_BROKER_SESSION": SESSION},
         "foreground_processes": [{"pid": pid, "cmdline": argv, "cwd": cwd} for pid, argv in procs]}
        for wid, procs, cwd in panes]}]}])


class InspectorHoldsOneRollout(unittest.TestCase):
    """A Codex pane has a state only through the one rollout it holds open; anything else is `agent`."""

    def setUp(self):
        self.home = Home(self)
        self.panes = {}

    def snapshot(self, tree):
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value={}), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home.codex_home)}):
            return pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree)

    def codex_pane(self, wid=9, pid=100, start=START, argv=("/opt/vendor/codex", "--yolo")):
        self.home.process(pid, list(argv), start=start)
        return (wid, [(pid, list(argv))], str(self.home.cwd))

    def hold(self, pid, path, fd=4):
        os.symlink(path, self.home.proc / str(pid) / "fd" / str(fd))

    def test_the_held_rollouts_events_name_idle_working_and_waiting(self):
        request = {"type": "event_msg", "payload": {"type": "exec_approval_request", "call_id": "a"}}
        for events, extra, expected in ((("task_started", "task_complete"), (), "idle"),
                                        (("task_started",), (), "working"),
                                        (("task_started",), [request], "waiting"),
                                        (("task_started", "turn_aborted"), (), "idle")):
            self.home = Home(self)
            path = self.home.rollout(1, stamp=START + 2, events=events, extra=extra)
            pane = self.codex_pane()
            self.hold(100, path)
            got = self.snapshot(tree_for(pane)).panes[0]
            self.assertEqual((got.activity, got.coding.provider, got.coding.session_id), (expected, "codex", uuid(1)), events)

    def test_a_pane_that_holds_nothing_stays_agent_whatever_the_directory_holds(self):
        """Time, directory, originator, argv and filename name no session; there is no such guess left."""
        self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        for argv in (("codex", "--yolo"), ("codex", "resume", uuid(1)), ("codex", "-C", str(self.home.cwd)),
                     ("codex", "exec", "go"), ("codex", "resume", "--last")):
            self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
            got = self.snapshot(tree_for(self.codex_pane(argv=argv))).panes[0]
            self.assertEqual((got.activity, got.coding.live_status), ("agent", "unknown"), argv)   # the id is a label only

    def test_two_different_rollouts_held_by_one_pane_are_ambiguous(self):
        first = self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        second = self.home.rollout(2, stamp=START + 3, events=("task_started",))
        pane = self.codex_pane()
        self.hold(100, first, 4)
        self.hold(100, second, 5)
        self.assertEqual(self.snapshot(tree_for(pane)).panes[0].activity, "agent")

    def test_the_same_rollout_held_twice_in_one_pane_is_still_one_session(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        self.home.process(101, ["/opt/vendor/codex", "--yolo"], parent=100)
        pane = (9, [(100, ["node", "/usr/local/bin/codex"]), (101, ["/opt/vendor/codex", "--yolo"])], str(self.home.cwd))
        self.home.process(100, ["node", "/usr/local/bin/codex"])
        self.hold(100, path, 4)
        self.hold(101, path, 5)
        got = self.snapshot(tree_for(pane)).panes[0]
        self.assertEqual((got.activity, got.coding.pids), ("working", (100, 101)))

    def test_a_descriptor_claimed_by_two_panes_is_nobodys(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        one, two = self.codex_pane(9, 100), self.codex_pane(10, 200)
        self.hold(100, path)
        self.hold(200, path)
        self.assertEqual([p.activity for p in self.snapshot(tree_for(one, two)).panes], ["agent", "agent"])

    def test_canonical_paths_are_compared(self):
        """A symlinked alias of one rollout is that rollout (one session), and held by two panes it is still shared."""
        path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        alias = self.home.root / "rollout-alias.jsonl"
        os.symlink(path, alias)
        one, two = self.codex_pane(9, 100), self.codex_pane(10, 200)
        self.hold(100, path)
        self.hold(200, alias)
        self.assertEqual([p.activity for p in self.snapshot(tree_for(one, two)).panes], ["agent", "agent"])
        solo = self.codex_pane(11, 300)
        self.hold(300, alias, 7)
        self.hold(300, path, 8)
        self.assertEqual(self.snapshot(tree_for(solo)).panes[0].activity, "working")

    def test_only_a_codex_process_holding_a_rollout_names_it(self):
        """`tail -f` or an editor on a rollout is not the session's owner."""
        path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        self.home.process(100, ["tail", "-f", str(path)])
        self.hold(100, path)
        pane = (9, [(100, ["tail", "-f", str(path)])], str(self.home.cwd))
        got = self.snapshot(tree_for(pane)).panes[0]
        self.assertIsNone(got.coding)
        self.assertNotEqual(got.activity, "idle")        # an ordinary foreground program: no agent state

    def test_a_rollout_that_cannot_be_read_stays_agent(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        pane = self.codex_pane()
        self.hold(100, path)
        path.unlink()
        self.assertEqual(self.snapshot(tree_for(pane)).panes[0].activity, "agent")

    def test_the_resolver_and_its_cache_are_gone(self):
        for name in ("Instance", "resolve_instances", "resume_id", "_META_CACHE", "_candidates"):
            self.assertFalse(hasattr(codex, name), name)
        for name in ("_codex_by_start", "_match_codex", "_codex_unclear"):
            self.assertFalse(hasattr(pane_center.Inspector, name) or hasattr(pane_center, name), name)


class ClaudeRecords(unittest.TestCase):
    """The registry names a live Claude only through a record whose start time is the process's."""

    def setUp(self):
        self.home = Home(self)
        self.claude = self.home.root / "claude"
        (self.claude / "sessions").mkdir(parents=True)

    def record(self, pid, *, session=None, start="", status="idle", name=None, **extra):
        data = {"pid": pid, "sessionId": session or uuid(pid), "status": status, "cwd": str(self.home.cwd), **extra}
        if start != "":
            data["procStart"] = start
        (self.claude / "sessions" / (name or f"{pid}.json")).write_text(json.dumps(data))

    def ticks(self, pid):
        return liveness.start_ticks(pid, proc_root=str(self.home.proc))

    def activity(self, pid=91):
        tree = tree_for((9, [(pid, ["claude"])], str(self.home.cwd)))
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "HOME": str(self.home.root)}):
            return pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree).panes[0]

    def test_a_record_with_the_live_start_time_names_the_state(self):
        self.home.process(91, ["claude"])
        for status, expected in (("idle", "idle"), ("shell", "idle"), ("busy", "working"), ("waiting", "waiting")):
            self.record(91, start=self.ticks(91), status=status)
            self.assertEqual(self.activity().activity, expected, status)

    def test_a_record_without_procstart_is_not_identity(self):
        self.home.process(91, ["claude"])
        self.record(91, status="busy")
        got = self.activity()
        self.assertEqual(got.activity, "agent")

    def test_a_record_for_another_start_time_is_a_reused_pid(self):
        self.home.process(91, ["claude"])
        self.record(91, start=str(int(self.ticks(91)) + 7), status="busy")
        self.assertEqual(self.activity().activity, "agent")

    def test_two_records_for_one_pid_and_start_are_ambiguous(self):
        self.home.process(91, ["claude"])
        self.record(91, session=uuid(1), start=self.ticks(91), status="idle", name="a.json")
        self.record(91, session=uuid(2), start=self.ticks(91), status="busy", name="b.json")
        self.assertEqual(self.activity().activity, "agent")

    def test_a_malformed_status_is_agent(self):
        self.home.process(91, ["claude"])
        self.record(91, start=self.ticks(91), status="sleeping")
        self.assertEqual(self.activity().activity, "agent")


class ClaudeRegistryStates(unittest.TestCase):
    def activity(self, status):
        session = Session(provider="claude", session_id="s", path="", cwd="/srv/x", title="", updated=0,
                          state="live", pids=(1,), live_status=status)
        pane = kitty_rc.parse([{"id": 1, "tabs": [{"id": 2, "windows": [{"id": 9, "pid": 70, "cwd": "/srv/x"}]}]}]).panes[0]
        return pane_center._activity(session, pane)

    def test_each_registry_status_has_one_meaning(self):
        for status, expected in (("idle", "idle"), ("shell", "idle"), ("busy", "working"), ("waiting", "waiting"),
                                 ("IDLE", "idle"), (" Shell ", "idle")):
            self.assertEqual(self.activity(status), expected, status)

    def test_anything_else_stays_an_unreadable_agent(self):
        for status in ("", "unknown", "running", "active", "ready", "blocked", "sleeping", "shell-ish"):
            self.assertEqual(self.activity(status), "agent", status)

    def test_the_mapping_is_claudes_alone(self):
        other = Session(provider="grok", session_id="s", path="", cwd="/srv/x", title="", updated=0,
                        state="live", pids=(1,), live_status="shell")
        pane = kitty_rc.parse([{"id": 1, "tabs": [{"id": 2, "windows": [{"id": 9, "pid": 70, "cwd": "/srv/x"}]}]}]).panes[0]
        self.assertEqual(pane_center._activity(other, pane), "agent")
        self.assertEqual(claude.ACTIVITY, {"idle": "idle", "shell": "idle", "busy": "working", "waiting": "waiting"})

    def test_a_claude_pane_with_a_background_monitor_reads_idle_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = tree_for((9, [(91, ["claude", "--resume", uuid(9)])], "/srv/x"))
            records = {91: (uuid(9), {"status": "shell", "cwd": "/srv/x"})}
            for status, expected in (("shell", "idle"), ("idle", "idle"), ("busy", "working"), ("waiting", "waiting"), ("odd", "agent")):
                records[91] = (uuid(9), {"status": status, "cwd": "/srv/x"})
                with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                        mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value=records):
                    got = pane_center.Inspector(proc_root=temporary).snapshot(tree).panes[0]
                self.assertEqual(got.activity, expected, status)


if __name__ == "__main__":
    unittest.main()
