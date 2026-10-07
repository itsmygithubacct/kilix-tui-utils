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
import time
import tracemalloc
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_rollout import claude, codex, grok, jsonl, liveness  # noqa: E402
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

    def ev(self, kind, call_id=None, **more):
        payload = {"type": kind, **more}
        if call_id is not None:
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

    def raw_state(self, records):
        """The state of a rollout whose records (after the metadata) are exactly these dicts."""
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=())
        with open(path, "a") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return codex.session_from_path(str(path), pids=(11,)).live_status

    def test_an_approval_is_matched_only_to_a_later_resolution(self):
        a = self.ev("exec_approval_request", "a")
        begin = self.ev("exec_command_begin", "a")
        start = self.ev("task_started")
        cases = (
            ([start, begin, a], "waiting"),                       # the command came first: it resolves nothing
            ([start, a, begin, a], "waiting"),                    # re-approval after a resolution
            ([start, a, begin, a, begin], "working"),
            ([start, a, begin], "working"),
            ([start, begin, begin, a, a], "waiting"),
            ([start, self.ev("apply_patch_approval_request", "p"), self.ev("patch_apply_end", "p")], "working"),
        )
        for records, expected in cases:
            with self.subTest(records=[r["payload"]["type"] for r in records]):
                self.assertEqual(self.raw_state(records), expected)

    def test_a_structurally_invalid_newest_event_is_unknown_never_an_older_state_or_a_crash(self):
        done = [self.ev("task_started"), self.ev("task_complete")]
        bad = (
            {"type": "event_msg", "payload": None},
            {"type": "event_msg", "payload": "x"},
            {"type": "event_msg", "payload": []},
            {"type": "event_msg", "payload": {"type": ["task_started"]}},
            {"type": "event_msg", "payload": {"type": 7}},
            {"type": "event_msg", "payload": {"type": {"a": 1}}},
            {"type": "event_msg"},
            {"type": ["event_msg"]},
            {"type": 5, "payload": {}},                         # no turn/approval marker in the text at all
            {"type": None},
            {"kind": "x"},
            {"type": None, "payload": {"type": "task_started"}},
            {"payload": {"type": "task_started"}},
            self.ev("exec_approval_request", ["a"]),
            self.ev("exec_command_begin", {"id": 1}),
            self.ev("task_started", turn_id=["t"]),
        )
        for record in bad:
            with self.subTest(record=record):
                self.assertEqual(self.raw_state(done + [record]), "unknown")
                self.assertEqual(self.raw_state(done + [record, self.ev("token_count")]), "unknown")   # not the last line
                self.assertEqual(self.raw_state([self.ev("task_started"), record]), "unknown")

    def test_an_invalid_record_older_than_the_settled_boundary_is_ignored(self):
        bad = {"type": "event_msg", "payload": None}
        self.assertEqual(self.raw_state([bad, self.ev("task_started"), self.ev("task_complete")]), "idle")
        self.assertEqual(self.raw_state([self.ev("task_started"), bad, self.ev("token_count"), self.ev("task_complete")]), "unknown")

    def test_turn_ids_must_be_coherent(self):
        s = self.ev
        cases = (
            ([s("task_started", turn_id="a"), s("task_started", turn_id="b"), s("task_complete", turn_id="a")], "unknown"),
            ([s("task_started", turn_id="a"), s("task_started", turn_id="b"), s("task_complete", turn_id="b")], "unknown"),   # a never ended
            ([s("task_started", turn_id="a"), s("task_complete", turn_id="a")], "idle"),
            ([s("task_started", turn_id="a"), s("task_complete", turn_id="a"), s("task_complete", turn_id="a")], "unknown"),    # a turn ends once
            ([s("task_started"), s("task_complete"), s("turn_aborted")], "unknown"),
            ([s("task_started", turn_id="a"), s("task_complete", turn_id="b")], "unknown"),
            ([s("task_complete", turn_id="a")], "unknown"),                     # an end with no start
            ([s("task_started", turn_id="a"), s("task_complete", turn_id="a"), s("task_started", turn_id="b")], "working"),
            ([s("task_started", turn_id="a"), s("turn_aborted", turn_id="a")], "idle"),
            ([s("task_started"), s("task_complete", turn_id="a")], "idle"),     # ids not both given: order decides
        )
        for records, expected in cases:
            with self.subTest(records=[(r["payload"]["type"], r["payload"].get("turn_id")) for r in records]):
                self.assertEqual(self.raw_state(records), expected)

    def test_anything_newer_than_the_boundary_that_is_not_understood_is_unknown(self):
        done = [self.ev("task_started"), self.ev("task_complete")]
        odd = (
            [1, 2],                                                         # valid JSON, not an object
            "text",
            {"type": "response_item", "payload": None},
            {"type": "response_item", "payload": {"type": "mystery"}},
            {"type": "response_item", "payload": {}},
            {"type": "turn_context", "payload": []},
            {"type": "brand_new_record_kind", "payload": {}},
            self.ev("new_unknown_state"),
            self.ev(""),
        )
        for record in odd:
            with self.subTest(record=record):
                self.assertEqual(self.raw_state(done + [record, self.ev("token_count")]), "unknown")
                self.assertEqual(self.raw_state(done + [record]), "unknown")
        known = [self.ev("token_count"), self.ev("item_completed"), self.ev("thread_settings_applied"),
                 {"type": "response_item", "payload": {"type": "reasoning"}},
                 {"type": "world_state", "payload": {}}, {"type": "token_usage_record", "payload": {}}]
        self.assertEqual(self.raw_state(done + known), "idle")               # what real 0.160 rollouts hold

    def test_json_nested_too_deep_is_unknown_and_never_raises(self):
        deep = '{"type":"response_item","payload":' + "[" * 10000 + "0" + "]" * 10000 + "}"
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        with open(path, "a") as handle:
            handle.write(deep + "\n")
        self.assertEqual(codex.session_from_path(str(path), pids=(1,)).live_status, "unknown")
        self.assertIsNone(jsonl.load(deep.encode()))

    def test_overlapping_open_turns_are_unknown(self):
        s = self.ev
        for records in ([s("task_started", turn_id="a"), s("task_started", turn_id="b")],
                        [s("task_started"), s("task_started")],
                        [s("task_complete", turn_id="x"), s("task_started", turn_id="a"), s("task_started", turn_id="b"), s("exec_approval_request", "q", turn_id="b")]):
            self.assertEqual(self.raw_state(records), "unknown", records)
        self.assertEqual(self.raw_state([s("task_started", turn_id="a"), s("task_complete", turn_id="a"), s("task_started", turn_id="b")]), "working")

    def test_approvals_and_resolutions_must_share_ids_and_turns(self):
        s = self.ev
        start = s("task_started", turn_id="a")
        cases = (
            ([start, s("exec_approval_request", "x", turn_id="a"), s("exec_command_begin", "x", turn_id="b")], "unknown"),
            ([start, s("exec_approval_request", "x", turn_id="a"), s("exec_command_begin", "x", turn_id="a")], "working"),
            ([start, s("exec_approval_request", "x", turn_id="a"), s("exec_command_begin", "y", turn_id="b")], "unknown"),   # another turn's event in the open turn
            ([start, s("exec_approval_request", approval_id="x"), s("exec_command_begin", approval_id="x")], "working"),
            ([start, s("exec_approval_request", approval_id="x"), s("exec_command_begin", "x")], "working"),
            ([start, s("exec_approval_request", "x", approval_id="y"), s("exec_command_begin", approval_id="y")], "working"),
            ([start, s("exec_approval_request", approval_id="x"), s("exec_command_begin", approval_id="z")], "waiting"),
            ([start, s("exec_approval_request", "x", turn_id="b")], "unknown"),                 # an approval of another turn
            ([s("task_started"), s("exec_approval_request", "x", turn_id="a"), s("exec_command_begin", "x", turn_id="b")], "unknown"),
            ([s("task_started"), s("exec_approval_request", "x", turn_id="a"), s("exec_command_begin", "x", turn_id="a")], "working"),
        )
        for records, expected in cases:
            with self.subTest(records=[(r["payload"]["type"], r["payload"].get("turn_id")) for r in records]):
                self.assertEqual(self.raw_state(records), expected)

    def test_the_recovery_tools_cut_off_flag_does_not_depend_on_the_strict_checks(self):
        """Records this reader does not understand make the live state unknown, but a dead session's turn
        that never ended is still offered for recovery as cut-off."""
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=("task_started",))
        with open(path, "a") as handle:
            for index in range(5):
                handle.write(json.dumps({"type": "response_item", "payload": {"index": index}}) + "\n")
        self.assertEqual(codex.session_from_path(str(path), pids=(1,)).live_status, "unknown")
        self.assertEqual(codex.session_from_path(str(path), pids=()).state, "cut-off")
        done = home.rollout(2, stamp=START + 3, events=("task_started", "task_complete"))
        self.assertEqual(codex.session_from_path(str(done), pids=()).state, "idle")

    def test_invalid_utf8_in_a_record_that_contributes_to_the_state_is_unknown_but_display_stays_tolerant(self):
        home = Home(self)
        path = home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        with open(path, "ab") as handle:
            handle.write(b'{"type":"event_msg","payload":{"type":"token_count","data":"\xff"}}\n')
        self.assertEqual(codex.session_from_path(str(path), pids=(1,)).live_status, "unknown")
        self.assertEqual(jsonl.load(b'{"a": "\xff"}'), {"a": "\ufffd"})
        self.assertIsNone(jsonl.load(b'{"a": "\xff"}', strict=True))
        # an invalid line older than the settled turn does not matter
        older = home.rollout(2, stamp=START + 3, events=())
        with open(older, "ab") as handle:
            handle.write(b'{"type":"response_item","payload":{"type":"message","role":"user","content":"\xff"}}\n')
            for record in (self.ev("task_started"), self.ev("task_complete")):
                handle.write((json.dumps(record) + "\n").encode())
        self.assertEqual(codex.session_from_path(str(older), pids=(1,)).live_status, "idle")

    def test_a_rollout_with_no_owner_is_not_live(self):
        self.assertEqual(self.state(("task_started",), pids=()).live_status, "")


class Counting:
    """A file wrapper that counts the bytes a reader actually asks the file for."""

    total = 0

    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()

    def seek(self, *args):
        return self.handle.seek(*args)

    def tell(self):
        return self.handle.tell()

    def read(self, size=-1):
        data = self.handle.read(size)
        Counting.total += len(data)
        return data

    def readline(self, size=-1):
        data = self.handle.readline(size)
        Counting.total += len(data)
        return data


class BoundedReads(unittest.TestCase):
    """A state is read from a bounded tail: bytes, line size and time are all capped."""

    def setUp(self):
        self.home = Home(self)
        Counting.total = 0

    def read_state(self, path):
        real = open
        with mock.patch.object(jsonl, "open", lambda p, m="r": Counting(real(p, m)), create=True):
            return codex.session_from_path(str(path), pids=(1,))

    def test_lines_are_yielded_newest_first_with_a_gap_for_a_line_too_long(self):
        path = self.home.root / "t.jsonl"
        path.write_bytes(b"l1\n" + b"x" * 200 + b"\nl3\n\nl4")
        self.assertEqual(list(jsonl.bounded_reverse_lines(str(path), max_line=100, chunk=7)), [b"l4", b"l3", None, b"l1"])
        self.assertEqual(list(jsonl.bounded_reverse_lines(str(path), max_line=1000, chunk=7))[2], b"x" * 200)

    def test_the_byte_budget_ends_with_a_gap_and_reads_no_more(self):
        path = self.home.root / "t.jsonl"
        path.write_bytes(b"".join(b"line %06d\n" % n for n in range(5000)))
        got = list(jsonl.bounded_reverse_lines(str(path), max_bytes=1000, chunk=100))
        self.assertIsNone(got[-1])
        self.assertEqual(got[0], b"line 004999")
        self.assertLessEqual(sum(len(line) + 1 for line in got if line), 1000)

    def test_the_time_budget_ends_with_a_gap(self):
        path = self.home.root / "t.jsonl"
        path.write_bytes(b"a\nb\n")
        self.assertEqual(list(jsonl.bounded_reverse_lines(str(path), max_seconds=0)), [None])

    def huge_line_rollout(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
        with open(path, "ab") as handle:
            handle.truncate(handle.tell() + 40 * 1024 * 1024)            # a 40 MiB line (sparse)
        return path

    def test_one_huge_line_after_an_open_turn_is_unknown_and_reads_only_the_budget(self):
        path = self.huge_line_rollout()
        started = time.monotonic()
        session = self.read_state(path)
        self.assertLess(time.monotonic() - started, 1.0)           # the byte budget ends it at once, not the 2 s clock
        self.assertEqual(session.live_status, "unknown")
        self.assertLessEqual(Counting.total, codex.MAX_TAIL_BYTES + (1 << 16))
        self.assertGreater(Counting.total, codex.MAX_TAIL_BYTES - (1 << 20))      # the budget, not the file, ended the read

    def test_one_huge_line_is_never_held_in_memory(self):
        path = self.huge_line_rollout()
        tracemalloc.start()
        try:
            codex.session_from_path(str(path), pids=(1,))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 12 * 1024 * 1024)

    def test_a_gap_before_the_newest_boundary_makes_the_state_unknown(self):
        """A record too long to read could be a newer turn start: the older idle is not believed."""
        path = self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        with open(path, "a") as handle:
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "y" * 5000}}) + "\n")
        with mock.patch.object(codex, "MAX_LINE_BYTES", 1000):
            self.assertEqual(codex.session_from_path(str(path), pids=(1,)).live_status, "unknown")
        self.assertEqual(codex.session_from_path(str(path), pids=(1,)).live_status, "idle")

    def test_many_records_before_a_settled_boundary_are_still_bounded(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        filler = (json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 30000}}) + "\n").encode()
        original = path.read_bytes()
        marker = b'{"type": "event_msg", "payload": {"type": "task_started"}}'
        head, _, rest = original.partition(marker)
        path.write_bytes(head + filler * 1300 + marker + rest)      # about 39 MB between the prompt and the turn
        started = time.monotonic()
        session = self.read_state(path)
        self.assertEqual(session.live_status, "unknown")           # the turn's start has no previous boundary in the budget
        self.assertGreater(Counting.total, 1024 * 1024)               # it did read on, up to the budget
        self.assertLessEqual(Counting.total, codex.MAX_TAIL_BYTES + (1 << 20))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_huge_first_record_is_not_assembled(self):
        path = self.home.root / "rollout-x.jsonl"
        with open(path, "wb") as handle:
            handle.truncate(40 * 1024 * 1024)
        self.assertEqual(codex._first_meta(str(path)), {})
        real = open
        with mock.patch.object(jsonl, "open", lambda p, m="r": Counting(real(p, m)), create=True):
            self.assertEqual(list(jsonl.head_records(str(path), 256)), [])
        self.assertLessEqual(Counting.total, jsonl.LINE_BYTES + 1)

    def test_the_head_scan_is_bounded_in_total(self):
        path = self.home.root / "rollout-x.jsonl"
        line = b'{"type": "x", "payload": {"pad": "' + b"p" * 900000 + b'"}}\n'
        path.write_bytes(line * 64)                                  # 64 records of 0.9 MB, no session_meta
        real = open
        with mock.patch.object(jsonl, "open", lambda p, m="r": Counting(real(p, m)), create=True):
            self.assertEqual(codex._first_meta(str(path)), {})
        self.assertLessEqual(Counting.total, jsonl.HEAD_BYTES + jsonl.LINE_BYTES)
        self.assertGreater(Counting.total, 1024 * 1024)

    def test_the_head_cap_is_exact(self):
        path = self.home.root / "rollout-x.jsonl"
        path.write_bytes(b'{"type":"turn_context","payload":{}}\n' * 100)
        real = open
        with mock.patch.object(jsonl, "open", lambda p, m="r": Counting(real(p, m)), create=True):
            list(jsonl.head_records(str(path), 256, max_bytes=40))
        self.assertLessEqual(Counting.total, 40)
        Counting.total = 0
        with mock.patch.object(jsonl, "open", lambda p, m="r": Counting(real(p, m)), create=True):
            records = list(jsonl.head_records(str(path), 256, max_bytes=10_000))
        self.assertEqual(len(records), 100)

    def test_the_old_unbounded_reader_is_not_used_for_a_state(self):
        with mock.patch.object(jsonl, "reverse_lines", side_effect=AssertionError("unbounded")):
            path = self.home.rollout(1, stamp=START + 2, events=("task_started",))
            self.assertEqual(codex.session_from_path(str(path), pids=(1,)).live_status, "working")


def tree_for(*panes):
    """panes: (window id, [(pid, argv)], cwd)"""
    return kitty_rc.parse([{"id": 1, "is_focused": True, "tabs": [{"id": 2, "title": "w", "is_active": True, "windows": [
        {"id": wid, "pid": 70 + wid, "title": f"p{wid}", "cwd": cwd, "env": {"KITTY_PTY_BROKER_SESSION": SESSION},
         "foreground_processes": [{"pid": pid, "cmdline": argv, "cwd": cwd} for pid, argv in procs]}
        for wid, procs, cwd in panes]}]}])


class CodexIsAlwaysAgent(unittest.TestCase):
    """Nothing Codex writes names the session a process runs NOW, so no pane gets a Codex state."""

    def setUp(self):
        self.home = Home(self)

    def snapshot(self, tree):
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home.codex_home)}):
            return pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree)

    def codex_pane(self, wid=9, pid=100, argv=("/opt/vendor/codex", "--yolo")):
        self.home.process(pid, list(argv))
        return (wid, [(pid, list(argv))], str(self.home.cwd))

    def hold(self, pid, path, fd=4):
        os.symlink(path, self.home.proc / str(pid) / "fd" / str(fd))

    def test_a_held_rollout_names_no_state_whatever_its_events(self):
        for events in (("task_started", "task_complete"), ("task_started",),
                       ("task_started", "turn_aborted"), ("task_started", "exec_approval_request")):
            home = Home(self)
            self.home = home
            path = home.rollout(1, stamp=START + 2, events=events)
            pane = self.codex_pane()
            self.hold(100, path)
            got = self.snapshot(tree_for(pane)).panes[0]
            self.assertEqual((got.activity, got.coding.provider, got.coding.live_status), ("agent", "codex", "unknown"), events)

    def test_no_pane_layout_gives_codex_a_state(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        other = self.home.rollout(2, stamp=START + 3, events=("task_started",))
        self.home.process(200, ["codex"])
        self.hold(200, path)
        cases = {
            "nothing held": self.codex_pane(),
            "resume argv": self.codex_pane(argv=("codex", "resume", uuid(1))),
            "two panes sharing": self.codex_pane(10, 300),
        }
        self.hold(100, path)
        self.hold(300, path)
        for name, pane in cases.items():
            self.assertEqual(self.snapshot(tree_for(pane)).panes[0].activity, "agent", name)
        self.hold(100, other, 5)

    def test_a_viewer_or_helper_or_replaced_file_never_gives_idle(self):
        path = self.home.rollout(1, stamp=START + 2, events=("task_started", "task_complete"))
        self.home.process(100, ["cat", str(path)])
        self.hold(100, path)
        for argv in (["cat", str(path)], ["less", "codex"], ["codex", "debug", "x"]):
            self.home.process(100, argv)
            got = self.snapshot(tree_for((9, [(100, argv)], str(self.home.cwd)))).panes[0]
            self.assertNotEqual(got.activity, "idle", argv)
        deleted = self.home.root / "gone.jsonl"
        os.symlink(f"{path} (deleted)", self.home.proc / "100" / "fd" / "9")
        self.assertEqual(self.snapshot(tree_for(self.codex_pane())).panes[0].activity, "agent")

    def test_a_pane_with_claude_and_a_codex_process_is_agent(self):
        pane = (9, [(100, ["claude"]), (101, ["/opt/vendor/codex"])], str(self.home.cwd))
        self.home.process(100, ["claude"])
        self.home.process(101, ["/opt/vendor/codex"])
        self.assertEqual(self.snapshot(tree_for(pane)).panes[0].activity, "agent")

    def test_the_activity_table_never_maps_a_codex_session(self):
        for status in ("idle", "working", "waiting", "ready", "busy"):
            session = Session(provider="codex", session_id="s", path="", cwd="/srv/x", title="", updated=0,
                              state="live", pids=(1,), live_status=status)
            pane = kitty_rc.parse([{"id": 1, "tabs": [{"id": 2, "windows": [{"id": 9, "pid": 70, "cwd": "/srv/x"}]}]}]).panes[0]
            self.assertEqual(pane_center._activity(session, pane), "agent", status)

    def test_the_held_descriptor_and_resolver_code_is_gone(self):
        for name in ("Instance", "resolve_instances", "resume_id", "_META_CACHE", "_candidates"):
            self.assertFalse(hasattr(codex, name), name)
        for name in ("_codex_by_start", "_match_codex", "_codex_unclear", "_codex_for", "_held", "_held_rollouts", "_codex_cache"):
            self.assertFalse(hasattr(pane_center.Inspector, name) or hasattr(pane_center.Inspector(), name), name)
        self.assertFalse(hasattr(pane_center, "_open_files"))


class ClaudeRecords(unittest.TestCase):
    """A Claude pane has a state only through a record of the process's OWN registry that names it now."""

    def setUp(self):
        self.home = Home(self)
        self.config = self.home.root / "claude"
        (self.config / "sessions").mkdir(parents=True)

    def claude(self, pid=91, argv=("claude",), environ="own", **kwargs):
        if environ == "own":
            environ = {"CLAUDE_CONFIG_DIR": str(self.config)}
        self.home.process(pid, list(argv), environ=environ, **kwargs)
        return pid

    def record(self, pid, *, config=None, session=None, start="live", status="idle", name=None):
        data = {"pid": pid, "sessionId": session or uuid(pid), "status": status, "cwd": str(self.home.cwd)}
        if start == "live":
            start = liveness.start_ticks(pid, proc_root=str(self.home.proc))
        if start is not None:
            data["procStart"] = start
        folder = (config or self.config) / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name or f"{pid}.json")).write_text(json.dumps(data))

    def activity(self, *pids, argv=("claude",), reader_config=None):
        pane = (9, [(pid, list(argv)) for pid in pids], str(self.home.cwd))
        env = {"HOME": str(self.home.root / "reader-home")}
        if reader_config:
            env["CLAUDE_CONFIG_DIR"] = str(reader_config)
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")), \
                mock.patch.dict(os.environ, env):
            return pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree_for(pane)).panes[0].activity

    def test_a_record_with_the_live_start_time_names_the_state(self):
        pid = self.claude()
        for status, expected in (("idle", "idle"), ("shell", "idle"), ("busy", "working"), ("waiting", "waiting")):
            self.record(pid, status=status)
            self.assertEqual(self.activity(pid), expected, status)

    def test_a_record_without_procstart_or_for_another_start_is_not_identity(self):
        pid = self.claude()
        self.record(pid, start=None, status="busy")
        self.assertEqual(self.activity(pid), "agent")
        self.record(pid, start=str(int(liveness.start_ticks(pid, proc_root=str(self.home.proc))) + 7), status="busy")
        self.assertEqual(self.activity(pid), "agent")

    def test_two_records_for_one_pid_are_ambiguous(self):
        pid = self.claude()
        self.record(pid, session=uuid(1), name="a.json")
        self.record(pid, session=uuid(2), status="busy", name="b.json")
        self.assertEqual(self.activity(pid), "agent")

    def test_a_malformed_status_is_agent(self):
        pid = self.claude()
        self.record(pid, status="sleeping")
        self.assertEqual(self.activity(pid), "agent")

    def test_two_claude_processes_in_one_pane_are_agent_in_either_order(self):
        one, two = self.claude(91), self.claude(92)
        self.record(one, status="idle")
        self.record(two, status="busy")
        self.assertEqual(self.activity(one, two), "agent")
        self.assertEqual(self.activity(two, one), "agent")
        self.assertEqual(self.activity(one), "idle")

    def test_a_pid_that_now_runs_something_else_is_agent(self):
        pid = self.claude()
        self.record(pid)
        self.home.process(pid, ["sh"], environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.activity(pid), "agent")           # the pane still lists `claude`
        self.home.process(pid, ["less", "claude"], environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.activity(pid), "agent")           # argv[1] names claude, but it is not the pane's command
        self.home.process(pid, ["claude"], environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.activity(pid), "idle")

    def test_a_pane_that_also_runs_codex_is_agent_even_with_a_valid_claude_record(self):
        pid = self.claude()
        self.record(pid, status="idle")
        self.home.process(101, ["/opt/vendor/codex"])
        self.assertEqual(self.activity(pid), "idle")
        self.assertEqual(self.activity(pid, 101, argv=("claude",)), "agent")
        pane = (9, [(pid, ["claude"]), (101, ["/opt/vendor/codex"])], str(self.home.cwd))
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")):
            got = pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree_for(pane)).panes[0]
        self.assertEqual(got.activity, "agent")

    def test_a_relative_config_directory_is_never_followed(self):
        relative = self.home.root / "rel"
        pid = self.claude(environ={"CLAUDE_CONFIG_DIR": "rel"})
        self.record(pid, config=relative, status="busy")
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.home.root)                                    # where "rel" would resolve
        self.assertEqual(self.activity(pid), "agent")

    def test_only_the_native_form_is_named_and_every_mention_counts_as_a_possible_agent(self):
        native = (["claude"], ["/home/u/.local/bin/claude", "--resume", "x"], ["claude", "-p", "go"])
        not_native = (["node", "/x/claude"], ["bun", "/x/claude"], ["nodejs", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
                      ["less", "claude"], ["env", "claude"], ["Claude"], ["claude.exe"], ["python3", "claude"],
                      ["sh", "-c", "claude"], ["claude-helper"], [])
        for argv in native:
            self.assertTrue(pane_center._claude_native(argv), argv)
        for argv in not_native:
            self.assertFalse(pane_center._claude_native(argv), argv)
        mentions = {
            ("claude",): ["claude"],
            ("node", "/opt/node_modules/@anthropic-ai/claude-code/cli.mjs"): ["claude"],
            ("less", "claude"): ["claude"],
            ("env", "codex", "x"): ["codex"],
            ("/usr/bin/python3", "/home/u/grok/run.py"): ["grok"],
            ("omp", "-p", "review"): ["omp"],
            ("kimi-code",): ["kimi"],
            ("claude", "/srv/codex/notes"): ["claude", "codex"],
            ("tmux", "new-session", "-s", "x", "claude", "--resume"): [],          # a host runs a command: the tmux hint's case
            ("ssh", "host", "claude"): [],
            ("kitten", "run-shell", "--shell=/bin/bash", "--env=KITTY_HOLD=1", "claude", "--model", "sonnet"): [],
            ("vim", "notes.txt"): [],
            ("bash",): [],
            ("claudette",): [],
            (): [],
        }
        for argv, expected in mentions.items():
            self.assertEqual(pane_center._mentioned(argv), expected, argv)

    def test_a_node_or_npm_claude_is_never_named_and_makes_a_pane_ambiguous(self):
        npm = ["node", "/opt/node_modules/@anthropic-ai/claude-code/cli.js"]
        pid = self.claude(argv=npm)
        self.record(pid)
        self.assertEqual(self.activity(pid, argv=npm), "agent")                 # a valid row, but not the native form
        native = self.claude(92)
        self.record(native, session=uuid(2), status="busy")
        self.home.process(pid, npm, environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        for order in ((native, pid), (pid, native)):
            pane = (9, [(n, ["claude"] if n == native else npm) for n in order], str(self.home.cwd))
            with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")):
                got = pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree_for(pane)).panes[0]
            self.assertEqual(got.activity, "agent", order)
        self.assertEqual(self.activity(native), "working")                       # alone, the native one is named

    def test_a_viewer_that_mentions_claude_is_agent_even_with_a_matching_record(self):
        pid = self.claude(argv=("less", "claude"))
        self.record(pid, status="idle")
        self.assertEqual(self.activity(pid, argv=("less", "claude")), "agent")

    def test_the_whole_command_line_must_equal_the_panes(self):
        pid = self.claude(argv=("claude", "--resume", uuid(1)))
        self.record(pid, status="idle")
        self.assertEqual(self.activity(pid, argv=("claude", "--resume", uuid(1))), "idle")
        self.assertEqual(self.activity(pid, argv=("claude", "--resume", uuid(2))), "agent")
        self.assertEqual(self.activity(pid, argv=("claude",)), "agent")

    def inspect_with(self, pids, hook_target, hook):
        """Run a snapshot of one pane per pid while `hook` runs the first time `hook_target` is called."""
        panes = [(9 + n, [(pid, ["claude"])], str(self.home.cwd)) for n, pid in enumerate(pids)]
        tree = kitty_rc.parse([{"id": 1, "is_focused": True, "tabs": [{"id": 2, "title": "w", "is_active": True, "windows": [
            {"id": wid, "pid": 70 + wid, "title": f"p{wid}", "cwd": cwd, "env": {"KITTY_PTY_BROKER_SESSION": SESSION},
             "foreground_processes": [{"pid": pid, "cmdline": argv, "cwd": cwd} for pid, argv in procs]}
            for wid, procs, cwd in panes]}]}])
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")):
            return [pane.activity for pane in pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree).panes]

    def test_a_process_that_changes_while_its_row_is_being_read_is_agent(self):
        env = {"CLAUDE_CONFIG_DIR": str(self.config)}
        changes = {
            "exec": lambda pid: self.home.process(pid, ["sh"], environ=env),
            "config": lambda pid: self.home.process(pid, ["claude"], environ={"CLAUDE_CONFIG_DIR": str(self.home.root / "other")}),
            "recycled": lambda pid: self.home.process(pid, ["claude"], start=START + 10, environ=env),
        }
        for name, change in changes.items():
            pid = self.claude()
            self.record(pid)
            original = liveness.registry_records

            def changing(*args, _change=change, _pid=pid, **kwargs):
                _change(_pid)
                return original(*args, **kwargs)
            with mock.patch.object(liveness, "registry_records", side_effect=changing):
                self.assertEqual(self.inspect_with([pid], None, None), ["agent"], name)

    def test_a_process_recycled_between_the_scan_and_its_start_check_is_agent(self):
        pid = self.claude()
        self.record(pid)
        original = liveness.start_ticks

        def late(number, **kwargs):
            result = original(number, **kwargs)
            self.home.process(number, ["claude"], start=START + 10, environ={"CLAUDE_CONFIG_DIR": str(self.config)})
            return result
        with mock.patch.object(liveness, "start_ticks", side_effect=late):
            self.assertEqual(self.inspect_with([pid], None, None), ["agent"])

    def test_a_cached_row_is_checked_again_when_a_later_pane_uses_it(self):
        one, two = self.claude(91), self.claude(92)
        self.record(one)
        self.record(two, session=uuid(2))
        original = pane_center._live_argv

        def recycle_second(number, **kwargs):
            if number == two:
                self.home.process(two, ["claude"], start=START + 10, environ={"CLAUDE_CONFIG_DIR": str(self.config)})
            return original(number, **kwargs)
        with mock.patch.object(pane_center, "_live_argv", side_effect=recycle_second):
            self.assertEqual(self.inspect_with([one, two], None, None), ["idle", "agent"])
        self.home.process(two, ["claude"], environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.inspect_with([one, two], None, None), ["idle", "idle"])

    def test_a_row_scanned_for_an_earlier_pane_is_compared_with_the_start_time_when_a_later_pane_uses_it(self):
        one, two = self.claude(91), self.claude(92)
        self.record(one)
        self.record(two, session=uuid(2))
        original = pane_center._live_argv
        calls = {"n": 0}

        def recycle_second_after_the_scan(number, **kwargs):
            if number == one:
                calls["n"] += 1
                if calls["n"] == 2:                       # the registry has been scanned for pane one
                    self.home.process(two, ["claude"], start=START + 10, environ={"CLAUDE_CONFIG_DIR": str(self.config)})
            return original(number, **kwargs)
        with mock.patch.object(pane_center, "_live_argv", side_effect=recycle_second_after_the_scan):
            self.assertEqual(self.inspect_with([one, two], None, None), ["idle", "agent"])

    def test_registry_rows_keep_pid_and_start_and_a_missing_start_is_refused_when_required(self):
        pid = self.claude()
        self.record(pid)
        self.record(self.claude(92), start=None, name="nostart.json")
        rows = liveness.registry_records(str(self.config / "sessions"), proc_root=str(self.home.proc), require_start=True)
        self.assertEqual([(r["pid"], r["procStart"]) for rows_ in rows.values() for r in rows_],
                         [(pid, liveness.start_ticks(pid, proc_root=str(self.home.proc)))])
        loose = liveness.registry_records(str(self.config / "sessions"), proc_root=str(self.home.proc))
        self.assertEqual(sum(len(v) for v in loose.values()), 2)

    def test_a_cut_off_read_is_never_compared_as_if_it_were_whole(self):
        # a command line longer than the bound: not equal to the pane's (which is shorter), but also never a prefix
        argv = ["claude", "x" * 200]
        pid = self.claude(argv=argv)
        self.record(pid)
        self.assertEqual(self.activity(pid, argv=argv), "idle")
        with mock.patch.object(pane_center, "MAX_CMDLINE_BYTES", 100):
            self.assertIsNone(pane_center._live_argv(pid, proc_root=str(self.home.proc)))
            self.assertEqual(self.activity(pid, argv=argv), "agent")
        long_live = argv + ["--resume", "different"]
        self.home.process(pid, long_live, environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.activity(pid, argv=argv), "agent")
        # the review's shape: the pane's argument fills 64 KiB exactly and the live command has more after it
        big = ["claude", "y" * 65528]
        self.home.process(pid, big + ["--resume", "different"], environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.activity(pid, argv=big), "agent")

    def test_an_environment_beyond_the_bound_is_refused_not_read_as_far_as_it_goes(self):
        pid = self.claude(environ={"HOME": str(self.home.root / "h")})
        self.record(pid, config=self.home.root / "h" / ".claude", status="busy")
        self.assertEqual(self.activity(pid), "working")
        env = {"HOME": str(self.home.root / "h"), **{f"FILL{n}": "x" * 60000 for n in range(20)},
               "CLAUDE_CONFIG_DIR": str(self.home.root / "elsewhere")}
        self.home.process(pid, ["claude"], environ=env)                  # the real override sits after 1.2 MB
        self.assertEqual(self.activity(pid), "agent")                    # read whole: that directory has no row
        with mock.patch.object(pane_center, "MAX_ENVIRON_BYTES", 1 << 20):
            self.assertIsNone(pane_center._live_environment(pid, proc_root=str(self.home.proc)))
            self.assertEqual(self.activity(pid), "agent")

    def test_proc_stat_and_registry_files_longer_than_their_bounds_are_refused(self):
        pid = self.claude()
        self.record(pid)
        proc_stat = self.home.proc / str(pid) / "stat"
        with mock.patch.object(liveness, "MAX_PROC_BYTES", 20):
            self.assertIsNone(liveness.start_ticks(pid, proc_root=str(self.home.proc)))
        self.assertIsNotNone(liveness.start_ticks(pid, proc_root=str(self.home.proc)))
        with mock.patch.object(liveness, "MAX_REGISTRY_BYTES", 20):
            self.assertEqual(liveness.registry_records(str(self.config / "sessions"), proc_root=str(self.home.proc)), {})
        self.assertIsNone(liveness.read_bounded(str(proc_stat), 5))
        self.assertIsNotNone(liveness.read_bounded(str(proc_stat), 4096))

    def test_the_launcher_wrapper_kilix_starts_every_coding_pane_with_does_not_hide_the_native_claude(self):
        """Live panes list `kitten run-shell … claude …`, the native claude, and its MCP helper together."""
        wrapper = ["kitten", "run-shell", "--shell=/bin/bash", "--env=KITTY_HOLD=1", "claude", "--model", "sonnet"]
        helper = ["python3", "-B", "needle_cli.py", "mcp"]
        native = ["claude", "--model", "sonnet"]
        self.home.process(90, wrapper)
        self.home.process(91, native, environ={"CLAUDE_CONFIG_DIR": str(self.config)}, parent=90)
        self.home.process(92, helper, parent=91)
        self.record(91, status="busy")
        pane = (9, [(90, wrapper), (91, native), (92, helper)], str(self.home.cwd))
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")):
            got = pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree_for(pane)).panes[0]
        self.assertEqual((got.activity, got.coding.session_id), ("working", uuid(91)))
        # a second real agent next to it still makes the pane ambiguous
        other = ["/opt/vendor/codex"]
        self.home.process(93, other)
        pane = (9, [(90, wrapper), (91, native), (93, other)], str(self.home.cwd))
        with mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")):
            got = pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree_for(pane)).panes[0]
        self.assertEqual(got.activity, "agent")

    def test_a_process_that_mentions_two_agents_is_never_named(self):
        argv = ["claude", "/srv/codex/notes"]
        pid = self.claude(argv=argv)
        self.record(pid)
        self.assertEqual(self.activity(pid, argv=argv), "agent")

    def test_every_step_of_the_bracket_is_needed_not_only_the_final_check(self):
        """A change that is undone before the last check of the snapshot is still caught where it happened."""
        env = {"CLAUDE_CONFIG_DIR": str(self.config)}
        pid = self.claude()
        self.record(pid)
        # 1. the process is something else only while the row is read and the process observed again
        original = liveness.registry_records
        calls = {"n": 0}

        def process_swapped_in_between(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                result = original(*args, **kwargs)
                self.home.process(pid, ["sh"], environ=env)         # after the first row read ...
                return result
            if calls["n"] == 2:
                self.home.process(pid, ["claude"], environ=env)     # ... undone before the second row read
            return original(*args, **kwargs)
        with mock.patch.object(liveness, "registry_records", side_effect=process_swapped_in_between):
            self.assertEqual(self.inspect_with([pid], None, None), ["agent"])
        self.home.process(pid, ["claude"], environ=env)
        # 2. the row is another session only while the process is observed the second time
        original_argv = pane_center._live_argv
        argv_calls = {"n": 0}

        def row_swapped_in_between(number, **kwargs):
            argv_calls["n"] += 1
            if argv_calls["n"] == 2:
                self.record(pid, session=uuid(7), status="busy")
            elif argv_calls["n"] == 3:
                self.record(pid)
            return original_argv(number, **kwargs)
        with mock.patch.object(pane_center, "_live_argv", side_effect=row_swapped_in_between):
            self.assertEqual(self.inspect_with([pid], None, None), ["agent"])
        self.record(pid)
        self.assertEqual(self.inspect_with([pid], None, None), ["idle"])

    def test_a_row_whose_start_time_is_not_the_processes_is_refused_by_the_consumer_too(self):
        pid = self.claude()
        self.record(pid)
        row = {"pid": pid, "procStart": "1", "cwd": "", "status": "idle", "name": "", "version": "", "entrypoint": ""}
        with mock.patch.object(liveness, "registry_records", return_value={uuid(pid): (row,)}):
            self.assertEqual(self.inspect_with([pid], None, None), ["agent"])

    def test_an_observation_whose_start_time_changed_inside_it_is_none(self):
        pid = self.claude()
        original = liveness.start_ticks
        seen = {"n": 0}

        def second_read_differs(number, **kwargs):
            seen["n"] += 1
            return original(number, **kwargs) if seen["n"] == 1 else "999"
        inspector = pane_center.Inspector(proc_root=str(self.home.proc))
        self.assertIsNotNone(inspector._observe(pid))
        with mock.patch.object(liveness, "start_ticks", side_effect=second_read_differs):
            self.assertIsNone(inspector._observe(pid))

    def test_registry_rows_are_never_taken_from_another_pane_or_an_earlier_read(self):
        one, two = self.claude(91), self.claude(92)
        self.record(one)
        self.record(two, session=uuid(2))
        original = pane_center._live_argv

        def replace_second_row(number, **kwargs):
            if number == two:
                self.record(two, session=uuid(3), status="busy")        # the file changes before pane two is looked at
            return original(number, **kwargs)
        pane = lambda pid, wid: (wid, [(pid, ["claude"])], str(self.home.cwd))          # noqa: E731
        with mock.patch.object(pane_center, "_live_argv", side_effect=replace_second_row), \
                mock.patch.object(pane_center, "_broker_statuses", return_value=({}, False, "")):
            got = pane_center.Inspector(proc_root=str(self.home.proc)).snapshot(tree_for(pane(one, 9), pane(two, 10))).panes
        self.assertEqual([item.activity for item in got], ["idle", "working"])
        self.assertEqual(got[1].coding.session_id, uuid(3))                              # the row as it is now, not as it was

    def test_a_process_that_changes_while_a_later_pane_is_inspected_is_dropped_at_the_end(self):
        one, two = self.claude(91), self.claude(92)
        self.record(one)
        self.record(two, session=uuid(2))
        original = pane_center._live_argv

        def break_first(number, **kwargs):
            if number == two:
                self.home.process(one, ["less", "claude"], environ={"CLAUDE_CONFIG_DIR": str(self.config)})
            return original(number, **kwargs)
        with mock.patch.object(pane_center, "_live_argv", side_effect=break_first):
            self.assertEqual(self.inspect_with([one, two], None, None), ["agent", "idle"])

    def test_a_process_replaced_inside_its_own_read_is_agent(self):
        pid = self.claude()
        self.record(pid)
        original = liveness.start_ticks
        calls = {"n": 0}

        def swap(number, **kwargs):
            value = original(number, **kwargs)
            calls["n"] += 1
            if calls["n"] == 3:                                       # between the two start reads of an observation
                self.home.process(number, ["claude"], start=START + 10, environ={"CLAUDE_CONFIG_DIR": str(self.config)})
            return value
        with mock.patch.object(liveness, "start_ticks", side_effect=swap):
            self.assertEqual(self.inspect_with([pid], None, None), ["agent"])

    def test_malformed_registry_values_are_refused_without_raising_and_do_not_hide_other_rows(self):
        good, bad = self.claude(91), self.claude(92)
        self.record(good)
        ticks = liveness.start_ticks(bad, proc_root=str(self.home.proc))
        shapes = {
            "fractional pid": b'{"pid": 92.5, "sessionId": "s", "procStart": "%s", "status": "idle"}' % ticks.encode(),
            "float pid": b'{"pid": 92.0, "sessionId": "s", "procStart": "%s", "status": "idle"}' % ticks.encode(),
            "infinite pid": b'{"pid": 1e309, "sessionId": "s", "procStart": "%s", "status": "idle"}' % ticks.encode(),
            "string pid": b'{"pid": "92", "sessionId": "s", "procStart": "%s", "status": "idle"}' % ticks.encode(),
            "boolean pid": b'{"pid": true, "sessionId": "s", "procStart": "%s", "status": "idle"}' % ticks.encode(),
            "huge pid": b'{"pid": 99999999999999999999, "sessionId": "s", "procStart": "1", "status": "idle"}',
            "negative pid": b'{"pid": -92, "sessionId": "s", "procStart": "1", "status": "idle"}',
            "float start": b'{"pid": 92, "sessionId": "s", "procStart": 1.5, "status": "idle"}',
            "signed start": b'{"pid": 92, "sessionId": "s", "procStart": "-%s", "status": "idle"}' % ticks.encode(),
            "spaced start": b'{"pid": 92, "sessionId": "s", "procStart": " %s", "status": "idle"}' % ticks.encode(),
            "deep": b'{"pid": 92, "x": ' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}",
            "not utf-8": b'{"pid": 92, "sessionId": "\xff", "procStart": "%s"}' % ticks.encode(),
            "not an object": b"[92]",
            "truncated": b'{"pid": 92, "sessionId": "s"',
        }
        for name, raw in shapes.items():
            (self.config / "sessions" / "92.json").write_bytes(raw)
            self.assertEqual(self.inspect_with([good, bad], None, None), ["idle", "agent"], name)
        # a start that is a JSON integer is as good as the digit string Claude writes
        (self.config / "sessions" / "92.json").write_text(json.dumps(
            {"pid": 92, "sessionId": uuid(2), "procStart": int(ticks), "status": "busy"}))
        self.assertEqual(self.inspect_with([good, bad], None, None), ["idle", "working"])

    def test_the_registry_is_the_target_processes_own_not_the_readers(self):
        target = self.home.root / "target-claude"
        pid = self.claude(environ={"CLAUDE_CONFIG_DIR": str(target)})
        (target / "sessions").mkdir(parents=True)
        self.record(pid, status="busy")                             # a matching record, in the reader's directory only
        self.assertEqual(self.activity(pid, reader_config=self.config), "agent")
        self.record(pid, config=target, status="waiting")
        self.assertEqual(self.activity(pid, reader_config=self.config), "waiting")

    def test_home_is_the_fallback_and_unknown_context_is_agent(self):
        pid = self.claude(environ={"HOME": str(self.home.root / "h")})
        self.record(pid, config=self.home.root / "h" / ".claude", status="busy")
        self.assertEqual(self.activity(pid), "working")
        self.claude(pid, environ={})                                # no CLAUDE_CONFIG_DIR and no HOME
        self.assertEqual(self.activity(pid, reader_config=self.config), "agent")
        self.claude(pid, environ={"CLAUDE_CONFIG_DIR": "relative/dir"})
        self.assertEqual(self.activity(pid), "agent")
        self.claude(pid)
        (self.home.proc / str(pid) / "environ").unlink()            # environment unreadable
        self.assertEqual(self.activity(pid), "agent")


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
