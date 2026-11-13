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
