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

from kilix_desk import desk, registry  # noqa: E402
from kilix_tui import app, keys as keymap, kitty_rc, privileged, theme  # noqa: E402


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
        desk.handle(ord("\t"), state)
        self.assertEqual(desk.SECTIONS[state.section], "Machine")
        desk.handle(ord("6"), state)
        self.assertEqual(desk.SECTIONS[state.section], "Power")


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
                if item.sibling is None:
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


class PanelTests(unittest.TestCase):
    def test_the_panel_look_is_assertable(self):
        with mock.patch.dict(os.environ, {"KILIX_PANEL": "1"}):
            theme.reset_panel_pairs()
            try:
                state = make_state()
                surface = app.TextSurface()
                desk.render(surface, state)
                self.assertIn("KILIX TUI", str(surface))
                self.assertTrue(surface.attr_shape().strip())
                text = str(surface)
                for section in desk.SECTIONS:
                    self.assertIn(section, text)
            finally:
                theme.reset_panel_pairs()

    def test_the_active_section_is_marked_in_text_not_only_colour(self):
        with mock.patch.dict(os.environ, {"KILIX_PANEL": "1"}):
            theme.reset_panel_pairs()
            try:
                state = make_state()
                state.section = 2
                text = app.render_to_text(desk.render, state)
                self.assertIn("▶Machine", text.replace(" ", ""))
            finally:
                theme.reset_panel_pairs()


if __name__ == "__main__":
    unittest.main()
