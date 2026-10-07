"""Agent state from structured evidence: Codex rollouts without an open descriptor, Claude registry states.

Synthetic shapes only: fake /proc and fake CODEX_HOME trees under a temporary directory, no real session
content. The reader must name a rollout only when nothing else could be that process's session; anything
uncertain stays `agent`.
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

    def instance(self, key=1, *, start=START, argv=None, **kwargs):
        return codex.Instance(key=key, start=start, cwd=str(self.cwd), home=str(self.codex_home),
                              pids=(key,), **kwargs)

    def resolve(self, *instances, skip=frozenset()):
        return codex.resolve_instances(instances, now=START + 600, skip=skip)


class CodexOwnership(unittest.TestCase):
    def setUp(self):
        self.home = Home(self)
        codex._META_CACHE.clear()

    def test_one_rollout_just_after_the_start_is_that_instances_session(self):
        path = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(path)})

    def test_the_window_edges(self):
        for delta, owned in ((-2.0, True), (-2.5, False), (0.0, True), (30.0, True), (31.0, False), (300.0, False)):
            codex._META_CACHE.clear()
            home = Home(self)
            path = home.rollout(1, stamp=START + delta)
            got = home.resolve(home.instance())
            self.assertEqual(got, {1: str(path)} if owned else {}, delta)

    def test_a_rollout_in_another_directory_is_not_a_candidate_and_does_not_refuse(self):
        other = self.home.root / "elsewhere"
        other.mkdir()
        self.home.rollout(2, stamp=START + 1, cwd=other)
        mine = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})

    def test_a_later_session_in_the_same_directory_refuses_the_directory(self):
        """A /new in the same process, or another Codex run there: the first rollout would be stale."""
        self.home.rollout(1, stamp=START + 2)
        self.home.rollout(2, stamp=START + 400)
        self.assertEqual(self.home.resolve(self.home.instance()), {})

    def test_a_rollout_before_the_instance_started_is_history_not_a_candidate(self):
        self.home.rollout(9, stamp=START - 600)
        mine = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})

    def test_two_instances_starting_together_get_no_session(self):
        self.home.rollout(1, stamp=START + 2)
        self.home.rollout(2, stamp=START + 3)
        got = self.home.resolve(self.home.instance(1), self.home.instance(2, start=START + 1))
        self.assertEqual(got, {})

    def test_one_rollout_inside_two_instances_windows_is_nobodys(self):
        self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance(1), self.home.instance(2, start=START + 1)), {})

    def test_two_sessions_started_in_one_instances_window_are_unclear(self):
        """A /new within seconds of the start: which of the two is the process's now?"""
        self.home.rollout(1, stamp=START + 2)
        self.home.rollout(2, stamp=START + 5)
        self.assertEqual(self.home.resolve(self.home.instance()), {})

    def test_two_instances_started_apart_each_get_their_own_rollout(self):
        first = self.home.rollout(1, stamp=START + 2)
        second = self.home.rollout(2, stamp=START + 1000 + 2)
        got = self.home.resolve(self.home.instance(1), self.home.instance(2, start=START + 1000))
        self.assertEqual(got, {1: str(first), 2: str(second)})

    def test_subagent_threads_do_not_count_either_way(self):
        self.home.rollout(5, stamp=START + 2, source={"subagent": {"thread_spawn": {}}}, thread_source="subagent")
        self.home.rollout(6, stamp=START + 500, thread_source="subagent")
        mine = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})

    def test_noninteractive_runs_do_not_refuse_a_tui_but_match_an_exec_instance(self):
        self.home.rollout(7, stamp=START + 400, originator="codex_exec", source="exec")
        mine = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})
        # and the reverse: a `codex exec` instance matches only the exec rollout
        run = self.home.instance(3, start=START + 398, exec_run=True)
        self.assertEqual(self.home.resolve(run), {3: str(self.home.codex_home / "sessions" / datetime.fromtimestamp(
            START + 400, timezone.utc).strftime("%Y/%m/%d") / f"rollout-{datetime.fromtimestamp(START + 400, timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}-{uuid(7)}.jsonl")})

    def test_a_resume_id_names_the_rollout_exactly(self):
        old = self.home.rollout(4, stamp=START - 90000)
        self.home.rollout(8, stamp=START - 700)             # another old session in the same directory
        got = self.home.resolve(self.home.instance(resume_id=uuid(4)))
        self.assertEqual(got, {1: str(old)})

    def test_a_resume_id_with_no_file_or_two_files_names_nothing(self):
        self.assertEqual(self.home.resolve(self.home.instance(resume_id=uuid(4))), {})
        self.home.rollout(4, stamp=START - 90000)
        self.home.rollout(4, stamp=START - 80000, name=f"rollout-2020-01-01T00-00-00-{uuid(4)}.jsonl")
        self.assertEqual(self.home.resolve(self.home.instance(resume_id=uuid(4))), {})

    def test_a_resumed_instance_that_also_wrote_a_new_rollout_is_unclear(self):
        self.home.rollout(4, stamp=START - 90000)
        self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance(resume_id=uuid(4))), {})

    def test_a_rollout_held_open_by_another_pane_is_not_claimed(self):
        path = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance(), skip=frozenset({str(path)})), {})

    def test_too_many_candidates_give_no_session(self):
        other = self.home.root / "elsewhere"
        other.mkdir()
        for number in range(10, 16):
            self.home.rollout(number, stamp=START + 2, cwd=other)
        mine = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})
        with mock.patch.object(codex, "MAX_CANDIDATES", 5):
            self.assertEqual(self.home.resolve(self.home.instance()), {})

    def test_an_unreadable_or_headless_rollout_is_ignored(self):
        folder = self.home.codex_home / "sessions" / datetime.fromtimestamp(START, timezone.utc).strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "rollout-garbage.jsonl").write_text("not json\n")
        (folder / "rollout-empty.jsonl").write_text("")
        os.utime(folder / "rollout-garbage.jsonl", (START + 60, START + 60))
        mine = self.home.rollout(1, stamp=START + 2)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})

    def test_a_symlinked_working_directory_is_the_same_directory(self):
        link = self.home.root / "link"
        link.symlink_to(self.home.cwd)
        mine = self.home.rollout(1, stamp=START + 2, cwd=link)
        self.assertEqual(self.home.resolve(self.home.instance()), {1: str(mine)})

    def test_sessions_are_found_across_the_utc_day_boundary(self):
        midnight = (START // 86400 + 1) * 86400
        mine = self.home.rollout(1, stamp=midnight + 1)
        got = codex.resolve_instances([self.home.instance(start=midnight - 5)], now=midnight + 600)
        self.assertEqual(got, {1: str(mine)})

    def test_resume_id_parsing(self):
        for argv, expected in ((["codex", "resume", uuid(3)], uuid(3)), (["node", "/x/codex", "resume", "--yolo", uuid(3).upper()], uuid(3)),
                               (["codex", "resume"], ""), (["codex", "resume", "--last"], ""), (["codex", "resume", "latest"], ""),
                               (["codex", "--yolo"], ""), (["codex", uuid(3)], "")):
            self.assertEqual(codex.resume_id(argv), expected, argv)


class CodexCommandLines(unittest.TestCase):
    def test_exec_resume_and_chdir_are_read_from_the_command_line(self):
        self.assertTrue(pane_center._codex_exec(["node", "/x/codex", "--model", "m", "exec", "do it"]))
        self.assertFalse(pane_center._codex_exec(["node", "/x/codex", "--yolo"]))
        self.assertTrue(pane_center._codex_unclear(["codex", "resume"]))
        self.assertTrue(pane_center._codex_unclear(["codex", "resume", "--last"]))
        self.assertTrue(pane_center._codex_unclear(["codex", "fork"]))
        self.assertFalse(pane_center._codex_unclear(["codex", "resume", uuid(3)]))
        self.assertFalse(pane_center._codex_unclear(["codex", "--yolo"]))
        self.assertEqual(pane_center._codex_directory(["codex", "-C", "/srv/other"], "/srv/here"), "/srv/other")
        self.assertEqual(pane_center._codex_directory(["codex", "--cd", "sub"], "/srv/here"), "/srv/here/sub")
        self.assertEqual(pane_center._codex_directory(["codex", "--cd=/srv/o"], "/srv/here"), "/srv/o")
        self.assertEqual(pane_center._codex_directory(["codex"], "/srv/here"), "/srv/here")


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

    def test_a_rollout_with_no_owner_is_not_live(self):
        self.assertEqual(self.state(("task_started",), pids=()).live_status, "")


def tree_for(*panes):
    """panes: (window id, [(pid, argv)], cwd)"""
    return kitty_rc.parse([{"id": 1, "is_focused": True, "tabs": [{"id": 2, "title": "w", "is_active": True, "windows": [
        {"id": wid, "pid": 70 + wid, "title": f"p{wid}", "cwd": cwd, "env": {"KITTY_PTY_BROKER_SESSION": SESSION},
         "foreground_processes": [{"pid": pid, "cmdline": argv, "cwd": cwd} for pid, argv in procs]}
        for wid, procs, cwd in panes]}]}])


class InspectorWithoutAnOpenRollout(unittest.TestCase):
    def setUp(self):
        self.home = Home(self)
        codex._META_CACHE.clear()
        self.env = {"HOME": str(self.home.root)}

    def snapshot(self, tree):
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                mock.patch.object(pane_center.Inspector, "_claude_by_pid", return_value={}), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home.codex_home)}):
            return pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree)

    def codex_pane(self, wid=9, pid=100, start=START, events=("task_started", "task_complete")):
        self.home.process(pid, ["node", "/usr/local/bin/codex", "--yolo"], start=start)
        self.home.process(pid + 1, ["/opt/vendor/codex", "--yolo"], start=start, parent=pid)
        return (wid, [(pid, ["node", "/usr/local/bin/codex", "--yolo"]), (pid + 1, ["/opt/vendor/codex", "--yolo"])],
                str(self.home.cwd))

    def test_idle_working_and_waiting_come_from_the_rollouts_events(self):
        for events, extra, expected in ((("task_started", "task_complete"), (), "idle"),
                                        (("task_started",), (), "working"),
                                        (("task_started",), [{"type": "event_msg", "payload": {"type": "exec_approval_request"}}], "waiting"),
                                        (("task_started", "turn_aborted"), (), "idle")):
            codex._META_CACHE.clear()
            home = Home(self)
            self.home = home
            home.rollout(1, stamp=START + 2, events=events, extra=extra)
            got = self.snapshot(tree_for(self.codex_pane())).panes[0]
            self.assertEqual((got.activity, got.coding.provider, got.coding.session_id), (expected, "codex", uuid(1)), events)
            self.assertEqual(got.coding.pids, (100, 101))

    def test_no_provable_rollout_stays_agent(self):
        self.home.rollout(1, stamp=START + 2)
        self.home.rollout(2, stamp=START + 900)
        self.assertEqual(self.snapshot(tree_for(self.codex_pane())).panes[0].activity, "agent")
        codex._META_CACHE.clear()
        self.home = Home(self)
        self.assertEqual(self.snapshot(tree_for(self.codex_pane())).panes[0].activity, "agent")   # no rollout at all

    def test_an_open_descriptor_is_still_the_first_choice(self):
        held = self.home.rollout(5, stamp=START - 7000, events=("task_started", "task_complete"))
        other = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        pane = self.codex_pane()
        os.symlink(held, self.home.proc / "101" / "fd" / "4")
        got = self.snapshot(tree_for(pane)).panes[0]
        self.assertEqual((got.activity, got.coding.session_id), ("idle", uuid(5)))
        self.assertIsNotNone(other)

    def test_two_panes_in_one_directory_started_apart_each_read_their_own_rollout(self):
        self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        self.home.rollout(2, stamp=START + 2000 + 2, events=("task_started",))
        first = self.codex_pane(9, 100, START)
        second = self.codex_pane(10, 200, START + 2000)
        got = self.snapshot(tree_for(first, second))
        self.assertEqual([(p.activity, p.coding.session_id) for p in got.panes], [("idle", uuid(1)), ("working", uuid(2))])

    def test_the_wrapper_that_started_first_sets_the_start_time(self):
        """node starts the native binary a few seconds later: the pane's start is the earlier one."""
        self.home.rollout(1, stamp=START + 12, events=("task_started", "task_complete"))
        self.home.process(100, ["node", "/usr/local/bin/codex", "--yolo"], start=START + 10)
        self.home.process(101, ["/opt/vendor/codex", "--yolo"], start=START + 20, parent=100)
        pane = (9, [(100, ["node", "/usr/local/bin/codex", "--yolo"]), (101, ["/opt/vendor/codex", "--yolo"])], str(self.home.cwd))
        self.assertEqual(self.snapshot(tree_for(pane)).panes[0].activity, "idle")

    def test_two_panes_started_together_stay_agent(self):
        self.home.rollout(1, stamp=START + 2)
        self.home.rollout(2, stamp=START + 3)
        got = self.snapshot(tree_for(self.codex_pane(9, 100, START), self.codex_pane(10, 200, START + 1)))
        self.assertEqual([p.activity for p in got.panes], ["agent", "agent"])

    def test_the_codex_home_of_the_process_is_used(self):
        elsewhere = self.home.root / "other-home"
        (elsewhere / "sessions").mkdir(parents=True)
        pane = self.codex_pane()
        self.home.process(100, ["node", "/usr/local/bin/codex", "--yolo"], environ={"CODEX_HOME": str(elsewhere)})
        moment = datetime.fromtimestamp(START + 2, timezone.utc)
        folder = elsewhere / "sessions" / moment.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        (folder / f"rollout-x-{uuid(3)}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
            {"type": "session_meta", "payload": {"id": uuid(3), "timestamp": moment.isoformat(), "cwd": str(self.home.cwd),
                                                   "originator": "codex-tui", "source": "cli"}},
            {"type": "event_msg", "payload": {"type": "task_started"}})))
        os.utime(folder / f"rollout-x-{uuid(3)}.jsonl", (START + 60, START + 60))
        got = self.snapshot(tree_for(pane)).panes[0]
        self.assertEqual((got.activity, got.coding.session_id), ("working", uuid(3)))

    def test_a_resume_command_line_names_the_session(self):
        old = self.home.rollout(4, stamp=START - 40000, events=("task_started", "task_complete"))
        self.home.process(100, ["node", "/usr/local/bin/codex", "resume", uuid(4)], start=START)
        self.home.process(101, ["/opt/vendor/codex", "resume", uuid(4)], start=START, parent=100)
        pane = (9, [(100, ["node", "/usr/local/bin/codex", "resume", uuid(4)]), (101, ["/opt/vendor/codex", "resume", uuid(4)])],
                str(self.home.cwd))
        got = self.snapshot(tree_for(pane)).panes[0]
        self.assertEqual((got.activity, got.coding.session_id, got.coding.path), ("idle", uuid(4), str(old)))


class InspectorCommandLineRules(InspectorWithoutAnOpenRollout):
    def pane_with(self, argv, wid=9, pid=100):
        self.home.process(pid, argv, start=START)
        return (wid, [(pid, argv)], str(self.home.cwd))

    def test_a_resume_picker_or_last_is_never_matched_by_time(self):
        self.home.rollout(1, stamp=START + 2)
        for argv in (["codex", "resume"], ["codex", "resume", "--last"], ["codex", "fork"]):
            got = self.snapshot(tree_for(self.pane_with(argv))).panes[0]
            self.assertEqual(got.activity, "agent", argv)

    def test_a_session_rooted_elsewhere_by_C_is_matched_in_that_directory(self):
        other = self.home.root / "rooted"
        other.mkdir()
        mine = self.home.rollout(1, stamp=START + 2, cwd=other, events=("task_started",))
        sibling = self.home.rollout(2, stamp=START + 3, events=("task_started", "task_complete"))
        got = self.snapshot(tree_for(self.pane_with(["codex", "-C", str(other)]))).panes[0]
        self.assertEqual((got.activity, got.coding.session_id), ("working", uuid(1)))
        self.assertIsNotNone(mine) and self.assertIsNotNone(sibling)

    def test_an_exec_run_matches_only_exec_rollouts(self):
        self.home.rollout(1, stamp=START + 2, originator="codex_exec", source="exec", events=("task_started",))
        got = self.snapshot(tree_for(self.pane_with(["codex", "exec", "go"]))).panes[0]
        self.assertEqual((got.activity, got.coding.session_id), ("working", uuid(1)))
        codex._META_CACHE.clear()
        got = self.snapshot(tree_for(self.pane_with(["codex", "--yolo"]))).panes[0]
        self.assertEqual(got.activity, "agent")


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
