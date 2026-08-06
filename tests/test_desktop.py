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
import shutil
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
    def test_one_cursor_walks_in_and_out_of_places(self):
        # Up/Down must mean the same thing everywhere: move the cursor in the
        # list on screen. Right walks into a place, Left walks back out.
        state = make_state()
        self.assertEqual([e.label for e in state.entries()],
                         list(desk.SECTIONS))
        desk.handle(258, state)                               # down
        self.assertEqual(state.entries()[state.selected].label, "Programs")
        desk.handle(261, state)                               # right: walk in
        self.assertEqual(state.path, ["Programs"])
        self.assertEqual(state.entries()[0].label, desk.BACK_LABEL)
        desk.handle(258, state)                               # down, same key
        self.assertEqual(state.selected, 1)
        desk.handle(260, state)                               # left: walk out
        self.assertEqual(state.path, [])

    def test_walking_out_lands_on_the_place_just_left(self):
        state = make_state()
        desk.handle(ord("4"), state)                          # System
        desk.handle(260, state)                               # back to root
        self.assertEqual(state.entries()[state.selected].label, "System")

    def test_the_back_row_is_reachable_by_cursor(self):
        # ncdu's "/.." — going back must be in the list, not only on a key.
        state = make_state()
        desk.handle(ord("3"), state)
        state.selected = 0
        self.assertTrue(state.entries()[0].back)
        desk.handle(10, state)                                # Enter on ".."
        self.assertEqual(state.path, [])

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


class DefaultDesktopPlaceTests(unittest.TestCase):
    """Choosing the desktop every later session starts with."""

    def _state(self):
        state = make_state()
        state.path = ["System", "Default desktop"]
        return state

    def test_the_choices_are_offered_with_the_current_one_marked(self):
        with mock.patch.object(registry, "default_desktop", return_value="tui"), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = [e for e in self._state().entries() if not e.back]
        names = [e.label for e in rows]
        self.assertIn("tui", names)
        self.assertIn("auto", names)
        current = next(e for e in rows if e.label == "tui")
        self.assertEqual(current.hint, "current")

    def test_choosing_one_goes_through_the_launcher(self):
        with mock.patch.object(registry, "default_desktop", return_value="auto"), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = self._state().entries()
            cap = next(e for e in rows if e.label == "cap")
        self.assertEqual(cap.argv,
                         ("/opt/kilix/kilix", "default-desktop", "set", "cap"))

    def test_it_degrades_without_a_checkout(self):
        with mock.patch.object(registry, "kilix_command", return_value=None):
            rows = [e for e in self._state().entries() if not e.back]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)


class SoftwarePlaceTests(unittest.TestCase):
    """Installing is a place, and it keeps no catalogue of its own."""

    ROWS = [
        {"id": "claude", "label": "Claude Code", "kind": "agent",
         "installed": True},
        {"id": "doom", "label": "Doom", "kind": "game", "installed": False},
    ]

    def _state(self):
        state = make_state()
        state.path = ["Programs", "Software"]
        return state

    def test_the_list_comes_from_the_launcher_not_from_here(self):
        with mock.patch.object(registry, "installable", return_value=self.ROWS), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            labels = [e.label for e in self._state().entries()]
        self.assertEqual(labels, [desk.BACK_LABEL, "Claude Code", "Doom"])

    def test_enter_installs_through_that_same_command(self):
        with mock.patch.object(registry, "installable", return_value=self.ROWS), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = self._state().entries()
            doom = next(e for e in rows if e.label == "Doom")
        self.assertEqual(doom.argv, ("/opt/kilix/kilix", "install", "doom"))

    def test_installed_entries_stay_selectable(self):
        """Re-running an install is how a pinned thing returns to its pin."""
        with mock.patch.object(registry, "installable", return_value=self.ROWS), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = self._state().entries()
            claude = next(e for e in rows if e.label == "Claude Code")
        self.assertEqual(claude.hint, "installed")
        self.assertIsNotNone(claude.argv)

    def test_the_launcher_is_asked_once_per_visit_not_once_per_frame(self):
        """`entries()` runs on every keystroke; shelling out there would crawl."""
        with mock.patch.object(registry, "installable",
                               return_value=self.ROWS) as ask, \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state = self._state()
            for _ in range(20):
                state.entries()
            self.assertEqual(ask.call_count, 1)
            desk.handle(ord("r"), state)
            state.entries()
            self.assertEqual(ask.call_count, 2, "r must re-ask")

    def test_it_degrades_to_an_explanation_without_a_checkout(self):
        with mock.patch.object(registry, "installable", return_value=None):
            rows = [e for e in self._state().entries() if not e.back]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)


class SubmenuTests(unittest.TestCase):
    def test_games_drilldown_lists_and_flips_toggles(self):
        # An older launcher without `games play`: Enter stays the toggle so
        # the list is never a dead end.
        quiet_calls = []
        state = make_state(quiet=lambda argv: quiet_calls.append(argv) or 0)
        games = [("kilix-pong", "Kilix Pong", True),
                 ("doom", "Doom", False)]
        with mock.patch.object(registry, "games", return_value=games), \
             mock.patch.object(registry, "games_play_supported",
                               return_value=False), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state.section = desk.SECTIONS.index("Programs")
            entries = state.entries()
            index = next(i for i, e in enumerate(entries)
                         if e.submenu == "games")
            state.selected = index
            desk.handle(10, state)                            # descend
            self.assertEqual(state.submenu, "games")
            listed = state.entries()
            self.assertEqual([e.label for e in listed],
                             [desk.BACK_LABEL, "Kilix Pong", "Doom"])
            self.assertEqual([e.hint for e in listed[1:]], ["on", "off"])
            state.selected = 1                                # past ".."
            desk.handle(10, state)                            # flip Kilix Pong
            self.assertEqual(
                quiet_calls,
                [("/opt/kilix/kilix", "games", "disable", "kilix-pong")])
            self.assertTrue(desk.handle(27, state))           # Esc pops
            self.assertIsNone(state.submenu)

    def test_games_launch_when_the_host_knows_play(self):
        # A launcher that advertises `play`: Enter starts the game and `t`
        # keeps the availability toggle one key away.
        run_calls = []
        quiet_calls = []
        state = make_state(runner=lambda argv: run_calls.append(argv) or 0,
                           quiet=lambda argv: quiet_calls.append(argv) or 0)
        games = [("kilix-pong", "Kilix Pong", True)]
        with mock.patch.object(registry, "games", return_value=games), \
             mock.patch.object(registry, "games_play_supported",
                               return_value=True), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]), \
             mock.patch.object(desk, "_resolve_program", lambda name: name):
            state.submenu = "games"
            listed = state.entries()
            self.assertEqual([e.hint for e in listed[1:]], ["on"])
            state.selected = 1
            desk.handle(10, state)                            # Enter plays
            self.assertEqual(
                run_calls,
                [("/opt/kilix/kilix", "games", "play", "kilix-pong")])
            desk.handle(ord("t"), state)                      # t still flips
            self.assertEqual(
                quiet_calls,
                [("/opt/kilix/kilix", "games", "disable", "kilix-pong")])

    def test_the_play_probe_is_asked_once_per_visit(self):
        state = make_state()
        probes = []
        with mock.patch.object(registry, "games",
                               return_value=[("doom", "Doom", True)]), \
             mock.patch.object(registry, "games_play_supported",
                               side_effect=lambda k: probes.append(k) or True), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state.submenu = "games"
            state.entries()
            state.entries()
            state.entries()
        self.assertEqual(len(probes), 1)

    def test_submenus_degrade_without_a_kilix_checkout(self):
        state = make_state()
        state.submenu = "games"
        with mock.patch.object(registry, "games", return_value=None):
            entries = [entry for entry in state.entries() if not entry.back]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].argv)


class RunCommandTests(unittest.TestCase):
    def test_bang_opens_the_prompt_and_enter_runs_the_argv(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        desk.handle(ord("!"), state)
        self.assertTrue(state.running_prompt)
        for ch in "echo hi":
            desk.handle(ord(ch), state)
        desk.handle(10, state)
        # The program is resolved to an absolute path in the desk's own
        # environment, so the spawn does not depend on the terminal's PATH.
        self.assertEqual(calls, [(shutil.which("echo"), "hi")])
        self.assertFalse(state.running_prompt)

    def test_the_programs_row_opens_the_same_prompt(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Programs")
        entries = state.entries()
        index = next(i for i, e in enumerate(entries) if e.prompt)
        state.selected = index
        desk.handle(10, state)
        self.assertTrue(state.running_prompt)

    def test_quoting_splits_like_a_shell_but_never_uses_one(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        desk.handle(ord("!"), state)
        for ch in 'printf "two words"':
            desk.handle(ord(ch), state)
        desk.handle(10, state)
        self.assertEqual(calls, [(shutil.which("printf"), "two words")])

    def test_shell_operators_are_refused_with_a_reason(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        desk.handle(ord("!"), state)
        for ch in "ls | wc":
            desk.handle(ord(ch), state)
        desk.handle(10, state)
        self.assertIn("pipes", state.message)
        self.assertFalse(state.running_prompt)

    def test_escape_cancels_without_running(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        desk.handle(ord("!"), state)
        for ch in "reboot":
            desk.handle(ord(ch), state)
        desk.handle(27, state)
        self.assertFalse(state.running_prompt)
        self.assertEqual(state.command, "")

    def test_an_empty_enter_just_closes_the_prompt(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        desk.handle(ord("!"), state)
        desk.handle(10, state)
        self.assertFalse(state.running_prompt)
        self.assertEqual(state.message, "")

    def test_the_prompt_echoes_and_backspace_edits(self):
        state = make_state()
        desk.handle(ord("!"), state)
        for ch in "top":
            desk.handle(ord(ch), state)
        self.assertIn("$ top", state.message)
        desk.handle(263, state)                               # Backspace
        self.assertIn("$ to", state.message)

    def test_the_footer_and_tip_explain_the_prompt(self):
        state = make_state()
        desk.handle(ord("!"), state)
        self.assertIn("Enter runs it", desk.footer(state))
        self.assertIn("page", state.tip())


class ApplicationsPlaceTests(unittest.TestCase):
    APPS = {
        "Internet": [
            {"id": "firefox.desktop", "name": "Firefox",
             "exec": "firefox --new-window", "terminal": False},
        ],
        "Accessories": [
            {"id": "htop.desktop", "name": "htop",
             "exec": "htop", "terminal": True},
        ],
    }

    def test_buckets_list_at_the_first_level(self):
        state = make_state()
        with mock.patch.object(registry, "applications",
                               return_value=self.APPS):
            state.submenu = "applications"
            listed = state.entries()
        self.assertEqual([e.label for e in listed],
                         [desk.BACK_LABEL, "Internet", "Accessories"])
        self.assertEqual([e.hint for e in listed[1:]],
                         ["1 apps", "1 apps"])

    def test_terminal_apps_launch_directly(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        with mock.patch.object(registry, "applications",
                               return_value=self.APPS), \
             mock.patch.object(desk.shutil, "which",
                               lambda name: f"/usr/bin/{name}"):
            state.path = ["Programs", "Applications", "Accessories"]
            state.selected = 1                                # past ".."
            desk.handle(10, state)
        self.assertEqual(calls, [("/usr/bin/htop",)])

    def test_gui_apps_are_contained_by_kilix_run(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        with mock.patch.object(registry, "applications",
                               return_value=self.APPS), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]), \
             mock.patch.object(desk, "_resolve_program", lambda name: name):
            state.path = ["Programs", "Applications", "Internet"]
            state.selected = 1
            desk.handle(10, state)
        self.assertEqual(calls, [("/opt/kilix/kilix", "run",
                                  "firefox", "--new-window")])

    def test_gui_apps_degrade_without_a_kilix_checkout(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        with mock.patch.object(registry, "applications",
                               return_value=self.APPS), \
             mock.patch.object(registry, "kilix_command", return_value=None):
            state.path = ["Programs", "Applications", "Internet"]
            rows = [e for e in state.entries() if not e.back]
        self.assertIsNone(rows[0].argv)
        self.assertIn("Kilix", rows[0].reason)

    def test_an_empty_catalog_is_a_state_not_an_error(self):
        state = make_state()
        with mock.patch.object(registry, "applications", return_value={}):
            state.submenu = "applications"
            rows = [e for e in state.entries() if not e.back]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)

    def test_the_scan_is_asked_once_per_visit_and_refresh_drops_it(self):
        state = make_state()
        asks = []
        with mock.patch.object(registry, "applications",
                               side_effect=lambda: asks.append(1) or self.APPS):
            state.submenu = "applications"
            state.entries()
            state.entries()
            self.assertEqual(len(asks), 1)
            desk.handle(ord("r"), state)
            state.entries()
        self.assertEqual(len(asks), 2)


class TextMouseTests(unittest.TestCase):
    def test_render_records_the_hit_map(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Machine")
        app.render_to_text(desk.render, state)
        self.assertEqual(state.text_hits["bar_row"], 1)
        self.assertGreater(state.text_hits["visible"], 0)
        self.assertIn("top", state.text_hits)

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

    def test_virtualbox_manager_resolves_from_this_checkout(self):
        item = next(
            item for item in registry.MACHINE
            if item.label == "VirtualBox VPN")
        with mock.patch("shutil.which", return_value=None):
            plan = registry.resolve(item)
        self.assertIsNotNone(plan)
        self.assertTrue(
            plan.argv[1].endswith("kilix-virtualbox-manager/main.py"))

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

    def test_web_browser_uses_the_real_browser_dispatch(self):
        item = next(
            item for item in registry.PROGRAMS if item.label == "Web browser"
        )
        with mock.patch.object(
                registry, "kilix_command",
                return_value=["/opt/kilix/kilix"]):
            plan = registry.resolve(item)
        self.assertEqual(plan.argv, ("/opt/kilix/kilix", "open-url"))
        self.assertNotIn("browse", plan.argv)

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
                               side_effect=kitty_rc.Unavailable("refused")), \
             mock.patch.object(desk, "_resolve_program", lambda name: name):
            desk._open(state, entry)
        self.assertEqual(calls, [("/usr/bin/tool",)])

    def test_bare_names_are_made_absolute_before_the_page_spawn(self):
        # kitty spawns a page's child from its own environment, whose PATH
        # may lack ~/.local/bin; a bare name then dies before its first
        # prompt and leaves a corpse page (the 0.1.7 dead rollout-resume
        # tab). The desk resolves in its own environment and hands the
        # terminal an absolute path.
        spawned = []
        state = make_state(runner=lambda argv: self.fail("page verb expected"),
                           live=lambda: True)
        entry = desk.Entry("Coding agents", ("kilix-rollout-resume",),
                           verb="tab")
        with mock.patch.object(
                kitty_rc, "launch_tab",
                lambda argv, **kw: spawned.append(tuple(argv)) or 7), \
             mock.patch.object(desk.shutil, "which",
                               lambda name: f"/home/someone/.local/bin/{name}"):
            desk._open(state, entry)
        self.assertEqual(
            spawned, [("/home/someone/.local/bin/kilix-rollout-resume",)])

    def test_a_missing_tool_fails_with_words_not_a_dead_page(self):
        state = make_state(runner=lambda argv: self.fail("must not spawn"),
                           live=lambda: True)
        entry = desk.Entry("Coding agents", ("no-such-tool-qq",), verb="tab")
        with mock.patch.object(
                kitty_rc, "launch_tab",
                side_effect=AssertionError("must not reach the terminal")), \
             mock.patch.object(desk.shutil, "which", lambda name: None):
            desk._open(state, entry)
        self.assertIn("no-such-tool-qq", state.message)
        self.assertIn("not installed", state.message)

    def test_local_bin_is_reached_when_path_misses_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            local = Path(home) / ".local" / "bin"
            local.mkdir(parents=True)
            tool = local / "kilix-rollout-resume"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {"HOME": home}), \
                 mock.patch.object(desk.shutil, "which", lambda name: None):
                self.assertEqual(
                    desk._resolve_program("kilix-rollout-resume"), str(tool))

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

    def test_the_frozen_contract_with_kilix_power(self):
        """`kilix power logout|reboot|poweroff` mirrors this exact list.

        The host verb exists for the desktops that cannot import this module;
        the two must never diverge. This pins the whole shape — three actions,
        these commands, every one confirming — so a drift on this side fails
        here rather than in a desktop.
        """
        actions = privileged.power_actions()
        self.assertEqual(len(actions), 3)
        commands = [tuple(argv[:2]) for _label, argv, _c in actions]
        self.assertEqual(commands, [
            ("loginctl", "terminate-session"),   # kilix power logout
            ("systemctl", "reboot"),             # kilix power reboot
            ("systemctl", "poweroff"),           # kilix power poweroff
        ])
        logout = actions[0][1]
        self.assertEqual(logout[2], os.environ.get("XDG_SESSION_ID", ""))
        for label, _argv, needs_confirmation in actions:
            self.assertTrue(needs_confirmation, f"{label} must confirm")

    def test_every_power_entry_confirms(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Power")
        actions = [entry for entry in state.entries() if not entry.back]
        self.assertTrue(actions)
        for entry in actions:
            self.assertTrue(entry.confirm)

    def test_the_back_row_carries_no_command(self):
        # The one row in Power that does not confirm must also be unable to
        # run anything at all.
        state = make_state()
        state.section = desk.SECTIONS.index("Power")
        back = state.entries()[0]
        self.assertTrue(back.back)
        self.assertIsNone(back.argv)

    def test_nothing_runs_on_a_single_keypress(self):
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        state.section = desk.SECTIONS.index("Power")
        # Select by label: an index would silently follow list changes and
        # confirm a different action than the one this test names.
        state.selected = next(
            index for index, entry in enumerate(state.entries())
            if entry.label == "Reboot")
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

    def test_where_you_are_is_stated_in_text_not_only_colour(self):
        # A monochrome terminal must still answer "where am I?", so the trail
        # and the cursor are both characters, not attributes.
        state = make_state()
        state.section = 2
        text = app.render_to_text(desk.render, state)
        self.assertIn("Kilix › Machine", text)
        self.assertIn("▶", text)

    def test_the_trail_grows_with_each_level(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Programs")
        state.submenu = "games"
        self.assertEqual(state.breadcrumb(), "Kilix › Programs › Games")
        self.assertIn("Kilix › Programs › Games",
                      app.render_to_text(desk.render, state))

    def test_every_screen_shows_the_keys_and_a_tip(self):
        state = make_state()
        text = app.render_to_text(desk.render, state)
        self.assertIn("? keys", text)
        self.assertIn("q quit", text)
        self.assertIn("tip", text)

    def test_the_key_line_is_never_cut_off(self):
        # The keys at the end of the line are the ones a stuck user needs.
        for width in (40, 60, 80, 120):
            line = keymap.footer(width)
            self.assertLessEqual(len(line), width, f"width {width}")
            self.assertTrue(line.endswith("q quit"), f"width {width}: {line}")

    def test_question_mark_opens_the_key_overlay_and_any_key_closes_it(self):
        state = make_state()
        desk.handle(ord("?"), state)
        self.assertTrue(state.help_open)
        text = app.render_to_text(desk.render, state)
        self.assertIn("Kilix TUI keys", text)
        self.assertIn("jump straight to a section", text)   # a non-footer key
        desk.handle(ord("x"), state)
        self.assertFalse(state.help_open)

    def test_slash_filters_the_list_and_escape_restores_it(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Machine")
        full = len(state.entries())
        desk.handle(ord("/"), state)
        for letter in "mem":
            desk.handle(ord(letter), state)
        labels = [entry.label for entry in state.entries() if not entry.back]
        self.assertEqual(labels, ["Memory"])
        desk.handle(27, state)                                # Esc clears
        self.assertEqual(state.filter, "")
        self.assertEqual(len(state.entries()), full)

    def test_filtering_never_hides_the_way_back(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Machine")
        desk.handle(ord("/"), state)
        for letter in "zzzz":
            desk.handle(ord(letter), state)
        self.assertEqual([e.label for e in state.entries()],
                         [desk.BACK_LABEL])

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
