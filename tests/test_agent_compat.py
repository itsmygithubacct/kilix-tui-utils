"""Codex/Grok observations stay useful while uncertain evidence stays unknown."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import subprocess
from dataclasses import replace
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kilix_rollout import codex, grok
from kilix_tui import pane_center
from test_agent_state import Home, START, uuid
from test_pane_center import agent_tree, two_agent_tree, load_tool, SESSION


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.home = Home(self)
        self.inspector = pane_center.Inspector(proc_root=str(self.home.proc))
        self.patch = mock.patch.object(pane_center, '_broker_statuses', return_value=({}, False, ''))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def snapshot(self, provider='codex'):
        return self.inspector.snapshot(agent_tree([provider], pid=91))

    def codex(self, events=('task_started', 'task_complete'), when=START + 5):
        self.home.process(91, ['codex'])
        path = self.home.rollout(1, stamp=START, events=events)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for r in records:
            r['timestamp'] = stamp(when)
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        fd = self.home.proc / '91' / 'fd' / '4'
        fd.symlink_to(path)
        info = self.home.proc / '91' / 'fdinfo'
        info.mkdir()
        (info / '4').write_text('flags:\t0100001\n')
        return path

    def grok(self, events=('turn_started', 'turn_ended')):
        base = self.home.root / 'grok'
        base.mkdir()
        self.home.process(91, ['grok'], environ={'GROK_HOME': str(base)})
        row = {'pid': 91, 'session_id': 'test-session', 'cwd': str(self.home.cwd),
               'opened_at': stamp(START + 1)}
        registry = base / 'active_sessions.json'
        registry.write_text(json.dumps([row]))
        folder = Path(grok.session_dir(row['session_id'], row['cwd'], str(base)))
        folder.mkdir(parents=True)
        path = folder / 'events.jsonl'
        path.write_text(''.join(json.dumps({'type': event, 'ts': stamp(START + 5)}) + '\n'
                                for event in events))
        return path, registry, row

    def test_codex_states_and_tell_wait_cli(self):
        path = self.codex()
        idle = self.snapshot()
        self.assertEqual(idle.panes[0].activity, 'idle')
        self.assertEqual(idle.panes[0].coding.session_id, uuid(1))
        tool = load_tool()
        with mock.patch.object(tool, '_live', return_value=idle), redirect_stdout(io.StringIO()), \
                mock.patch.object(tool.kitty_rc, 'send_text', return_value=5) as send:
            self.assertEqual(tool.cli(['wait', '9', '--for', 'idle', '--timeout', '0', '--json']), 0)
            self.assertEqual(tool.cli(['tell', '9', 'hello', '--json']), 0)
            self.assertEqual(send.call_args.args[0].broker_session, SESSION)
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'event_msg', 'timestamp': stamp(START + 6),
                                     'payload': {'type': 'task_started'}}) + '\n')
        self.assertEqual(self.snapshot().panes[0].activity, 'working')
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'event_msg', 'timestamp': stamp(START + 7),
                                     'payload': {'type': 'exec_approval_request', 'call_id': 'a'}}) + '\n')
        self.assertEqual(self.snapshot().panes[0].activity, 'waiting')

    def test_codex_stale_completion_and_missing_descriptor_are_unknown(self):
        self.codex(when=START - 10)
        self.assertEqual(self.snapshot().panes[0].activity, 'agent')
        (self.home.proc / '91/fd/4').unlink()
        self.assertEqual(self.snapshot().panes[0].activity, 'agent')

    def test_codex_readonly_replaced_or_multiple_rollouts_are_unknown(self):
        path = self.codex()
        info = self.home.proc / '91/fdinfo/4'
        info.write_text('flags:\t0100000\n')
        self.assertEqual(self.snapshot().panes[0].activity, 'agent')
        info.write_text('flags:\t0100001\n')
        other = self.home.rollout(2, stamp=START)
        (self.home.proc / '91/fd/5').symlink_to(other)
        (self.home.proc / '91/fdinfo/5').write_text('flags:\t0100001\n')
        self.assertEqual(self.snapshot().panes[0].activity, 'agent')
        (self.home.proc / '91/fd/5').unlink()
        original = codex.session_from_path
        with mock.patch.object(codex, 'session_from_path', wraps=original) as read:
            def change(*args, **kwargs):
                result = original(*args, **kwargs)
                path.write_text('{}\n')
                return result
            read.side_effect = change
            self.assertEqual(self.snapshot().panes[0].activity, 'agent')

    def test_shared_rollout_inode_across_panes_is_ambiguous(self):
        path = self.codex()
        self.home.process(92, ['codex'])
        alias = path.parent / 'rollout-alias.jsonl'
        os.link(path, alias)
        (self.home.proc / '92/fd/4').symlink_to(alias)
        (self.home.proc / '92/fdinfo').mkdir()
        (self.home.proc / '92/fdinfo/4').write_text('flags:\t0100001\n')
        snapshot = self.inspector.snapshot(two_agent_tree(['codex'], ['codex']))
        self.assertEqual([item.activity for item in snapshot.panes], ['agent', 'agent'])

    def wrapper_tree(self, *, parent=90, wrapper_start=START, reverse=False):
        self.home.process(90, ['node', '/usr/local/bin/codex'], start=wrapper_start)
        self.home.process(91, ['codex'], parent=parent)
        tree = agent_tree(['codex'], pid=91)
        child = tree.panes[0].processes[0]
        wrapper = replace(child, pid=90, argv=('node', '/usr/local/bin/codex'))
        processes = (child, wrapper) if reverse else (wrapper, child)
        tree.pages[0].panes[0] = replace(tree.panes[0], processes=processes)
        return tree

    def test_verified_wrapper_child_accepts_either_listing_order(self):
        self.codex()
        for reverse in (False, True):
            snapshot = self.inspector.snapshot(self.wrapper_tree(reverse=reverse))
            self.assertEqual(snapshot.panes[0].activity, 'idle')
            self.assertEqual(snapshot.panes[0].coding.pids, (91,))

    def test_unrelated_or_recycled_wrapper_is_ambiguous(self):
        self.codex()
        for kwargs in ({'parent': 89}, {'wrapper_start': START + 1}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.inspector.snapshot(self.wrapper_tree(**kwargs)).panes[0].activity, 'agent')
        tree = self.wrapper_tree()
        (self.home.proc / '90/cmdline').write_bytes(b'node\0other-script\0')
        self.assertEqual(self.inspector.snapshot(tree).panes[0].activity, 'agent')

    def test_wrapper_must_not_write_a_rollout_or_be_listed_twice(self):
        path = self.codex()
        tree = self.wrapper_tree()
        (self.home.proc / '90/fd/4').symlink_to(path)
        (self.home.proc / '90/fdinfo').mkdir()
        (self.home.proc / '90/fdinfo/4').write_text('flags:\t0100001\n')
        self.assertEqual(self.inspector.snapshot(tree).panes[0].activity, 'agent')
        (self.home.proc / '90/fd/4').unlink()
        tree.pages[0].panes.append(replace(tree.panes[0], id=10, processes=(tree.panes[0].processes[0],)))
        self.assertEqual([p.activity for p in self.inspector.snapshot(tree).panes], ['agent', 'agent'])

    def test_wrapper_relationship_is_rechecked_after_read(self):
        self.codex()
        tree = self.wrapper_tree()
        original = codex.session_from_path
        def reparent(*args, **kwargs):
            result = original(*args, **kwargs)
            self.home.process(91, ['codex'], parent=1)
            return result
        with mock.patch.object(codex, 'session_from_path', side_effect=reparent):
            self.assertEqual(self.inspector.snapshot(tree).panes[0].activity, 'agent')

    def test_grok_pending_permissions_require_coherent_resolution(self):
        path, _, _ = self.grok(events=())
        cases = [
            ([{'type': 'permission_requested'}, {'type': 'tool_completed'}], 'waiting'),
            ([{'type': 'permission_requested'}, {'type': 'permission_pending'},
              {'type': 'tool_completed'}, {'type': 'permission_resolved'}], 'working'),
            ([{'type': 'permission_requested', 'request_id': 'a'},
              {'type': 'permission_resolved', 'request_id': 'b'}], 'agent'),
            ([{'type': 'permission_requested', 'request_id': 'a'},
              {'type': 'permission_resolved'}], 'agent'),
            ([{'type': 'permission_requested', 'request_id': 'a'},
              {'type': 'permission_requested', 'request_id': 'b'},
              {'type': 'permission_resolved', 'request_id': 'a'},
              {'type': 'tool_completed'}], 'waiting'),
            ([{'type': 'permission_requested'}, {'type': 'permission_requested'},
              {'type': 'permission_resolved'}], 'agent'),
            ([{'type': 'permission_resolved'}], 'agent'),
            ([{'type': 'permission_requested', 'request_id': True}], 'agent'),
            ([{'type': 'permission_requested', 'turn_id': 'other'}], 'agent'),
        ]
        for events, expected in cases:
            with self.subTest(events=events):
                records = [{'type': 'turn_started'}] + events
                path.write_text(''.join(json.dumps({**r, 'ts': stamp(START + 5 + i)}) + '\n'
                                        for i, r in enumerate(records)))
                self.assertEqual(self.snapshot('grok').panes[0].activity, expected)

    def test_newer_malformed_or_user_event_never_reuses_idle(self):
        path = self.codex()
        original = path.read_bytes()
        for tail in (b'{', b'[]\n', b'{"type":"unknown"}\n',
                     json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message',
                                                                 'message': 'new task'}}).encode()):
            with self.subTest(tail=tail):
                path.write_bytes(original + tail)
                self.assertEqual(self.snapshot().panes[0].activity, 'agent')

    def test_process_recycle_and_changed_command_are_unknown(self):
        self.codex()
        self.home.process(91, ['codex'], start=START + 20)
        self.assertEqual(self.snapshot().panes[0].activity, 'agent')
        self.home.process(91, ['less', 'codex'])
        self.assertEqual(self.snapshot().panes[0].activity, 'agent')

    def test_grok_states_and_wait(self):
        path, _, _ = self.grok()
        self.assertEqual(self.snapshot('grok').panes[0].activity, 'idle')
        tool = load_tool()
        with mock.patch.object(tool, '_live', return_value=self.snapshot('grok')), redirect_stdout(io.StringIO()):
            self.assertEqual(tool.cli(['wait', '9', '--for', 'idle', '--timeout', '0']), 0)
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'turn_started', 'ts': stamp(START + 10)}) + '\n')
        self.assertEqual(self.snapshot('grok').panes[0].activity, 'working')
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'permission_requested', 'ts': stamp(START + 11)}) + '\n')
        self.assertEqual(self.snapshot('grok').panes[0].activity, 'waiting')

    def test_grok_known_model_phases_keep_turn_and_permission_boundaries(self):
        path, _, _ = self.grok()
        def write(records):
            path.write_text(''.join(json.dumps({**r, 'ts': stamp(START + 5 + i)}) + '\n'
                                    for i, r in enumerate(records)))
        start = {'type': 'turn_started'}
        for phase in ('waiting_for_model', 'streaming_text'):
            event = {'type': 'phase_changed', 'phase': phase}
            write([start, event])
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'working')
            write([start, {'type': 'permission_requested', 'request_id': 'a'}, event])
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'waiting')
            write([start, event, {'type': 'turn_ended'}])
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'idle')
        for phase in ('unknown', '', None, 1):
            write([start, {'type': 'phase_changed', 'phase': phase}, {'type': 'turn_ended'}])
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'agent')

    def test_grok_duplicate_stale_missing_or_changed_registry_is_unknown(self):
        path, registry, row = self.grok()
        for rows in ([row, row], [{**row, 'opened_at': stamp(START - 1)}],
                     [{k: v for k, v in row.items() if k != 'opened_at'}],
                     [row, {**row, 'pid': 92}]):
            registry.write_text(json.dumps(rows))
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'agent')
        registry.write_text(json.dumps([row]))
        original = grok.observed_activity
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            registry.write_text('[]')
            return result
        with mock.patch.object(grok, 'observed_activity', side_effect=change):
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'agent')

    def test_grok_incomplete_or_historical_log_is_unknown(self):
        path, _, _ = self.grok()
        original = path.read_bytes()
        for tail in (b'{', b'[]\n', b'{"type":"new_event"}\n', b'{"type":[]}\n',
                     b'{"type":"turn_ended","type":"turn_started"}\n'):
            path.write_bytes(original + tail)
            self.assertEqual(self.snapshot('grok').panes[0].activity, 'agent')
        path.write_bytes(original.replace(stamp(START + 5).encode(), stamp(START - 1).encode()))
        self.assertEqual(self.snapshot('grok').panes[0].activity, 'agent')

    def test_wait_rejects_a_replaced_broker(self):
        self.codex()
        first, second = self.snapshot(), self.snapshot()
        first.panes[0].activity = "working"
        second.panes[0].pane = replace(second.panes[0].pane, broker_session="b" * 32)
        tool = load_tool()
        with mock.patch.object(tool, '_live', side_effect=[first, second]), \
                mock.patch.object(tool.time, 'sleep'), self.assertRaisesRegex(ValueError, 'changed broker'):
            tool._cmd_wait(tool._cli_parser().parse_args(['wait', '9', '--timeout', '10']))

    def test_live_process_and_writable_descriptor(self):
        # This uses a real owned process and /proc, but no installed agent or live pane.
        (self.home.root / 'bin').mkdir()
        executable = self.home.root / 'bin/codex'
        executable.symlink_to(sys.executable)
        path = self.home.root / ('rollout-' + uuid(3) + '.jsonl')
        code = """import json,sys,time,datetime
handle=open(sys.argv[1], 'a')
for kind in ('task_started', 'task_complete'):
    handle.write(json.dumps({'type':'event_msg', 'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'payload':{'type':kind}})+'\\n')
handle.flush()
print('ready', flush=True)
sys.stdin.readline()
"""
        argv = [str(executable), '-c', code, str(path)]
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            inspector = pane_center.Inspector()
            got = inspector.snapshot(agent_tree(argv, pid=child.pid))
            self.assertEqual(got.panes[0].activity, 'idle')
            self.assertFalse(got.panes[0].to_dict()['activity_readiness_verified'])
            child.stdin.write('stop\n')
            child.stdin.flush()
            child.wait(timeout=5)
            self.assertEqual(inspector.snapshot(agent_tree(argv, pid=child.pid)).panes[0].activity, 'agent')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()

    def test_unknown_does_not_satisfy_wait_idle(self):
        self.codex(when=START - 1)
        tool = load_tool()
        with mock.patch.object(tool, '_live', return_value=self.snapshot()), redirect_stdout(io.StringIO()):
            self.assertEqual(tool.cli(['wait', '9', '--for', 'idle', '--timeout', '0', '--json']), 1)


if __name__ == '__main__':
    unittest.main()
