"""kilix-rollout-resume: session discovery, safety, and pacing.

Three agents store conversations three different ways, so most of these build a
small transcript in each layout and assert the recovery state read back out of
it. The rest pin the properties that make this safe to put behind a menu: it
never resumes a session another process still owns, it never pipes anything
into a shell without a yes, and the install commands it would run are exactly
the ones its vendors document.
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_rollout import claude, codex, kimi, launch, manage, menu, providers  # noqa: E402
from kilix_rollout.model import Session  # noqa: E402

CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CODEX_ID = "22222222-2222-4222-8222-222222222222"
KIMI_ID = "session_33333333-3333-4333-8333-333333333333"


def load_tool():
    path = ROOT / "tools" / "rollout_resume" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_rollout_resume", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_lines(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


# ── Claude Code ──────────────────────────────────────────────────────────────

def claude_tree(root, *, ending="tool_use", cwd="/tmp/project"):
    base = {"cwd": cwd, "sessionId": CLAUDE_ID, "isSidechain": False}
    records = [
        {"type": "mode", "mode": "normal", "sessionId": CLAUDE_ID},
        dict(base, type="user",
             message={"role": "user", "content": "recover this session"}),
        dict(base, type="assistant",
             message={"role": "assistant",
                      "content": [{"type": "text", "text": "working"}]}),
    ]
    if ending == "tool_use":
        records.append(dict(base, type="assistant", message={
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}))
    elif ending == "tool_result":
        records.append(dict(base, type="user", message={
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}))
    elif ending == "aborted":
        records.append(dict(base, type="user", message={
            "role": "user", "content": "[Request interrupted by user]"}))
    elif ending == "sidechain":
        tail = dict(base, type="assistant", message={
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t9", "name": "Grep", "input": {}}]})
        tail["isSidechain"] = True
        records.append(tail)
    records.append({"type": "ai-title", "aiTitle": "Recover the session",
                    "sessionId": CLAUDE_ID})
    write_lines(os.path.join(root, "projects", "-tmp-project",
                             f"{CLAUDE_ID}.jsonl"), records)
    return root


class ClaudeTests(unittest.TestCase):
    def discover(self, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            claude_tree(temporary, **kwargs)
            return claude.discover(root=temporary,
                                   proc_root=os.path.join(temporary, "noproc"))

    def test_unanswered_tool_call_is_cut_off(self):
        found = self.discover()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].state, "cut-off")
        self.assertEqual(found[0].session_id, CLAUDE_ID)
        self.assertEqual(found[0].cwd, "/tmp/project")
        self.assertEqual(found[0].title, "Recover the session")

    def test_unanswered_tool_result_is_cut_off(self):
        self.assertEqual(self.discover(ending="tool_result")[0].state, "cut-off")

    def test_finished_turn_is_idle(self):
        self.assertEqual(self.discover(ending="text")[0].state, "idle")

    def test_deliberate_abort_is_not_a_cut_off(self):
        self.assertEqual(self.discover(ending="aborted")[0].state, "idle")

    def test_subagent_tail_does_not_decide_the_state(self):
        self.assertEqual(self.discover(ending="sidechain")[0].state, "idle")

    def test_truncated_final_record_is_tolerated(self):
        with tempfile.TemporaryDirectory() as temporary:
            claude_tree(temporary)
            path = os.path.join(temporary, "projects", "-tmp-project",
                                f"{CLAUDE_ID}.jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"type":"assistant","message":')
            found = claude.discover(root=temporary,
                                    proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(found[0].state, "cut-off")

    def test_live_registry_entry_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            claude_tree(temporary)
            proc = os.path.join(temporary, "proc", "4242")
            os.makedirs(proc)
            fields = ["4242", "(claude test)", "S"] + [str(n) for n in range(4, 53)]
            fields[21] = "999"
            with open(os.path.join(proc, "stat"), "w", encoding="utf-8") as handle:
                handle.write(" ".join(fields) + "\n")
            os.makedirs(os.path.join(temporary, "sessions"))
            with open(os.path.join(temporary, "sessions", "4242.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"pid": 4242, "sessionId": CLAUDE_ID,
                           "procStart": "999"}, handle)
            found = claude.discover(root=temporary,
                                    proc_root=os.path.join(temporary, "proc"))
            self.assertEqual(found[0].state, "live")
            self.assertEqual(found[0].pids, (4242,))
            self.assertFalse(found[0].resumable)

    def test_recycled_process_id_does_not_resurrect_a_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            claude_tree(temporary)
            proc = os.path.join(temporary, "proc", "4242")
            os.makedirs(proc)
            fields = ["4242", "(other)", "S"] + [str(n) for n in range(4, 53)]
            fields[21] = "12345"          # started at a different time
            with open(os.path.join(proc, "stat"), "w", encoding="utf-8") as handle:
                handle.write(" ".join(fields) + "\n")
            os.makedirs(os.path.join(temporary, "sessions"))
            with open(os.path.join(temporary, "sessions", "4242.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"pid": 4242, "sessionId": CLAUDE_ID,
                           "procStart": "999"}, handle)
            found = claude.discover(root=temporary,
                                    proc_root=os.path.join(temporary, "proc"))
            self.assertNotEqual(found[0].state, "live")

    def test_slash_command_prompts_read_as_typed(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_lines(os.path.join(temporary, "projects", "-tmp-p",
                                     f"{CLAUDE_ID}.jsonl"), [
                {"type": "user", "cwd": "/tmp/p", "sessionId": CLAUDE_ID,
                 "message": {"role": "user", "content": [{"type": "text", "text":
                     "<command-name>/model</command-name>"
                     "<command-message>model</command-message>"
                     "<command-args>opus</command-args>"}]}},
            ])
            found = claude.discover(root=temporary,
                                    proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(found[0].title, "/model opus")


# ── Codex ────────────────────────────────────────────────────────────────────

class CodexTests(unittest.TestCase):
    def build(self, root, *, complete=False):
        records = [
            {"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/tmp/old"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "turn_context", "payload": {"cwd": "/tmp/codex"}},
            {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "reverse engineer this"}},
        ]
        if complete:
            records.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        write_lines(os.path.join(root, "sessions", "2026", "07", "28",
                                 f"rollout-{CODEX_ID}.jsonl"), records)

    def test_task_started_without_completion_is_cut_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.build(temporary)
            found = codex.discover(root=temporary,
                                   proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].state, "cut-off")
            self.assertEqual(found[0].session_id, CODEX_ID)
            self.assertEqual(found[0].cwd, "/tmp/codex")
            self.assertEqual(found[0].title, "reverse engineer this")

    def test_completed_task_is_idle(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.build(temporary, complete=True)
            found = codex.discover(root=temporary,
                                   proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(found[0].state, "idle")

    def test_an_open_descriptor_marks_it_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.build(temporary)
            rollout = os.path.join(temporary, "sessions", "2026", "07", "28",
                                   f"rollout-{CODEX_ID}.jsonl")
            descriptors = os.path.join(temporary, "proc", str(os.getpid()), "fd")
            os.makedirs(descriptors)
            os.symlink(rollout, os.path.join(descriptors, "7"))
            found = codex.discover(root=temporary,
                                   proc_root=os.path.join(temporary, "proc"))
            self.assertEqual(found[0].state, "live")


# ── Kimi Code ────────────────────────────────────────────────────────────────

class KimiTests(unittest.TestCase):
    def build(self, root, *, complete=False, title="New Session"):
        directory = os.path.join(root, "sessions", "wd_project_abc", KIMI_ID)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(root, "session_index.jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({"sessionId": KIMI_ID, "sessionDir": directory,
                                     "workDir": "/tmp/kimi"}) + "\n")
        with open(os.path.join(directory, "state.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"title": title, "workDir": "/tmp/kimi"}, handle)
        records = [
            {"type": "metadata", "protocol_version": "1.4"},
            {"type": "turn.prompt", "input": [{"type": "text", "text": "build a thing"}]},
            {"type": "context.append_loop_event",
             "event": {"type": "step.begin", "step": 1}},
        ]
        if complete:
            records.append({"type": "context.append_loop_event",
                            "event": {"type": "step.end", "step": 1}})
        write_lines(kimi.wire_path(directory), records)
        return directory

    def test_step_without_an_end_is_cut_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.build(temporary)
            found = kimi.discover(root=temporary,
                                  proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].state, "cut-off")
            self.assertEqual(found[0].cwd, "/tmp/kimi")
            # A placeholder title falls back to what was actually asked.
            self.assertEqual(found[0].title, "build a thing")

    def test_finished_step_is_idle(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.build(temporary, complete=True)
            found = kimi.discover(root=temporary,
                                  proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(found[0].state, "idle")

    def test_a_real_title_is_preferred_over_the_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.build(temporary, complete=True, title="Tiny LLM research")
            found = kimi.discover(root=temporary,
                                  proc_root=os.path.join(temporary, "noproc"))
            self.assertEqual(found[0].title, "Tiny LLM research")

    def test_a_missing_session_directory_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            with open(os.path.join(temporary, "session_index.jsonl"), "w",
                      encoding="utf-8") as handle:
                handle.write(json.dumps({"sessionId": KIMI_ID,
                                         "sessionDir": "/nonexistent/session"}) + "\n")
            self.assertEqual(kimi.discover(root=temporary), [])


# ── resume commands and pacing ───────────────────────────────────────────────

def sample(provider_key, *, state="idle", cwd="/tmp"):
    return Session(provider=provider_key, session_id="abc123", path="/tmp/t",
                   cwd=cwd, title="t", updated=0.0, state=state)


class ResumeTests(unittest.TestCase):
    def test_each_agent_gets_its_own_documented_resume_command(self):
        self.assertEqual(launch.resume_command(sample("claude")),
                         ["claude", "--resume", "abc123"])
        self.assertEqual(launch.resume_command(sample("codex")),
                         ["codex", "resume", "abc123"])
        self.assertEqual(launch.resume_command(sample("kimi")),
                         ["kimi", "--session", "abc123"])

    def test_a_vanished_working_directory_is_refused(self):
        with self.assertRaises(RuntimeError):
            launch.working_directory(sample("claude", cwd="/nonexistent/dir"))
        with self.assertRaises(RuntimeError):
            launch.working_directory(sample("claude", cwd=""))

    def test_tmux_names_are_unique_and_carry_the_agent(self):
        item = sample("codex", cwd="/tmp/my project")
        first = launch.tmux_name(item)
        self.assertTrue(first.startswith("codex_"))
        self.assertEqual(launch.tmux_name(item, {first}), f"{first}_2")

    def test_batch_restore_waits_between_launches(self):
        waits = []

        class Runner:
            def __call__(self, argv, **kwargs):
                import subprocess
                if argv[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(argv, 1, "", "no server")
                return subprocess.CompletedProcess(argv, 0, "", "")

        sessions = [sample("codex", cwd="/tmp"), sample("codex", cwd="/tmp")]
        original = manage.installed
        manage.installed = lambda item: "/usr/bin/" + item.command
        try:
            results = launch.restore_all(sessions, gap=30.0, runner=Runner(),
                                         sleeper=waits.append)
        finally:
            manage.installed = original
        self.assertTrue(all(result["ok"] for result in results))
        self.assertAlmostEqual(sum(waits), 30.0, places=3)

    def test_a_failed_launch_does_not_make_the_next_one_wait(self):
        waits = []

        def runner(argv, **kwargs):
            import subprocess
            if argv[:2] == ["tmux", "list-sessions"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        # The first session's directory is gone, so it never starts.
        sessions = [sample("codex", cwd="/nonexistent"), sample("codex", cwd="/tmp")]
        original = manage.installed
        manage.installed = lambda item: "/usr/bin/" + item.command
        try:
            results = launch.restore_all(sessions, gap=30.0, runner=runner,
                                         sleeper=waits.append)
        finally:
            manage.installed = original
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertEqual(waits, [])


# ── install / update ─────────────────────────────────────────────────────────

class ManagementTests(unittest.TestCase):
    def test_install_commands_match_the_vendor_documentation(self):
        """Pinned so any change to what gets piped into a shell shows in a diff."""
        documented = {
            "claude": ("curl -fsSL https://claude.ai/install.sh | bash",
                       "https://code.claude.com/docs/en/quickstart"),
            "codex": ("curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                      "https://developers.openai.com/codex/cli/"),
            "kimi": ("curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash",
                     "https://moonshotai.github.io/kimi-code/"),
        }
        for item in providers.PROVIDERS:
            command, source = documented[item.key]
            self.assertEqual(item.install_shell, command)
            self.assertEqual(item.install_source, source)

    def test_updates_delegate_to_each_agent_rather_than_reinstalling(self):
        for item in providers.PROVIDERS:
            self.assertEqual(item.update_argv[0], item.command)
            self.assertNotIn("curl", item.update_argv)

    def test_updating_a_missing_agent_is_refused(self):
        item = providers.provider("claude")
        original = manage.installed
        manage.installed = lambda _: ""
        try:
            with self.assertRaises(RuntimeError):
                manage.run_update(item)
        finally:
            manage.installed = original

    def test_declining_the_prompt_runs_no_installer(self):
        tool = load_tool()
        calls = []
        original_run = manage.run_install
        original_installed = manage.installed
        manage.run_install = lambda item, **kwargs: calls.append(item) or 0
        manage.installed = lambda _: ""
        replaced = tool.__builtins__["input"] if isinstance(tool.__builtins__, dict) else None
        try:
            import builtins
            saved = builtins.input
            builtins.input = lambda *_: "n"
            try:
                code = tool._cmd_install("claude", False)
            finally:
                builtins.input = saved
        finally:
            manage.run_install = original_run
            manage.installed = original_installed
            del replaced
        self.assertEqual(calls, [], "an install must never run without a yes")
        self.assertEqual(code, 1)

    def test_unknown_agent_names_are_rejected(self):
        with self.assertRaises(KeyError):
            providers.provider("gpt")


# ── start menu ───────────────────────────────────────────────────────────────

class MenuTests(unittest.TestCase):
    def test_update_entries_track_which_agents_are_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            present = {"claude"}
            menu.sync(providers.PROVIDERS, applications_dir=temporary,
                      is_installed=lambda item: item.key in present)
            names = set(os.listdir(temporary))
            self.assertIn(menu.PICKER, names)
            self.assertIn("kilix-update-claude.desktop", names)
            self.assertNotIn("kilix-update-codex.desktop", names)

            present.add("codex")
            menu.sync(providers.PROVIDERS, applications_dir=temporary,
                      is_installed=lambda item: item.key in present)
            self.assertIn("kilix-update-codex.desktop", os.listdir(temporary))

            present.clear()
            result = menu.sync(providers.PROVIDERS, applications_dir=temporary,
                               is_installed=lambda item: item.key in present)
            self.assertEqual(len(result["removed"]), 2)
            self.assertNotIn("kilix-update-claude.desktop", os.listdir(temporary))

    def test_entries_open_in_a_kilix_tab_under_development(self):
        text = menu.picker_entry()
        self.assertIn("Terminal=true", text)
        self.assertIn("Categories=Development;", text)
        self.assertIn("X-Kilix-Open=tab", text)

    def test_unmanaged_entries_are_never_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            foreign = os.path.join(temporary, "kilix-update-codex.desktop")
            with open(foreign, "w", encoding="utf-8") as handle:
                handle.write("[Desktop Entry]\nName=Someone else\n")
            menu.sync(providers.PROVIDERS, applications_dir=temporary,
                      is_installed=lambda item: False)
            self.assertTrue(os.path.exists(foreign))


# ── safety properties ────────────────────────────────────────────────────────

class SafetyTests(unittest.TestCase):
    @staticmethod
    def commands_invoked(relative):
        tree = ast.parse((ROOT / relative).read_text())
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in ("run", "Popen", "call", "check_output", "check_call"):
                continue
            for argument in node.args:
                if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts:
                    first = argument.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.add(first.value)
        return found

    def test_launching_only_ever_shells_out_to_tmux(self):
        """Recovery must not become an arbitrary command runner."""
        source = (ROOT / "src/kilix_rollout/launch.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertLessEqual(self.commands_invoked("src/kilix_rollout/launch.py"),
                             {"tmux"})

        used = []

        def runner(argv, **kwargs):
            import subprocess
            used.append(list(argv))
            if argv[:2] == ["tmux", "list-sessions"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        original = manage.installed
        manage.installed = lambda item: "/usr/bin/" + item.command
        try:
            launch.restore_all([sample("kimi", cwd="/tmp")], runner=runner,
                               sleeper=lambda _: None)
        finally:
            manage.installed = original
        self.assertTrue(used)
        for argv in used:
            self.assertEqual(argv[0], "tmux")

    def test_a_live_session_is_never_offered_for_recovery(self):
        self.assertFalse(sample("claude", state="live").resumable)
        self.assertTrue(sample("claude", state="cut-off").resumable)
        self.assertTrue(sample("claude", state="idle").resumable)

    def test_the_picker_refuses_to_resume_a_live_session(self):
        tool = load_tool()
        state = tool.State()
        state.pane = 0
        state.sessions = [sample("claude", state="live")]
        state.view = VIEW_ALL = tool.VIEWS.index("all")
        state.agent = 0
        state.selected = 0
        self.assertTrue(tool._resume_here(state))     # returned, did not exec
        self.assertIn("Protected", state.status)
        del VIEW_ALL

    def test_one_agent_failing_does_not_hide_the_others(self):
        original = providers.PROVIDERS[0].discover

        def explode(**kwargs):
            raise OSError("transcripts unreadable")

        object.__setattr__(providers.PROVIDERS[0], "discover", explode)
        try:
            providers.discover()          # must not raise
        finally:
            object.__setattr__(providers.PROVIDERS[0], "discover", original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
