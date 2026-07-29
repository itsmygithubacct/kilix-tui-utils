"""The desktop keeps its charter: compose, degrade, confirm.

Four things are pinned here. Resolution follows the Start-menu discipline
(installed command first, sibling tool second, `kilix` subcommand third).
The page verb exists only inside Kilix and falls back to in-place everywhere
else. Power runs exactly the shared privileged argvs, and only after a
confirmation. And the whole thing renders headlessly at every size class,
because that is what makes it safe to make a session out of.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_desk import desk, graphics, gui, registry, tango  # noqa: E402
from kilix_tui import app, keys as keymap, kitty_rc, privileged  # noqa: E402


def load_entry():
    path = ROOT / "kilix-tui" / "main.py"
    spec = importlib.util.spec_from_file_location("desktop_entry", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_state(**kwargs):
    kwargs.setdefault("runner", lambda argv: 0)
    kwargs.setdefault("live", lambda: False)
    return desk.State(**kwargs)


class ContractTests(unittest.TestCase):
    def test_entry_point_imports_without_a_terminal(self):
        module = load_entry()
        self.assertTrue(callable(module.main))

    def test_renders_headlessly_at_every_size_class(self):
        state = make_state()
        for height, width in ((24, 80), (14, 60), (10, 40), (5, 20), (2, 6)):
            text = app.render_to_text(
                desk.render, state, height=height, width=width)
            self.assertIsInstance(text, str)

    def test_every_section_renders(self):
        state = make_state()
        for index in range(len(desk.SECTIONS)):
            state.section = index
            app.render_to_text(desk.render, state)

    def test_quit_key_exits(self):
        state = make_state()
        self.assertFalse(desk.handle(ord("q"), state))

    def test_quit_confirms_when_the_desktop_is_the_session(self):
        state = make_state()
        with mock.patch.dict(os.environ, {"KILIX_TUI_SESSION": "1"}):
            self.assertTrue(desk.handle(ord("q"), state))
            self.assertIsNotNone(state.confirm)
            self.assertFalse(desk.handle(ord("y"), state))
        with mock.patch.dict(os.environ, {"KILIX_TUI_SESSION": "1"}):
            desk.handle(ord("q"), state)
            self.assertTrue(desk.handle(ord("n"), state))
            self.assertIsNone(state.confirm)

    def test_sections_switch_by_digit_and_tab(self):
        state = make_state()
        desk.handle(ord("2"), state)
        self.assertEqual(desk.SECTIONS[state.section], "Programs")
        self.assertEqual(state.focus, "entries")
        desk.handle(ord("\t"), state)
        self.assertEqual(desk.SECTIONS[state.section], "Machine")
        desk.handle(ord("6"), state)
        self.assertEqual(desk.SECTIONS[state.section], "Power")


class NavigationTests(unittest.TestCase):
    def test_arrows_drive_the_focused_column(self):
        state = make_state()
        self.assertEqual(state.focus, "sections")
        desk.handle(258, state)                               # down
        self.assertEqual(desk.SECTIONS[state.section], "Programs")
        desk.handle(261, state)                               # right: dive in
        self.assertEqual(state.focus, "entries")
        desk.handle(258, state)
        self.assertEqual(state.selected, 1)
        desk.handle(260, state)                               # left: back out
        self.assertEqual(state.focus, "sections")

    def test_escape_walks_out_one_level_at_a_time(self):
        state = make_state()
        desk.handle(ord("3"), state)                          # Machine, entries
        self.assertTrue(desk.handle(27, state))               # -> Home
        self.assertEqual(state.section, 0)
        self.assertEqual(state.focus, "sections")
        self.assertFalse(desk.handle(27, state))              # -> quit

    def test_home_and_end_jump_within_the_list(self):
        state = make_state()
        desk.handle(ord("3"), state)
        desk.handle(360, state)                               # end
        self.assertEqual(state.selected, len(state.entries()) - 1)
        desk.handle(262, state)                               # home
        self.assertEqual(state.selected, 0)

    def test_selection_stays_visible_when_the_list_scrolls(self):
        self.assertEqual(desk.visible_window(10, 4, 0), 0)
        self.assertEqual(desk.visible_window(10, 4, 3), 0)
        self.assertEqual(desk.visible_window(10, 4, 7), 4)
        self.assertEqual(desk.visible_window(10, 4, 9), 6)
        self.assertEqual(desk.visible_window(3, 8, 2), 0)


class SubmenuTests(unittest.TestCase):
    def test_games_drilldown_lists_and_flips_toggles(self):
        quiet_calls = []
        state = make_state(quiet=lambda argv: quiet_calls.append(argv) or 0)
        games = [("kilix-pong", "Kilix Pong", True),
                 ("doom", "Doom", False)]
        with mock.patch.object(registry, "games", return_value=games), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state.section = desk.SECTIONS.index("Programs")
            state.focus = "entries"
            entries = state.entries()
            index = next(i for i, e in enumerate(entries)
                         if e.submenu == "games")
            state.selected = index
            desk.handle(10, state)                            # descend
            self.assertEqual(state.submenu, "games")
            listed = state.entries()
            self.assertEqual([e.label for e in listed],
                             ["Kilix Pong", "Doom"])
            self.assertEqual([e.hint for e in listed], ["on", "off"])
            desk.handle(10, state)                            # flip Kilix Pong
            self.assertEqual(
                quiet_calls,
                [("/opt/kilix/kilix", "games", "disable", "kilix-pong")])
            self.assertTrue(desk.handle(27, state))           # Esc pops
            self.assertIsNone(state.submenu)

    def test_submenus_degrade_without_a_kilix_checkout(self):
        state = make_state()
        state.submenu = "games"
        with mock.patch.object(registry, "games", return_value=None):
            entries = state.entries()
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].argv)


class TextMouseTests(unittest.TestCase):
    def test_render_records_the_hit_map(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Machine")
        state.focus = "entries"
        app.render_to_text(desk.render, state)
        self.assertTrue(state.text_hits["sections"])
        self.assertEqual(state.text_hits["bar_row"], 1)
        self.assertGreater(state.text_hits["visible"], 0)

    def test_mouse_key_is_safe_without_curses(self):
        state = make_state()
        self.assertTrue(desk.handle(desk.KEY_MOUSE, state))


class ResolutionTests(unittest.TestCase):
    def test_installed_command_wins_over_the_sibling_checkout(self):
        item = registry.Item("x", command="kilix-calculator",
                             sibling="calculator")
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/kilix-calculator"):
            plan = registry.resolve(item)
        self.assertEqual(plan.argv, ("/usr/bin/kilix-calculator",))

    def test_sibling_tool_backs_up_a_missing_install(self):
        item = registry.Item("x", command="kilix-calculator",
                             sibling="calculator")
        with mock.patch.object(registry.shutil, "which", return_value=None):
            plan = registry.resolve(item)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.argv[1].endswith("tools/calculator/main.py"))

    def test_temperatures_resolve_from_the_unified_checkout(self):
        item = next(
            item for item in registry.MACHINE if item.label == "Temperatures"
        )
        with mock.patch.object(registry.shutil, "which", return_value=None):
            plan = registry.resolve(item)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.argv[1].endswith("tools/temps/main.py"))

    def test_kilix_subcommand_is_the_last_resort(self):
        item = registry.Item("x", command="kilix-bonsai", kilix=("bonsai",))
        with mock.patch.object(registry.shutil, "which", return_value=None), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            plan = registry.resolve(item)
        self.assertEqual(plan.argv, ("/opt/kilix/kilix", "bonsai"))

    def test_unresolvable_items_carry_a_reason_not_a_crash(self):
        item = registry.Item("x", command="kilix-bonsai", kilix=("bonsai",))
        with mock.patch.object(registry.shutil, "which", return_value=None), \
             mock.patch.object(registry, "kilix_command", return_value=None):
            self.assertIsNone(registry.resolve(item))
        self.assertTrue(registry.disabled_reason(item))

    def test_source_checkout_never_wins_for_non_sibling_tools(self):
        # The Start-menu rule: a working tree is not the pinned closure. Only
        # `command` and `kilix` branches exist for outside tools — assert the
        # registry offers no path-based resolution for them.
        for section in registry.SECTIONS.values():
            for item in section:
                if item.sibling is None and not item.submenu:
                    self.assertTrue(item.command or item.kilix)


class VerbTests(unittest.TestCase):
    def test_kilix_only_items_hide_outside_kilix(self):
        state = make_state(live=lambda: False)
        state.section = desk.SECTIONS.index("Session")
        labels = [entry.label for entry in state.entries()]
        self.assertNotIn("Switcher", labels)
        self.assertNotIn("PTY sessions", labels)

    def test_tab_verb_degrades_to_inplace_outside_kilix(self):
        state = make_state(live=lambda: False)
        state.section = desk.SECTIONS.index("Machine")
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/tool"):
            verbs = {entry.label: entry.verb for entry in state.entries()}
        self.assertEqual(verbs["Temperatures"], "inplace")

    def test_tab_verb_survives_inside_kilix(self):
        state = make_state(live=lambda: True)
        state.section = desk.SECTIONS.index("Machine")
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/tool"):
            verbs = {entry.label: entry.verb for entry in state.entries()}
        self.assertEqual(verbs["Temperatures"], "tab")

    def test_tab_launch_falls_back_in_place_when_refused(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0,
                           live=lambda: True)
        entry = desk.Entry("Temps", ("/usr/bin/tool",), verb="tab")
        with mock.patch.object(kitty_rc, "launch_tab",
                               side_effect=kitty_rc.Unavailable("refused")):
            desk._open(state, entry)
        self.assertEqual(calls, [("/usr/bin/tool",)])

    def test_disabled_entry_reports_its_reason(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        entry = desk.Entry("x", None, reason="not installed")
        desk._open(state, entry)
        self.assertEqual(state.message, "not installed")


class PowerTests(unittest.TestCase):
    def test_the_exact_privileged_argvs(self):
        argvs = {tuple(argv) for _label, argv, _c in privileged.power_actions()}
        self.assertIn(("systemctl", "reboot"), argvs)
        self.assertIn(("systemctl", "poweroff"), argvs)
        self.assertTrue(any(argv[:2] == ("loginctl", "terminate-session")
                            for argv in argvs))

    def test_every_power_entry_confirms(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Power")
        self.assertTrue(state.entries())
        for entry in state.entries():
            self.assertTrue(entry.confirm)

    def test_nothing_runs_on_a_single_keypress(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        state.section = desk.SECTIONS.index("Power")
        state.focus = "entries"
        state.selected = 1                                    # Reboot
        desk.handle(list(keymap.SELECT)[0], state)
        self.assertEqual(calls, [])
        self.assertIsNotNone(state.confirm)
        desk.handle(ord("x"), state)                          # cancel
        self.assertEqual(calls, [])
        desk.handle(list(keymap.SELECT)[0], state)
        desk.handle(ord("y"), state)                          # confirm
        self.assertEqual(calls, [("systemctl", "reboot")])

    def test_the_control_tui_shares_the_same_list(self):
        path = ROOT / "tools" / "plebian_control" / "main.py"
        spec = importlib.util.spec_from_file_location("control_for_desk", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        control = module.State()
        control.section = module.SECTIONS.index("Power")
        self.assertEqual(control.actions(), privileged.power_actions())


class LaunchTabTests(unittest.TestCase):
    def test_command_construction(self):
        with mock.patch.object(kitty_rc, "_run", return_value="42\n") as run:
            pane = kitty_rc.launch_tab(["kilix-temps"], title="Temperatures")
        self.assertEqual(pane, 42)
        run.assert_called_once_with([
            "launch", "--type=tab", "--tab-title", "Temperatures",
            "--keep-focus", "--", "kilix-temps",
        ])

    def test_follow_focus_and_cwd(self):
        with mock.patch.object(kitty_rc, "_run", return_value="") as run:
            pane = kitty_rc.launch_tab(
                ["x"], title="t", cwd="/tmp", keep_focus=False)
        self.assertEqual(pane, 0)
        self.assertIn("--cwd=/tmp", run.call_args[0][0])
        self.assertNotIn("--keep-focus", run.call_args[0][0])


class TangoTextTests(unittest.TestCase):
    def test_the_text_layout_is_assertable(self):
        tango.reset()
        state = make_state()
        surface = app.TextSurface()
        desk.render(surface, state)
        text = str(surface)
        self.assertIn("KILIX TUI", text)
        for section in desk.SECTIONS:
            self.assertIn(section, text)
        # Headless attributes are synthetic but distinct, so the layout's
        # styling is a picture, not a guess.
        self.assertTrue(surface.attr_shape().strip())

    def test_the_active_section_is_marked_in_text_not_only_colour(self):
        state = make_state()
        state.section = 2
        text = app.render_to_text(desk.render, state)
        self.assertIn("▶3Machine", text.replace(" ", ""))

    def test_confirm_shows_the_exact_command(self):
        state = make_state()
        state.confirm = ("Shut down", ("systemctl", "poweroff"))
        text = app.render_to_text(desk.render, state)
        self.assertIn("Confirm: Shut down", text)
        self.assertIn("$ systemctl poweroff", text)


class StubCanvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.texts: list[str] = []

    def fill_rect(self, *args, **kwargs):
        pass

    def fill_circle(self, *args, **kwargs):
        pass

    def text(self, x, y, value, color, scale=1):
        self.texts.append(value)

    def text_shadow(self, x, y, value, color, scale=1):
        self.texts.append(value)

    def rgb_bytes(self):
        return b"\0" * (self.width * self.height * 3)

    def close(self):
        pass


class GraphicsTests(unittest.TestCase):
    def test_kitty_graphics_likely_reads_the_environment(self):
        self.assertTrue(graphics.kitty_graphics_likely({"KITTY_WINDOW_ID": "4"}))
        self.assertTrue(graphics.kitty_graphics_likely({"TERM": "xterm-kitty"}))
        self.assertFalse(graphics.kitty_graphics_likely({"TERM": "xterm"}))

    def test_fonts_step_in_integer_scales(self):
        self.assertEqual(graphics.font_for(11).scale, 1)
        self.assertEqual(graphics.font_for(26).scale, 2)
        self.assertEqual(graphics.font_for(44).scale, 3)

    def test_key_sequences_speak_the_shared_keymap(self):
        self.assertIn(gui._SEQUENCES[b"\x1b[A"], keymap.UP)
        self.assertIn(gui._SEQUENCES[b"\x1b[B"], keymap.DOWN)
        self.assertIn(gui._SEQUENCES[b"\x1b[5~"], keymap.PAGE_UP)
        self.assertIn(gui._SEQUENCES[b"\x1b[6~"], keymap.PAGE_DOWN)

    def test_renderer_draws_every_screen_headlessly(self):
        captured: list[StubCanvas] = []

        def factory(width, height):
            canvas = StubCanvas(width, height)
            captured.append(canvas)
            return canvas

        renderer = graphics.DesktopRenderer(canvas_factory=factory)
        state = make_state()
        for section in range(len(desk.SECTIONS)):
            state.section = section
            frame = renderer.render(state, 100, 30, (960, 560),
                                    clock="12:00")
            self.assertEqual(len(frame.rgb), 960 * 560 * 3)
        drawn = " ".join(text for canvas in captured for text in canvas.texts)
        self.assertIn("KILIX TUI", drawn)
        for section in desk.SECTIONS:
            self.assertIn(section, drawn)

    def test_confirm_overlay_names_the_command(self):
        canvases: list[StubCanvas] = []

        def factory(width, height):
            canvas = StubCanvas(width, height)
            canvases.append(canvas)
            return canvas

        renderer = graphics.DesktopRenderer(canvas_factory=factory)
        state = make_state()
        state.section = desk.SECTIONS.index("Power")
        state.confirm = ("Shut down", ("systemctl", "poweroff"))
        renderer.render(state, 100, 30, (960, 560), clock="12:00")
        drawn = " ".join(text for canvas in canvases for text in canvas.texts)
        self.assertIn("Confirm: Shut down", drawn)
        self.assertIn("$ systemctl poweroff", drawn)

    def test_renderer_records_hits_for_the_mouse(self):
        renderer = graphics.DesktopRenderer(
            canvas_factory=lambda w, h: StubCanvas(w, h))
        state = make_state()
        state.section = desk.SECTIONS.index("Machine")
        renderer.render(state, 100, 30, (960, 560), clock="12:00")
        kinds = {kind for kind, _i, _box in renderer.hits}
        self.assertEqual(kinds, {"section", "entry"})
        sections = [i for kind, i, _box in renderer.hits
                    if kind == "section"]
        self.assertEqual(sections, list(range(len(desk.SECTIONS))))


class PixelMouseTests(unittest.TestCase):
    def _desktop(self):
        desktop = object.__new__(gui.GraphicalDesktop)
        desktop.state = make_state()
        desktop.renderer = mock.Mock(hits=[])
        desktop.running = True
        desktop.redraw = False
        desktop._cells = (100, 30)
        desktop._render_px = (1000, 600)
        desktop._raw_px = (2000, 1200)
        desktop._pending = b""
        return desktop

    def test_sgr_reports_parse_in_pixels_and_cells(self):
        desktop = self._desktop()
        # 1016 pixel coordinates scale by the raw-to-render ratio…
        self.assertEqual(desktop._to_render(1000, 600), (500, 300))
        # …and plain 1006 cell coordinates map through the cell grid.
        self.assertEqual(desktop._to_render(50, 15), (495, 290))

    def test_click_selects_then_opens(self):
        desktop = self._desktop()
        opened = []
        desktop.state.runner = lambda argv: opened.append(tuple(argv)) or 0
        desktop.renderer.hits = [
            ("section", 2, (0, 100, 200, 140)),
            ("entry", 1, (220, 100, 900, 140)),
        ]
        with mock.patch.object(gui, "handle",
                               wraps=desk.handle) as wrapped:
            desktop._handle_bytes(b"\x1b[<0;600;240M")        # pixel coords
            self.assertEqual(desktop.state.focus, "entries")
            self.assertEqual(desktop.state.selected, 1)
            desktop._handle_bytes(b"\x1b[<0;600;240M")        # second click
            wrapped.assert_any_call(10, desktop.state)

    def test_wheel_moves_the_selection(self):
        desktop = self._desktop()
        desktop.state.section = desk.SECTIONS.index("Power")
        desktop.state.focus = "entries"
        desktop.state.selected = 0
        desktop._handle_bytes(b"\x1b[<65;10;10M")             # wheel down
        self.assertEqual(desktop.state.selected, 1)

    def test_split_mouse_reports_are_buffered(self):
        desktop = self._desktop()
        desktop._handle_bytes(b"\x1b[<0;60")
        self.assertEqual(desktop._pending, b"\x1b[<0;60")
        desktop.renderer.hits = [("entry", 0, (0, 0, 1000, 600))]
        desktop._handle_bytes(b"0;240M")
        self.assertEqual(desktop._pending, b"")
        self.assertEqual(desktop.state.focus, "entries")

    def test_clicks_are_ignored_while_a_confirmation_is_open(self):
        desktop = self._desktop()
        desktop.state.confirm = ("Shut down", ("systemctl", "poweroff"))
        desktop.renderer.hits = [("entry", 0, (0, 0, 1000, 600))]
        desktop._handle_bytes(b"\x1b[<0;500;300M")
        self.assertIsNotNone(desktop.state.confirm)


class GraphicsBackendTests(unittest.TestCase):
    @unittest.skipUnless(graphics.available()[0],
                         "soft-raster / presenter not available")
    def test_real_backend_produces_full_frames(self):
        renderer = graphics.DesktopRenderer()
        state = make_state()
        frame = renderer.render(state, 80, 24, (640, 360), clock="12:00")
        self.assertEqual(len(frame.rgb), 640 * 360 * 3)
        self.assertNotEqual(frame.rgb.count(b"\0"), len(frame.rgb))


if __name__ == "__main__":
    unittest.main()
