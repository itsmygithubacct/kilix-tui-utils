"""Discovery is metadata-only, keeps every tier, and never merges ambiguous IDs."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kilix_rollout.inventory import discover, describe, raw_entries


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, rows):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_all_providers_titles_roots_and_duplicate_ids(self):
        session = "12345678-1234-1234-1234-123456789abc"
        self.write("claude/project/one.jsonl", [{"type": "user", "sessionId": session,
            "cwd": "/workspace/project", "message": {"role": "user", "content": "First request"}},
            {"type": "custom-title", "customTitle": "Named conversation"}])
        self.write("codex/2026/11/13/rollout.jsonl", [{"type": "session_meta", "payload": {
            "id": session, "cwd": "/workspace/build"}}, {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Build the tool"}]}}])
        self.write("omp/project/session.jsonl", [{"type": "title", "title": "Qwen work"},
            {"type": "session", "id": session, "cwd": "/workspace/qwen"}])
        self.write("grok/%2Fworkspace%2Fdemo/" + session + "/updates.jsonl", [
            {"params": {"sessionId": session, "update": {"sessionUpdate": "user_message_chunk",
             "content": {"type": "text", "text": "Find the bug"}}}}])
        self.write("grok/%2Fworkspace%2Fdemo/" + session + "/events.jsonl", [{}])
        result = discover(locations={p: [self.root / p] for p in ("claude", "codex", "grok", "omp")})
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["sessions"]), 4)
        self.assertEqual(len({r["inventory_id"] for r in result["sessions"]}), 4)
        titles = {r["provider"]: r["title"] for r in result["sessions"]}
        self.assertEqual(titles, {"claude": "Named conversation", "codex": "Build the tool",
                                  "grok": "Find the bug", "omp": "Qwen work"})

    def test_recent_archived_and_live_names_and_recency(self):
        for name, stamp in (("live.log", 10), ("recent/recent.log.zst", 30), ("archive/old.log.zst", 20)):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"recording")
            os.utime(path, (stamp, stamp))
        (self.root / "recent.meta").write_text("cwd=/workspace/parser\ncmd=codex resume\ntitle=Parser repair\n")
        (self.root / "old.meta").write_text("cwd=/workspace/reports\ncmd=python report.py\n")
        (self.root / "escape.log").symlink_to(self.root / "live.log")
        rows = raw_entries(self.root)
        self.assertEqual([r["tier"] for r in rows], ["recent", "archived", "live"])
        self.assertEqual(rows[0]["title"], "Parser repair")
        self.assertEqual(rows[1]["title"], "reports · python report.py")
        spec = importlib.util.spec_from_file_location("session_logs", ROOT / "tools/session_log/main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = object.__new__(module.State)
        state.items = module.entries(str(self.root))
        from kilix_tui.shell import Filter
        state.filter = Filter()
        state.filter.open()
        for letter in "parser":
            state.filter.handle(ord(letter))
        self.assertEqual([r["id"] for r in state.view()], ["recent"])
        state.selected, state.viewing, state.directory = 0, None, str(self.root)
        state.items[0]["title"] = "Parser repair " + "long title " * 20
        frame = module.app.render_to_text(module.render, state, width=80, height=24)
        self.assertIn("[recent]", frame)
        self.assertIn("Parser repair", frame)

    def test_metadata_is_bounded_and_control_characters_do_not_label_rows(self):
        path = self.write("omp/session.jsonl", [{"type": "title", "title": "Title\x1b\nspoof"},
            {"type": "session", "id": "s", "cwd": "/workspace/project"}])
        with path.open("ab") as stream:
            stream.write(b"x" * 2000000 + b"\n")
        result = describe(path, "omp")
        self.assertNotIn("\x1b", result["title"])
        self.assertNotIn("\n", result["title"])
        self.assertEqual(result["session_id"], "s")
        limited = discover(locations={"omp": [self.root / "omp"]}, max_entries=0)
        self.assertFalse(limited["complete"])

    def test_codex_fork_keeps_child_identity_and_name(self):
        path = self.write("codex/child.jsonl", [
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "parent",
             "agent_nickname": "Parser worker", "cwd": "/workspace/child"}},
            {"type": "session_meta", "payload": {"id": "parent", "cwd": "/workspace/parent"}}])
        item = describe(path, "codex")
        self.assertEqual((item["session_id"], item["title"], item["cwd"]),
                         ("child", "Parser worker", "/workspace/child"))

    def test_saved_codex_names_override_prompts_without_rereading_unchanged_logs(self):
        session = "12345678-1234-1234-1234-123456789abc"
        path = self.write("codex/sessions/2026/11/13/rollout.jsonl", [
            {"type": "session_meta", "payload": {"id": session, "cwd": "/workspace/parser"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Generic prompt"}}])
        titles = self.root / "codex/session_index.jsonl"
        titles.write_text(json.dumps({"id": session, "thread_name": "Parser repair"}) + "\n")
        locations = {"codex": [self.root / "codex/sessions"]}
        result = discover(locations=locations)
        self.assertEqual(result["sessions"][0]["title"], "Parser repair")
        cached = {("codex", str(path)): result["sessions"][0]}
        with mock.patch("kilix_rollout.inventory.describe", side_effect=AssertionError("metadata reread")):
            result = discover(locations=locations, cached=cached)
        self.assertEqual(result["sessions"][0]["title"], "Parser repair")

    def omp_log(self, relative, session_id, *, parent=None, agent=None, prompt="", title="",
                system="Follow the rules.", stamp=None, tail=()):
        """An OMP log shaped like OMP's own: a padded title slot, the session
        header, then for a subagent its session_init and assignment."""
        header = {"type": "session", "version": 3, "id": session_id, "cwd": "/workspace/corpus"}
        if parent is not None:
            header["parentSession"] = str(parent)
        rows = [{"type": "title", "v": 1, "title": title, "pad": " " * 64}, header]
        if agent is not None:
            rows.append({"type": "session_init", "agent": agent, "task": prompt, "systemPrompt": system})
        if prompt:
            rows.append({"type": "message", "message": {"role": "user", "content": [
                {"type": "text", "text": prompt}]}})
        path = self.write("omp/" + relative, rows + list(tail))
        if stamp is not None:
            os.utime(path, (stamp, stamp))
        return path

    def nested_tree(self, base):
        """A session, its subagent, a grandchild, and a deeper great-grandchild."""
        session = "-workspace-corpus/2026-01-02T03-04-05-006Z_0000aaaa-0000-7000-8000-00000000000" + base
        top = self.omp_log(session + ".jsonl", session[-36:], prompt="Repair the corpus")
        child = self.omp_log(session + "/MapTrainer.jsonl", "child-" + base, parent=top, agent="scout")
        grandchild = self.omp_log(session + "/MapTrainer/MapTrainer.Stages.jsonl", "grandchild-" + base,
                                  parent=child, agent="scout")
        deeper = self.omp_log(session + "/MapTrainer/MapTrainer.Stages/MapTrainer.Stages.Check.jsonl",
                              "deeper-" + base, parent=grandchild, agent="reviewer")
        return top, child, grandchild, deeper

    def test_nested_omp_subagent_logs_are_separate_sessions_in_recency_order(self):
        parent_id = "0000aaaa-0000-7000-8000-000000000001"
        session = "-workspace-corpus/2026-01-02T03-04-05-006Z_" + parent_id
        assignment = "Assignment from the parent agent. Target: "
        parent = self.omp_log(session + ".jsonl", parent_id, title="Corpus repair",
                              prompt="Repair the corpus", stamp=100)
        mapper = self.omp_log(session + "/MapParser.jsonl", "child-parser", parent=parent, agent="scout",
                              prompt=assignment + "map the parser", stamp=400)
        trainer = self.omp_log(session + "/MapTrainer.jsonl", "child-trainer", parent=parent, agent="scout",
                               prompt=assignment + "map the trainer", stamp=200)
        stages = self.omp_log(session + "/MapTrainer/MapTrainer.Stages.jsonl", "grandchild-stages",
                              parent=trainer, agent="reviewer", prompt=assignment + "list the stages", stamp=500)
        other_id = "0000bbbb-0000-7000-8000-000000000001"
        other = self.omp_log("-workspace-arcade/2026-01-03T04-05-06-007Z_" + other_id + ".jsonl", other_id,
                             prompt="Review the arcade shell", stamp=300)
        # Task outputs and tool logs sit beside subagent logs; they are not recordings.
        for artifact in ("MapParser.md", "MapParser.json", "0.bash.log", "7.bash-original.log",
                         "MapTrainer/3.bash.log"):
            (self.root / "omp" / session / artifact).write_text("{}\n")
        locations = {"omp": [self.root / "omp"]}
        result = discover("qwen", locations=locations)
        self.assertTrue(result["complete"], result["errors"])
        rows = result["sessions"]
        self.assertEqual([r["path"] for r in rows], [str(p) for p in (stages, mapper, other, trainer, parent)])
        self.assertEqual([r["session_id"] for r in rows],
                         ["grandchild-stages", "child-parser", other_id, "child-trainer", parent_id])
        self.assertEqual([r["title"] for r in rows], ["MapTrainer.Stages · reviewer", "MapParser · scout",
                                                      "Review the arcade shell", "MapTrainer · scout",
                                                      "Corpus repair"])
        self.assertEqual({r["provider"] for r in rows}, {"omp"})
        self.assertEqual(len({r["inventory_id"] for r in rows}), 5)
        self.assertEqual(discover("omp", locations=locations)["sessions"], rows)
        self.assertEqual({k: describe(mapper, "qwen")[k] for k in ("provider", "session_id", "title")},
                         {"provider": "omp", "session_id": "child-parser", "title": "MapParser · scout"})

    def test_omp_child_names_and_identities_come_from_the_child_itself(self):
        parent_id = "0000cccc-0000-7000-8000-000000000001"
        session = "-workspace-corpus/2026-01-02T03-04-05-006Z_" + parent_id
        # The parent may already be archived; its children still name it.
        parent = self.root / "omp" / (session + ".jsonl")
        named = self.omp_log(session + "/AuditShell.jsonl", "child-shell", parent=parent, agent="reviewer",
                             title="Shell audit", prompt="Assignment from the parent agent. Target: the shell")
        # An agent definition can outgrow the metadata window; the child's place
        # in the directory its header names as parent still identifies it.
        filler = {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "y" * 1000}]}}
        hidden = self.omp_log(session + "/AuditCore.jsonl", "child-core", parent=parent, agent="reviewer",
                              system="x" * 70000, prompt="Assignment from the parent agent. Target: the core",
                              tail=[filler] * 100)
        orphan = self.write("omp/" + session + "/Truncated.jsonl", [
            {"type": "message", "message": {"role": "user", "content": "Partial assignment"}}])
        fork_id = "0000cccc-0000-7000-8000-000000000002"
        fork = self.omp_log("-workspace-corpus/2026-01-04T05-06-07-008Z_" + fork_id + ".jsonl", fork_id,
                            parent=parent, prompt="Continue the corpus repair")
        expected = {named: ("child-shell", "Shell audit"), hidden: ("child-core", "AuditCore"),
                    orphan: ("", "Partial assignment"), fork: (fork_id, "Continue the corpus repair")}
        result = discover(locations={"omp": [self.root / "omp"]})
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual({Path(r["path"]): (r["session_id"], r["title"]) for r in result["sessions"]}, expected)
        # Entries cached by an older parser are described again, not reused.
        stale = dict(describe(hidden, "omp"), inventory_version=1, title="corpus")
        current = dict(describe(named, "omp"), title="Kept name")
        cached = {("omp", str(hidden)): stale, ("omp", str(named)): current}
        titles = {r["path"]: r["title"] for r in discover(locations={"omp": [self.root / "omp"]},
                                                         cached=cached)["sessions"]}
        self.assertEqual((titles[str(hidden)], titles[str(named)]), ("AuditCore", "Kept name"))

    def test_nested_omp_walk_has_no_depth_ceiling_and_reports_an_exhausted_budget(self):
        session = "-workspace-corpus/2026-01-02T03-04-05-006Z_0000dddd-0000-7000-8000-000000000001"
        self.omp_log(session + ".jsonl", session[-36:], prompt="Repair the corpus")
        deepest = self.omp_log(session + "".join("/Stage%02d" % i for i in range(40)) + "/Deepest.jsonl",
                               "deepest", agent="scout", prompt="Deep task")
        root = self.root / "omp"
        entries = sum(len(dirs) + len(files) for _, dirs, files in os.walk(root))
        result = discover(locations={"omp": [root]}, max_entries=entries)
        self.assertTrue(result["complete"], result["errors"])
        self.assertIn(str(deepest), [r["path"] for r in result["sessions"]])
        # Breadth first, a budget that runs out costs the deepest logs and says so.
        for limited in (discover(locations={"omp": [root]}, max_entries=entries - 1),
                        discover("qwen", locations={"omp": [root]}, max_seconds=-1)):
            self.assertFalse(limited["complete"])
            self.assertEqual(limited["errors"], [{"code": "inventory_incomplete", "path": str(root),
                                                  "message": "inventory discovery limit reached"}])
            self.assertNotIn(str(deepest), [r["path"] for r in limited["sessions"]])

    def test_nested_omp_walk_never_follows_links_cycles_or_special_files(self):
        top, child, grandchild, deeper = self.nested_tree("1")
        artifacts = child.parent
        planted = self.write("outside/MapTrainer/Planted.jsonl", [{"type": "session", "id": "planted"}])
        (artifacts / "escape").symlink_to(planted.parent.parent, target_is_directory=True)
        (artifacts / "MapTrainer" / "loop").symlink_to(self.root / "omp", target_is_directory=True)
        (artifacts / "Linked.jsonl").symlink_to(planted)
        if hasattr(os, "mkfifo"):
            os.mkfifo(artifacts / "Pipe.jsonl")
        expected = sorted(str(p) for p in (top, child, grandchild, deeper))
        result = discover(locations={"omp": [self.root / "omp"]})
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(sorted(r["path"] for r in result["sessions"]), expected)
        # A configured root may itself be a link, as a relocated agent home is.
        link = self.root / "omp-home"
        link.symlink_to(self.root / "omp", target_is_directory=True)
        result = discover(locations={"omp": [link]})
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(sorted(r["path"] for r in result["sessions"]),
                         [p.replace(str(self.root / "omp"), str(link), 1) for p in expected])

    @unittest.skipIf(os.geteuid() == 0, "permissions do not bind root")
    def test_nested_omp_walk_reports_an_unreadable_directory_and_walks_on(self):
        top, child, grandchild, deeper = self.nested_tree("2")
        locked = child.parent / "Locked"
        locked.mkdir()
        self.omp_log(str(locked.relative_to(self.root / "omp")) + "/Locked.Inner.jsonl", "inner", agent="scout")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        result = discover(locations={"omp": [self.root / "omp"]})
        self.assertFalse(result["complete"])
        self.assertEqual([(e["code"], e["path"]) for e in result["errors"]], [("inventory_incomplete", str(locked))])
        # Directories walked after the unreadable one are still inventoried.
        self.assertEqual(sorted(r["path"] for r in result["sessions"]),
                         sorted(str(p) for p in (top, child, grandchild, deeper)))

    def test_nested_omp_walk_refuses_a_directory_replaced_after_listing(self):
        real_open = os.open
        for case, base in (("link", "3"), ("ancestor", "4")):
            top, child, grandchild, deeper = self.nested_tree(base)
            target = grandchild.parent
            outside = self.root / ("outside-" + case)
            planted = self.write(outside.name + "/MapTrainer/Planted.jsonl", [{"type": "session", "id": "planted"}])
            raced = []

            def racing_open(path, flags, *args, **kwargs):
                if os.fspath(path) == str(target) and not raced:
                    raced.append(case)
                    # Swap the directory, or one of its ancestors, for a link out of the tree.
                    moved = target if case == "link" else target.parent
                    moved.rename(moved.with_name(moved.name + ".moved"))
                    moved.symlink_to(planted.parent if case == "link" else planted.parent.parent,
                                     target_is_directory=True)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch("os.open", racing_open):
                result = discover(locations={"omp": [self.root / "omp"]})
            self.assertEqual(raced, [case])
            self.assertFalse(result["complete"])
            self.assertEqual([(e["code"], e["path"]) for e in result["errors"]],
                             [("inventory_incomplete", str(target))])
            if case == "ancestor":
                self.assertEqual(result["errors"][0]["message"], "directory changed during discovery")
            paths = {r["path"] for r in result["sessions"]}
            self.assertNotIn(planted.name, {Path(p).name for p in paths})
            self.assertTrue({str(top), str(child)} <= paths)
            shutil.rmtree(self.root / "omp")
            shutil.rmtree(outside)

    @unittest.skipUnless(shutil.which("zstd"), "zstd required")
    def test_recent_archive_preview_keeps_the_tail(self):
        spec = importlib.util.spec_from_file_location("session_logs", ROOT / "tools/session_log/main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.root / "recent.log.zst"
        path.write_bytes(subprocess.run(["zstd", "-qc"], input=b"older\n" * 10000 + b"newest\n",
                                       capture_output=True, check=True).stdout)
        self.assertEqual(module.read_transcript({"path": str(path), "tier": "recent"}, limit=7), "newest\n")


if __name__ == "__main__":
    unittest.main()
