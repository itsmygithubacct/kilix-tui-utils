"""Every shared-core text tool renders, handles input, and stays in bounds.

These are contract tests rather than deep per-tool tests: each tool must import
without a terminal, render headlessly at an awkward size without raising, clip
to its surface, and quit when told. That is what makes a tool safe to put behind
a desktop menu, and it is the check most likely to catch a regression in the
shared core. The framebuffer memory and temperature dashboards have dedicated
suites because their renderer and event-loop contracts are deliberately richer.
"""
import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402

TOOLS = [
    "calculator", "cpu", "disk", "system", "volume",
    "file", "launcher", "package", "session_log", "weather", "music",
    "plebian_control", "rollout_resume", "switcher", "cameras",
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

    def test_every_tool_uses_the_virtualbox_manager_shell(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                module = load(name)
                frame = app.render_to_text(
                    module.render, make_state(module),
                    height=24, width=100,
                )
                lines = frame.splitlines()
                self.assertIn("KILIX TUI", lines[0])
                self.assertIn("▶1", lines[1])
                self.assertTrue(lines[2].startswith("─"))
                self.assertNotIn(" // ", frame)

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

    def test_installer_refuses_a_symlinked_command_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prefix = base / "prefix"
            prefix.mkdir()
            target = base / "unrelated"
            target.mkdir()
            (prefix / "bin").symlink_to(target, target_is_directory=True)
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh")],
                env=dict(
                    os.environ,
                    HOME=str(base / "home"),
                    KILIX_TUI_UTILS_PREFIX=str(prefix),
                ),
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlinked install directory", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    def test_installer_uses_exclusive_random_temporary_launchers(self):
        source = (ROOT / "install.sh").read_text()
        self.assertIn('mktemp "$BIN/.${command_name}.XXXXXX"', source)
        self.assertNotIn(".tmp.$", source)

    def test_installer_replaces_launcher_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prefix = base / "prefix"
            bin_dir = prefix / "bin"
            unrelated = base / "unrelated"
            bin_dir.mkdir(parents=True)
            unrelated.mkdir()
            (bin_dir / "kilix-calculator").symlink_to(
                unrelated, target_is_directory=True,
            )
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh")],
                env=dict(
                    os.environ,
                    HOME=str(base / "home"),
                    KILIX_HOME=str(base / "missing-kilix"),
                    KILIX_TUI_UTILS_PREFIX=str(prefix),
                ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = bin_dir / "kilix-calculator"
            self.assertFalse(launcher.is_symlink())
            self.assertTrue(launcher.is_file())
            self.assertEqual(list(unrelated.iterdir()), [])

    def test_installed_launcher_quotes_the_checkout_path_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout = base / "repo $KILIX_TEST_EXPANSION ' quote"
            tool = checkout / "tools" / "calculator"
            tool.mkdir(parents=True)
            shutil.copy2(ROOT / "install.sh", checkout / "install.sh")
            (tool / "main.py").write_text(
                "import sys\nprint('OK:' + '|'.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            prefix = base / "prefix"
            installed = subprocess.run(
                ["bash", str(checkout / "install.sh")],
                env=dict(
                    os.environ,
                    HOME=str(base / "home"),
                    KILIX_HOME=str(base / "missing-kilix"),
                    KILIX_TUI_UTILS_PREFIX=str(prefix),
                ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            launched = subprocess.run(
                [str(prefix / "bin" / "kilix-calculator"),
                 "a b", "$literal"],
                env=dict(os.environ, KILIX_TEST_EXPANSION="wrong"),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertEqual(launched.stdout.strip(), "OK:a b|$literal")


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

    def test_malformed_proc_values_degrade_instead_of_raising(self):
        for malformed in ("not-a-number\n", "nan\n", "inf\n", "-1\n"):
            with self.subTest(malformed=malformed), \
                 mock.patch.object(proc, "_read", return_value=malformed):
                self.assertEqual(proc.uptime_seconds(), 0.0)
        with mock.patch.object(proc, "_read", return_value="model name\n"):
            self.assertEqual(proc.cpu_model(), "unknown")

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

    def test_theme_loads_the_selected_sdk_without_poisoning_sys_path(self):
        from kilix_tui import theme
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "kilix"
            package = home / "config" / "kilix_sdk"
            package.mkdir(parents=True)
            (package / "settings.py").write_text(
                "def load():\n    return {'KILIX_TEST_VALUE': 'selected'}\n",
                encoding="utf-8",
            )
            poisoned_package = types.ModuleType("kilix_sdk")
            poisoned_settings = types.ModuleType("kilix_sdk.settings")
            poisoned_settings.load = lambda: {"KILIX_TEST_VALUE": "poisoned"}
            previous_sdk = theme._SDK
            before_path = list(sys.path)
            private_before = {
                name for name in sys.modules
                if name.startswith("_kilix_tui_host_settings_")
            }
            try:
                theme._SDK = None
                with mock.patch.dict(
                    sys.modules,
                    {
                        "kilix_sdk": poisoned_package,
                        "kilix_sdk.settings": poisoned_settings,
                    },
                ), mock.patch.dict(
                    os.environ,
                    {"KILIX_HOME": str(home)},
                    clear=False,
                ):
                    self.assertEqual(
                        theme.setting("KILIX_TEST_VALUE", "fallback"),
                        "selected",
                    )
                self.assertEqual(sys.path, before_path)
            finally:
                theme._SDK = previous_sdk
                for name in list(sys.modules):
                    if (name.startswith("_kilix_tui_host_settings_")
                            and name not in private_before):
                        sys.modules.pop(name, None)

    def test_only_the_main_tango_renderer_remains(self):
        self.assertFalse((ROOT / "src/kilix_tui/panel.py").exists())
        self.assertFalse((ROOT / "src/kilix_tui/chrome.py").exists())
        source = (ROOT / "src/kilix_tui/theme.py").read_text()
        for removed in ("KILIX_PANEL", "PANEL_RGB", "panel_attr"):
            self.assertNotIn(removed, source)


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


class FilterTests(unittest.TestCase):
    """`/` behaves the same in every list that has it."""

    def test_typing_narrows_and_escape_restores(self):
        f = shell.Filter()
        rows = ["alpha", "beta", "gamma"]
        self.assertEqual(f.apply(rows), rows)
        f.open()
        for letter in "am":
            f.handle(ord(letter))
        self.assertEqual(f.apply(rows), ["gamma"])
        f.handle(27)
        self.assertEqual(f.apply(rows), rows)
        self.assertFalse(f.active())

    def test_enter_keeps_the_needle_and_returns_to_navigating(self):
        f = shell.Filter()
        f.open()
        f.handle(ord("a"))
        f.handle(ord("\n"))
        self.assertFalse(f.typing)        # keys drive the list again
        self.assertTrue(f.active())       # but the needle still applies
        self.assertEqual(f.text, "a")

    def test_the_filter_consumes_keys_only_while_open(self):
        f = shell.Filter()
        self.assertFalse(f.handle(ord("q")))   # q must still quit
        self.assertTrue(f.handle(ord("/")))    # `/` opens it
        self.assertTrue(f.handle(ord("q")))    # now q is text
        self.assertEqual(f.text, "q")

    def test_a_filtered_playlist_plays_the_right_track(self):
        # The backend addresses tracks by playlist position, so a filtered
        # view has to carry the original index or Enter plays the wrong song.
        music = load_tool("music")
        state = music.State.__new__(music.State)
        state.playlist = ["/m/aaa.flac", "/m/target.flac", "/m/ccc.flac"]
        state.filter = shell.Filter()
        state.filter.open()
        for letter in "target":
            state.filter.handle(ord(letter))
        view = state.view()
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0][0], 1)        # original index, not 0
        self.assertTrue(view[0][1].endswith("target.flac"))

    def test_every_list_tool_offers_the_filter(self):
        for name in ("file", "session_log", "music"):
            with self.subTest(tool=name):
                source = (ROOT / "tools" / name / "main.py").read_text()
                self.assertIn("shell.Filter()", source)
                self.assertIn("/ filter", source)


def load_tool(name):
    return load(name)


class SharedShellTests(unittest.TestCase):
    """What every tool gets from the frame and the loop, not one at a time."""

    def test_the_key_line_drops_the_middle_and_keeps_the_way_out(self):
        line = "↑/↓ select · Enter scan · r refresh · q quit"
        for width in range(12, len(line) + 5):
            fitted = shell.fit(line, width)
            self.assertLessEqual(len(fitted), width, f"width {width}")
            self.assertTrue(fitted.endswith("q quit"), f"width {width}")

    def test_help_is_advertised_only_where_it_works(self):
        surface = app.TextSurface(height=12, width=70)
        shell.draw(surface, title="Disk", footer="r refresh · q quit")
        self.assertIn("? keys", str(surface))
        surface = app.TextSurface(height=12, width=70)
        shell.draw(surface, title="Calculator", footer="q quit",
                   help_key=False)
        self.assertNotIn("? keys", str(surface))

    def test_the_help_key_never_displaces_the_way_out(self):
        """`? keys` goes before the tool's last binding, not after it.

        Appending it pushed "q quit" out of the one position `fit` protects,
        and the trimmer then dropped quitting while keeping the help key — on
        the widest tool in the suite, at ordinary terminal widths.
        """
        footer = ("Enter resume · x tmux · A attach · Space mark · R restore "
                  "· / filter · y yolo · q quit")
        for width in (95, 70, 50, 34, 20):
            surface = app.TextSurface(height=12, width=width)
            shell.draw(surface, title="Rollout Resume", footer=footer)
            last = str(surface).splitlines()[-1]
            self.assertIn("quit", last, f"width {width}: {last!r}")
            self.assertLessEqual(len(last), width)

    def test_the_desktop_home_place_shows_its_way_back(self):
        """Home is a place, so the row that leaves it must be on screen."""
        from kilix_desk import desk
        state = desk.State(live=lambda: False)
        state.path = ["Home"]
        text = app.render_to_text(desk.render, state, height=18, width=76)
        body = "\n".join(text.splitlines()[4:])
        self.assertIn("..", body,
                      "the cursor sat on a back row nothing drew")

    def test_the_overlay_is_built_from_the_tool_own_key_line(self):
        surface = app.TextSurface(height=20, width=76)
        shell.draw(surface, title="Disk",
                   footer="↑/↓ select · Enter scan · r refresh · q quit")
        app.help_overlay(surface)
        text = str(surface)
        self.assertIn("Disk — keys", text)
        self.assertIn("scan", text)
        # Only what is true here: no section keys in a tool without sections.
        self.assertNotIn("jump straight to a section", text)
        self.assertNotIn("next section", text)

    def test_the_overlay_mentions_the_mouse_only_when_enabled(self):
        surface = app.TextSurface(height=20, width=76)
        shell.draw(surface, title="Disk", footer="q quit")
        app.help_overlay(surface, mouse=False)
        self.assertNotIn("wheel to scroll", str(surface))
        surface = app.TextSurface(height=20, width=76)
        shell.draw(surface, title="Disk", footer="q quit")
        app.help_overlay(surface, mouse=True)
        self.assertIn("wheel to scroll", str(surface))

    def test_ctrl_h_is_not_swallowed_as_help(self):
        # Ctrl-H arrives as 8, the same code as Backspace, which the file
        # manager uses for "go up". The shared loop must only take "?".
        self.assertTrue(keymap.is_help_char(ord("?")))
        self.assertFalse(keymap.is_help_char(8))

    def test_cursor_visibility_failure_does_not_abort_the_event_loop(self):
        class Screen:
            def keypad(self, _enabled):
                pass

            def erase(self):
                pass

            def refresh(self):
                pass

            def getch(self):
                return ord("q")

        def wrapper(loop):
            return loop(Screen())

        with mock.patch.object(app.curses, "wrapper", side_effect=wrapper), \
             mock.patch.object(
                 app.curses, "curs_set", side_effect=app.curses.error,
             ):
            self.assertEqual(app.run(lambda _surface, _state: None, None), 0)

    def test_every_tool_title_that_draws_a_frame_has_a_tip(self):
        titles = set()
        for path in sorted(glob.glob(os.path.join(ROOT, "tools", "*",
                                                  "main.py"))):
            with open(path, encoding="utf-8") as handle:
                titles.update(re.findall(r'title="([^"]+)"', handle.read()))
        missing = sorted(t for t in titles if t not in shell.TIPS)
        self.assertEqual(missing, [], f"tools without a tip: {missing}")


if __name__ == "__main__":
    unittest.main()
