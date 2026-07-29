"""Every tool renders, handles input, and stays inside its surface.

These are contract tests rather than deep per-tool tests: each tool must import
without a terminal, render headlessly at an awkward size without raising, clip
to its surface, and quit when told. That is what makes a tool safe to put behind
a desktop menu, and it is the check most likely to catch a regression in the
shared core.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import app, keys as keymap, proc  # noqa: E402

TOOLS = [
    "calculator", "cpu", "memory", "disk", "system", "volume",
    "file", "package", "session_log", "weather", "music", "plebian_control",
    "rollout_resume", "switcher",
]


def load(name):
    path = ROOT / "tools" / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_state(module):
    if hasattr(module, "State"):
        return module.State()
    if hasattr(module, "facts"):
        return module.facts()
    raise AssertionError("tool exposes neither State nor facts")


class ToolContractTests(unittest.TestCase):
    def test_every_tool_imports_without_a_terminal(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                module = load(name)
                self.assertTrue(hasattr(module, "render"))
                self.assertTrue(hasattr(module, "main"))

    def test_every_tool_renders_headlessly(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                module = load(name)
                frame = app.render_to_text(module.render, make_state(module))
                self.assertIsInstance(frame, str)

    def test_rendering_clips_to_awkward_sizes(self):
        # A pane can be one column wide. Nothing may raise or overrun.
        for name in TOOLS:
            for height, width in ((24, 80), (10, 40), (5, 20), (3, 8)):
                with self.subTest(tool=name, size=(height, width)):
                    module = load(name)
                    frame = app.render_to_text(
                        module.render, make_state(module),
                        height=height, width=width)
                    for line in frame.splitlines():
                        self.assertLessEqual(len(line), width)
                    self.assertLessEqual(len(frame.splitlines()), height)

    def test_quit_key_exits_every_interactive_tool(self):
        for name in TOOLS:
            module = load(name)
            if not hasattr(module, "handle"):
                continue
            with self.subTest(tool=name):
                state = make_state(module)
                # The calculator types 'q' into a non-empty entry by design.
                if name == "calculator":
                    state.entry = ""
                self.assertFalse(module.handle(ord("q"), state))


class SafetyTests(unittest.TestCase):
    """Properties that make a tool safe to put one keystroke from a menu."""

    @staticmethod
    def commands_invoked(relative: str) -> set[str]:
        """External commands a tool can run, read out of its source.

        Substring matching over source text is useless here — "install" appears
        in `installed()`, and prose in a docstring explains why apt is avoided.
        This walks the AST and collects the first element of every list literal
        handed to subprocess, which is the actual property under test.
        """
        import ast
        tree = ast.parse((ROOT / relative).read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "attr", getattr(target, "id", ""))
            if name not in ("run", "Popen", "call", "check_output",
                            "check_call"):
                continue
            for argument in node.args:
                if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts:
                    first = argument.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(
                            first.value, str):
                        found.add(first.value)
        return found

    def test_package_viewer_runs_only_read_only_commands(self):
        self.assertEqual(self.commands_invoked("tools/package/main.py"),
                         {"dpkg-query"},
                         "the package viewer must never mutate the system")

    def test_file_manager_has_no_destructive_operations(self):
        import ast
        tree = ast.parse((ROOT / "tools/file/main.py").read_text())
        forbidden = {"remove", "unlink", "rmtree", "rmdir", "move", "rename",
                     "chmod", "chown"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                self.assertNotIn(name, forbidden,
                                 f"file manager must not call {name}()")

    def test_weather_uses_no_ip_geolocation_and_no_api_key(self):
        source = (ROOT / "tools/weather/main.py").read_text()
        self.assertIn("api.open-meteo.com", source)
        for forbidden in ("api_key", "apikey", "ipinfo", "ip-api", "geolocate"):
            self.assertNotIn(forbidden, source.lower())

    def test_control_tui_confirms_before_power_and_autologin(self):
        module = load("plebian_control")
        state = module.State()
        for index, name in enumerate(module.SECTIONS):
            if name not in ("Power", "Session", "Update"):
                continue
            state.section = index
            for label, argv, needs in state.actions():
                if argv and (argv[0] == "systemctl" or "autologin" in argv
                             or argv[0] == "plebian-os-update"
                             or "terminate-session" in argv):
                    self.assertTrue(needs, f"{label} must confirm first")

    def test_control_tui_shells_out_rather_than_reimplementing(self):
        module = load("plebian_control")
        state = module.State()
        seen = set()
        for index in range(len(module.SECTIONS)):
            state.section = index
            for _label, argv, _needs in state.actions():
                if argv:
                    seen.add(argv[0])
        # It must drive the existing commands, not carry its own logic.
        self.assertTrue({"pleb", "plebian-os-update", "systemctl"} <= seen)


class SharedCoreTests(unittest.TestCase):
    def test_human_bytes_and_duration(self):
        self.assertEqual(proc.human_bytes(0), "0B")
        self.assertEqual(proc.human_bytes(1536), "1.5K")
        self.assertTrue(proc.human_bytes(5 * 1024 ** 3).endswith("G"))
        self.assertEqual(proc.human_duration(90), "1m")
        self.assertEqual(proc.human_duration(3700), "1h 1m")
        self.assertIn("d", proc.human_duration(200000))

    def test_bar_is_proportional_and_bounded(self):
        self.assertEqual(len(proc.bar(0.5, 10)), 10)
        self.assertEqual(proc.bar(0, 4), "░░░░")
        self.assertEqual(proc.bar(1, 4), "████")
        self.assertEqual(proc.bar(5.0, 4), "████")   # clamped
        self.assertEqual(proc.bar(-1.0, 4), "░░░░")
        self.assertEqual(proc.bar(0.5, 0), "")

    def test_proc_readers_return_data_and_never_raise(self):
        self.assertGreater(proc.uptime_seconds(), 0)
        self.assertEqual(len(proc.loadavg()), 3)
        self.assertIn("MemTotal", proc.meminfo())
        sample = proc.cpu_sample()
        self.assertGreater(sample.total, 0)
        self.assertGreaterEqual(proc.usage_since(sample, proc.cpu_sample()), 0.0)
        self.assertIsInstance(proc.thermal_zones(), list)
        self.assertIsInstance(proc.mounts(), list)
        self.assertIsInstance(proc.processes(limit=3), list)

    def test_missing_paths_degrade_instead_of_raising(self):
        self.assertEqual(proc._read("/nonexistent/path/here", "fallback"),
                         "fallback")
        self.assertEqual(proc.disk_usage("/nonexistent/path/here"), (0, 0, 0))
        self.assertEqual(proc.pressure("nonexistent"), {})

    def test_keymap_is_shared_not_per_tool(self):
        self.assertTrue(keymap.is_quit(ord("q")))
        self.assertTrue(keymap.is_quit(27))
        self.assertEqual(keymap.direction(ord("j")), 1)
        self.assertEqual(keymap.direction(ord("k")), -1)
        self.assertEqual(keymap.direction(ord("x")), 0)

    def test_theme_falls_back_without_a_kilix_checkout(self):
        from kilix_tui import theme
        self.assertEqual(theme.setting("KILIX_DEFINITELY_NOT_SET", "fallback"),
                         "fallback")


class SessionLogTests(unittest.TestCase):
    def test_both_tiers_are_listed_together(self):
        import tempfile
        module = load("session_log")
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "archive"))
            with open(os.path.join(tmp, "live1.log"), "w") as handle:
                handle.write("hello")
            with open(os.path.join(tmp, "archive/old1.log.zst"), "wb") as handle:
                handle.write(b"\x28\xb5\x2f\xfd")
            items = module.entries(tmp)
            tiers = {item["tier"] for item in items}
            self.assertEqual(tiers, {"live", "archived"})
            self.assertEqual({i["id"] for i in items}, {"live1", "old1"})


if __name__ == "__main__":
    unittest.main()
