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
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_desk import desk, durable, facts, graphics, gui, manual, registry, tango  # noqa: E402
from kilix_tui import app, keys as keymap, kitty_rc, privileged, proc  # noqa: E402

# The desk records launches in its one durable file; point that file into a
# scratch directory for this whole process so no test — the launch tests
# included — ever touches the user's real state (`durable.state_path`
# honors KILIX_TUI_STATE).
_SCRATCH_STATE = tempfile.TemporaryDirectory()
os.environ["KILIX_TUI_STATE"] = os.path.join(_SCRATCH_STATE.name, "desk.json")


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

    def test_focused_centers_share_state_but_cannot_back_into_the_desktop(self):
        module = load_entry()
        state, action, action_input = module._focused_state(
            ["--app", "system", "--action", "memory"])
        module._apply_action(state, "system", action, action_input)
        self.assertEqual(state.root_path, ("Machine",))
        self.assertEqual(state.breadcrumb(), "System Center")
        self.assertFalse(any(row.back for row in state.entries()))
        self.assertEqual(state.entries()[state.selected].label, "Memory")
        self.assertFalse(desk.handle(27, state))

    def test_software_install_action_opens_the_shared_confirmation_boundary(self):
        module = load_entry()
        rows = [{"id": "doom", "label": "Doom", "kind": "game"}]
        with mock.patch.object(registry, "installable", return_value=rows), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state, action, value = module._focused_state(
                ["--app", "software", "--action", "install", "doom"])
            module._apply_action(state, "software", action, value)
        self.assertEqual(state.root_path, ("Programs", "Software"))
        self.assertEqual(state.confirm,
                         ("Install Doom", ("/opt/kilix/kilix", "install", "doom")))


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

    def test_catalog_app_place_launches_every_app_through_the_host(self):
        state = make_state()
        state.path = ["Programs", "Catalog apps"]
        app_rows = [
            *self.ROWS,
            {"id": "kilix-file", "label": "File Manager", "kind": "app",
             "installed": True},
        ]
        with mock.patch.object(registry, "installable", return_value=app_rows), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = [row for row in state.entries() if not row.back]
        self.assertEqual([row.label for row in rows], ["File Manager"])
        self.assertEqual(
            rows[0].argv,
            ("/opt/kilix/kilix", "app", "run", "kilix-file"),
        )
        self.assertEqual(rows[0].verb, "inplace")

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


class HomeAlertTests(unittest.TestCase):
    """The default-password nag: confirmed or absent, never a guess."""

    def test_uncertainty_never_nags(self):
        # No helper (or no sudo) on this machine: not even a subprocess.
        with mock.patch.object(facts.os, "access", return_value=False), \
             mock.patch.object(facts.subprocess, "run",
                               side_effect=AssertionError("must not run")):
            self.assertFalse(facts.default_password())
        # Helper present but `sudo -n` refuses or the password changed:
        # still no nag.
        with mock.patch.object(facts.shutil, "which",
                               return_value="/usr/bin/sudo"), \
             mock.patch.object(facts.os, "access", return_value=True), \
             mock.patch.object(facts.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            self.assertFalse(facts.default_password())

    def test_the_check_is_the_bounded_helper_call(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((tuple(argv), kwargs.get("timeout")))
            return mock.Mock(returncode=0)

        with mock.patch.object(facts.shutil, "which",
                               return_value="/usr/bin/sudo"), \
             mock.patch.object(facts.os, "access", return_value=True), \
             mock.patch.object(facts.subprocess, "run", side_effect=run):
            self.assertTrue(facts.default_password())
        self.assertEqual(
            calls, [(("sudo", "-n", facts.PASSWD_HELPER, "check"), 5)])

    def test_no_alert_without_confirmation(self):
        with mock.patch.object(facts, "default_password", return_value=False):
            self.assertEqual(facts.alerts(), [])

    def test_home_shows_the_alert_and_points_at_the_fix(self):
        with mock.patch.object(facts, "default_password", return_value=True):
            lines = facts.alerts()
            state = make_state()
        self.assertTrue(any("System" in line for line in lines))
        state.section = desk.SECTIONS.index("Home")
        text = app.render_to_text(desk.render, state)
        self.assertIn("default password still set", text)
        self.assertIn("Change password", text)

    def test_refresh_reasks_the_alert(self):
        with mock.patch.object(facts, "alerts", side_effect=[[], ["x"]]):
            state = make_state()
            self.assertEqual(state.alerts, [])
            desk.handle(ord("r"), state)
        self.assertEqual(state.alerts, ["x"])

    def test_the_pixel_home_carries_the_alert(self):
        with mock.patch.object(facts, "default_password", return_value=True):
            state = make_state()
        canvases: list[StubCanvas] = []

        def factory(width, height):
            canvas = StubCanvas(width, height)
            canvases.append(canvas)
            return canvas

        renderer = graphics.DesktopRenderer(canvas_factory=factory)
        renderer.render(state, 100, 30, (960, 560), clock="12:00")
        drawn = " ".join(text for canvas in canvases for text in canvas.texts)
        self.assertIn("default password still set", drawn)

    def test_change_password_is_a_system_entry_with_held_output(self):
        state = make_state(live=lambda: True)
        state.section = desk.SECTIONS.index("System")
        with mock.patch.object(registry.shutil, "which",
                               lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(registry, "kilix_command", return_value=None):
            entry = next(e for e in state.entries()
                         if e.label == "Change password")
        self.assertEqual(entry.verb, "report")
        self.assertEqual(entry.argv[:2], ("sh", "-c"))
        self.assertIn("/usr/bin/passwd", entry.argv[2])


class TrayFactTests(unittest.TestCase):
    """Battery, network and volume on Home: shown when true, absent over a
    guess — and never asked per frame."""

    def test_batteries_skip_supplies_that_are_not_batteries(self):
        tree = {
            "/sys/class/power_supply/AC/type": "Mains\n",
            "/sys/class/power_supply/BAT0/type": "Battery\n",
            "/sys/class/power_supply/BAT0/capacity": "87\n",
            "/sys/class/power_supply/BAT0/status": "Discharging\n",
        }
        with mock.patch.object(
                proc, "_read",
                side_effect=lambda path, default="": tree.get(path, default)), \
             mock.patch.object(proc.os, "listdir",
                               return_value=["AC", "BAT0"]):
            self.assertEqual(proc.batteries(), [("BAT0", 87, "discharging")])

    def test_a_machine_without_batteries_answers_an_empty_list(self):
        with mock.patch.object(proc.os, "listdir", side_effect=OSError):
            self.assertEqual(proc.batteries(), [])
        with mock.patch.object(facts.proc, "batteries", return_value=[]):
            self.assertIsNone(facts.battery())

    def test_an_unreadable_capacity_is_marked_not_invented(self):
        tree = {"/sys/class/power_supply/BAT0/type": "Battery\n"}
        with mock.patch.object(
                proc, "_read",
                side_effect=lambda path, default="": tree.get(path, default)), \
             mock.patch.object(proc.os, "listdir", return_value=["BAT0"]):
            self.assertEqual(proc.batteries(), [("BAT0", -1, "")])
        with mock.patch.object(facts.proc, "batteries",
                               return_value=[("BAT0", -1, "")]):
            self.assertEqual(facts.battery(), ("battery", "?"))

    def test_network_links_skip_loopback_and_name_what_is_up(self):
        tree = {
            "/sys/class/net/eth0/operstate": "down\n",
            "/sys/class/net/wlan0/operstate": "up\n",
        }
        with mock.patch.object(
                proc, "_read",
                side_effect=lambda path, default="": tree.get(path, default)), \
             mock.patch.object(proc.os, "listdir",
                               return_value=["eth0", "lo", "wlan0"]):
            self.assertEqual(proc.network_links(),
                             [("eth0", "down"), ("wlan0", "up")])
        with mock.patch.object(facts.proc, "network_links",
                               return_value=[("eth0", "down"),
                                             ("wlan0", "up")]):
            self.assertEqual(facts.network(), ("network", "wlan0 up"))
        with mock.patch.object(facts.proc, "network_links",
                               return_value=[("eth0", "down")]):
            self.assertEqual(facts.network(),
                             ("network", "all 1 interfaces down"))
        with mock.patch.object(facts.proc, "network_links", return_value=[]):
            self.assertEqual(facts.network(), ("network", "no interfaces"))

    PACTL_SINKS = (
        "Sink #53\n"
        "\tName: alsa_output.usb\n"
        "\tMute: no\n"
        "\tVolume: front-left: 39321 /  60% / -13.31 dB\n"
        "\tBase Volume: 65536 / 100% / 0.00 dB\n"
        "Sink #54\n"
        "\tName: hdmi\n"
        "\tMute: yes\n"
        "\tVolume: front-left: 65536 / 100% / 0.00 dB\n"
    )

    def _pactl(self, default="alsa_output.usb"):
        def run(argv, **kwargs):
            stdout = (self.PACTL_SINKS if argv[1] == "list"
                      else f"{default}\n")
            return mock.Mock(returncode=0, stdout=stdout)
        return run

    def test_volume_reports_the_default_sink(self):
        with mock.patch.object(facts.shutil, "which",
                               return_value="/usr/bin/pactl"), \
             mock.patch.object(facts.subprocess, "run",
                               side_effect=self._pactl()):
            self.assertEqual(facts.volume(), "60%")
        with mock.patch.object(facts.shutil, "which",
                               return_value="/usr/bin/pactl"), \
             mock.patch.object(facts.subprocess, "run",
                               side_effect=self._pactl(default="hdmi")):
            self.assertEqual(facts.volume(), "muted (100%)")

    def test_no_pactl_means_no_row_and_no_subprocess(self):
        with mock.patch.object(facts.shutil, "which", return_value=None), \
             mock.patch.object(facts.subprocess, "run",
                               side_effect=AssertionError("must not run")):
            self.assertIsNone(facts.volume())

    def test_home_carries_the_tray_rows_and_uncertainty_is_absence(self):
        with mock.patch.object(facts, "battery",
                               return_value=("battery", "87% discharging")), \
             mock.patch.object(facts, "network",
                               return_value=("network", "wlan0 up")), \
             mock.patch.object(facts, "volume", return_value="60%"):
            rows = facts.status_rows()
            state = make_state()
        self.assertIn(("battery", "87% discharging"), rows)
        self.assertIn(("network", "wlan0 up"), rows)
        self.assertIn(("volume", "60%"), rows)
        state.section = desk.SECTIONS.index("Home")
        text = app.render_to_text(desk.render, state)
        self.assertIn("battery", text)
        self.assertIn("87% discharging", text)
        with mock.patch.object(facts, "battery", return_value=None), \
             mock.patch.object(facts, "volume", return_value=None), \
             mock.patch.object(facts, "network",
                               return_value=("network", "no interfaces")):
            labels = [label for label, _value in facts.status_rows()]
        self.assertNotIn("battery", labels)
        self.assertNotIn("volume", labels)
        self.assertIn("network", labels)


class SubmenuTests(unittest.TestCase):
    def test_games_drilldown_lists_and_flips_toggles(self):
        # An older launcher without `games play`: Enter stays the toggle so
        # the list is never a dead end.
        quiet_calls = []
        state = make_state(quiet=lambda argv: quiet_calls.append(argv) or 0)
        games = [("kilix-pong", "Kilix Pong", True),
                 ("doom", "Doom", False)]
        with mock.patch.object(registry, "games", return_value=games), \
             mock.patch.object(registry, "installable", return_value=None), \
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
             mock.patch.object(registry, "installable", return_value=None), \
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
             mock.patch.object(registry, "installable", return_value=None), \
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
        with mock.patch.object(registry, "kilix_command", return_value=None), \
             mock.patch.object(registry, "games", return_value=None):
            entries = [entry for entry in state.entries() if not entry.back]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].argv)

    def test_games_degrade_with_a_launcher_but_no_list_at_all(self):
        # A launcher that answers neither `install --json` nor the SDK
        # toggle table: one reasoned row, never a crash.
        state = make_state()
        state.submenu = "games"
        with mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]), \
             mock.patch.object(registry, "installable", return_value=None), \
             mock.patch.object(registry, "games", return_value=None):
            entries = [entry for entry in state.entries() if not entry.back]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].argv)
        self.assertTrue(entries[0].reason)


class GamesFromCatalogTests(unittest.TestCase):
    """The Games place lists the host catalog, not the SDK toggle table.

    A game added to the catalog is listed and playable with no desktop
    change; the toggle table survives only as the on/off hint and the `t`
    flip, so an older catalog loses nothing either.
    """

    CATALOG = [
        {"id": "kilix-land", "label": "Kilix Land", "kind": "game",
         "installed": False},
        {"id": "doom", "label": "Doom", "kind": "game", "installed": True},
        {"id": "kilix-file", "label": "File Manager", "kind": "app",
         "installed": True},
    ]
    KILIX = ["/opt/kilix/kilix"]

    def _entries(self, *, toggles, play=True, state=None):
        state = state or make_state()
        state.submenu = "games"
        with mock.patch.object(registry, "installable",
                               return_value=self.CATALOG), \
             mock.patch.object(registry, "games", return_value=toggles), \
             mock.patch.object(registry, "games_play_supported",
                               return_value=play), \
             mock.patch.object(registry, "kilix_command",
                               return_value=self.KILIX):
            return [e for e in state.entries() if not e.back]

    def test_a_catalog_game_is_listed_without_an_sdk_toggle(self):
        rows = self._entries(toggles=[("doom", "Doom", True)])
        self.assertEqual([e.label for e in rows], ["Doom", "Kilix Land"])
        land = rows[1]
        self.assertEqual(
            land.argv,
            ("/opt/kilix/kilix", "games", "play", "kilix-land"))
        self.assertEqual(land.hint, "installs on first play")
        self.assertIsNone(land.alt_argv)         # no toggle table row to flip

    def test_the_toggle_table_still_supplies_the_hint_and_the_flip(self):
        rows = self._entries(toggles=[("doom", "Doom", True)])
        doom = rows[0]
        self.assertEqual(doom.hint, "on")
        self.assertEqual(
            doom.alt_argv,
            ("/opt/kilix/kilix", "games", "disable", "doom"))

    def test_toggle_only_games_stay_listed_after_the_catalog_rows(self):
        rows = self._entries(
            toggles=[("chess-bash", "Chess Bash", False)])
        self.assertEqual([e.label for e in rows],
                         ["Doom", "Kilix Land", "Chess Bash"])
        self.assertEqual(rows[2].hint, "off")

    def test_no_sdk_still_lists_the_catalog_games(self):
        rows = self._entries(toggles=None)
        self.assertEqual([e.label for e in rows], ["Doom", "Kilix Land"])
        self.assertEqual([e.hint for e in rows],
                         ["installed", "installs on first play"])

    def test_a_catalog_game_on_an_old_launcher_installs_not_dead_ends(self):
        rows = self._entries(toggles=None, play=False)
        land = next(e for e in rows if e.label == "Kilix Land")
        self.assertEqual(land.argv,
                         ("/opt/kilix/kilix", "install", "kilix-land"))
        self.assertEqual(land.hint, "install")

    def test_games_and_software_read_the_one_cached_list(self):
        """The one-list discipline as the catalog grows: no second ask."""
        state = make_state()
        with mock.patch.object(registry, "installable",
                               return_value=self.CATALOG) as ask, \
             mock.patch.object(registry, "games", return_value=None), \
             mock.patch.object(registry, "games_play_supported",
                               return_value=True), \
             mock.patch.object(registry, "kilix_command",
                               return_value=self.KILIX):
            state.submenu = "games"
            state.entries()
            state.path = ["Programs", "Software"]
            state.entries()
            state.path = ["Programs", "Catalog apps"]
            state.entries()
            self.assertEqual(ask.call_count, 1)
            desk.handle(ord("r"), state)
            state.entries()
            self.assertEqual(ask.call_count, 2, "r must re-ask")


class CatalogOverrideTests(unittest.TestCase):
    """`KILIX_TUI_CATALOG` substitutes the one list; it never adds one."""

    def _write(self, payload):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(payload)
        handle.close()
        return handle.name

    def test_a_catalog_document_reads_as_the_launchers_rows(self):
        path = self._write(
            '{"content": [{"id": "doom", "label": "Doom", "kind": "game",'
            ' "description": "d"}]}')
        with mock.patch.dict(os.environ, {"KILIX_TUI_CATALOG": path}), \
             mock.patch("subprocess.run",
                        side_effect=AssertionError("no launcher call")):
            rows = registry.installable()
        self.assertEqual(rows, [{"id": "doom", "label": "Doom",
                                 "kind": "game", "description": "d",
                                 "installed": False}])

    def test_a_saved_json_answer_passes_through_unchanged(self):
        path = self._write('[{"id": "x", "kind": "app", "installed": true}]')
        with mock.patch.dict(os.environ, {"KILIX_TUI_CATALOG": path}):
            rows = registry.installable()
        self.assertEqual(rows, [{"id": "x", "kind": "app",
                                 "installed": True}])

    def test_an_unreadable_override_degrades_like_no_launcher(self):
        with mock.patch.dict(os.environ,
                             {"KILIX_TUI_CATALOG": "/nonexistent.json"}):
            self.assertIsNone(registry.installable())
        path = self._write("not json")
        with mock.patch.dict(os.environ, {"KILIX_TUI_CATALOG": path}):
            self.assertIsNone(registry.installable())


def real_catalog_path():
    """The kilix-content catalog checked out near this one, if any.

    Discovered, never hardcoded: this checkout's own parent first (flat
    layouts keep the two side by side), then the same workspace search the
    runtime uses (`sources.candidates`) under both accepted layouts. A
    machine without a kilix-content checkout skips the audit rather than
    failing it — the audit is about the catalog, not about having one.
    """
    from kilix_desk import sources
    bases = [str(ROOT.parent), *sources.candidates()]
    for base in bases:
        for tail in ("kilix-content",
                     os.path.join("kilix-modules", "kilix-content")):
            path = os.path.join(base, tail, "src", "kilix_content",
                                "catalog", "plebian.json")
            if os.path.isfile(path):
                return path
    return None


@unittest.skipUnless(real_catalog_path(), "no kilix-content checkout nearby")
class CatalogAuditTests(unittest.TestCase):
    """Every row of the real catalog reaches a place and resolves to an argv.

    The catalog is read through the runtime's own override
    (`KILIX_TUI_CATALOG`), so what is audited is exactly what the desktop
    would list. A future kind or entry that falls through fails here
    instead of quietly vanishing from every menu.
    """

    KILIX = ["/opt/kilix/kilix"]
    LAUNCH_PLACES = {"app": ["Programs", "Catalog apps"],
                     "game": ["Programs", "Games"]}

    def setUp(self):
        patcher = mock.patch.dict(
            os.environ, {"KILIX_TUI_CATALOG": real_catalog_path()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows = registry.installable()

    def _entries(self, path, kilix=True):
        state = make_state()
        state.path = list(path)
        with mock.patch.object(registry, "kilix_command",
                               return_value=self.KILIX if kilix else None), \
             mock.patch.object(registry, "games", return_value=None), \
             mock.patch.object(registry, "games_play_supported",
                               return_value=True):
            return [e for e in state.entries() if not e.back]

    def test_the_catalog_reads_and_names_the_expected_entries(self):
        self.assertIsInstance(self.rows, list)
        self.assertTrue(self.rows)
        ids = {row["id"] for row in self.rows}
        for expected in ("dosbox", "kilix-land", "kilix-tmux-manager"):
            self.assertIn(expected, ids)

    def test_every_row_is_installable_from_the_software_place(self):
        argvs = {e.label: e.argv for e in self._entries(
            ["Programs", "Software"])}
        for row in self.rows:
            self.assertIn(row["label"], argvs)
            self.assertEqual(
                argvs[row["label"]],
                ("/opt/kilix/kilix", "install", row["id"]),
                f"{row['id']} must install through the one command")

    def test_every_kind_has_a_decided_launch_place(self):
        unexpected = ({row["kind"] for row in self.rows}
                      - set(self.LAUNCH_PLACES))
        self.assertFalse(
            unexpected,
            f"new catalog kind(s) {sorted(unexpected)} need a place decision")

    def test_every_app_and_game_launch_resolves_in_its_place(self):
        expect = {
            "app": lambda i: ("/opt/kilix/kilix", "app", "run", i),
            "game": lambda i: ("/opt/kilix/kilix", "games", "play", i),
        }
        for kind, place in self.LAUNCH_PLACES.items():
            argvs = {e.label: e.argv for e in self._entries(place)}
            for row in self.rows:
                if row["kind"] != kind:
                    continue
                self.assertIn(row["label"], argvs,
                              f"{row['id']} vanished from {place[-1]}")
                self.assertEqual(argvs[row["label"]],
                                 expect[kind](row["id"]))

    def test_kilix_land_renders_in_both_places_with_its_state(self):
        games = {e.label: e for e in self._entries(["Programs", "Games"])}
        self.assertEqual(games["Kilix Land"].hint, "installs on first play")
        software = {e.label: e for e in self._entries(
            ["Programs", "Software"])}
        self.assertEqual(software["Kilix Land"].hint, "game")

    def test_every_place_degrades_without_a_launcher_never_crashes(self):
        for place in (["Programs", "Software"],
                      ["Programs", "Catalog apps"],
                      ["Programs", "Games"]):
            with mock.patch.object(registry, "installable",
                                   return_value=None):
                rows = self._entries(place, kilix=False)
            self.assertEqual(len(rows), 1, place)
            self.assertIsNone(rows[0].argv)
            self.assertTrue(rows[0].reason)

    def test_a_future_catalog_app_surfaces_with_no_desktop_change(self):
        # The multiplexer contract: when a new app row lands in the catalog,
        # it appears here and resolves through the host with zero edits.
        rows = self.rows + [{"id": "kilix-multiplexer",
                             "label": "Multiplexer", "kind": "app",
                             "installed": False}]
        state = make_state()
        state.path = ["Programs", "Catalog apps"]
        with mock.patch.object(registry, "installable", return_value=rows), \
             mock.patch.object(registry, "kilix_command",
                               return_value=self.KILIX):
            entries = {e.label: e for e in state.entries() if not e.back}
        self.assertIn("Multiplexer", entries)
        self.assertEqual(
            entries["Multiplexer"].argv,
            ("/opt/kilix/kilix", "app", "run", "kilix-multiplexer"))


class SystemMenuTests(unittest.TestCase):
    """The reference desktop's maintenance rows, grown into the System place."""

    def _system_entries(self, **kwargs):
        state = make_state(**kwargs)
        state.section = desk.SECTIONS.index("System")
        return state, state.entries()

    def test_reinstall_dependencies_is_hidden_without_the_helper(self):
        with mock.patch.object(registry, "helper_ready", return_value=False):
            _state, rows = self._system_entries()
        self.assertNotIn("Reinstall dependencies", [e.label for e in rows])

    def test_reinstall_dependencies_confirms_and_holds_its_output(self):
        with mock.patch.object(registry, "helper_ready", return_value=True):
            _state, rows = self._system_entries(live=lambda: True)
            entry = next(e for e in rows
                         if e.label == "Reinstall dependencies")
        self.assertTrue(entry.confirm)
        self.assertEqual(entry.verb, "report")
        self.assertEqual(entry.argv[:2], ("sh", "-c"))
        self.assertIn("sudo /usr/local/sbin/plebian-os-install-deps",
                      entry.argv[2])

    def test_scripts_is_a_system_place(self):
        _state, rows = self._system_entries()
        entry = next(e for e in rows if e.label == "Scripts")
        self.assertEqual(entry.submenu, "scripts")

    def test_the_scripts_place_lists_only_executable_shell_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "go.sh"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            (Path(tmp) / "plain.sh").write_text("")     # not executable
            with mock.patch.object(registry, "script_dirs",
                                   return_value=[tmp]):
                state = make_state()
                state.path = ["System", "Scripts"]
                rows = [e for e in state.entries() if not e.back]
        self.assertEqual([e.label for e in rows], ["go.sh"])
        self.assertEqual(rows[0].argv, (str(exe),))
        self.assertEqual(rows[0].verb, "inplace")

    def test_the_scripts_place_degrades_to_an_explanation(self):
        with mock.patch.object(registry, "script_rows", return_value=[]):
            state = make_state()
            state.path = ["System", "Scripts"]
            rows = [e for e in state.entries() if not e.back]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)
        self.assertIn("scripts", rows[0].reason)

    def test_the_scripts_listing_is_asked_once_per_visit(self):
        asks = []
        rows = [{"kind": "script", "label": "go.sh", "detail": "script",
                 "argv": ["/stack/scripts/go.sh"], "verb": "inplace"}]
        with mock.patch.object(registry, "script_rows",
                               side_effect=lambda: asks.append(1) or rows):
            state = make_state()
            state.path = ["System", "Scripts"]
            state.entries()
            state.entries()
            self.assertEqual(len(asks), 1)
            desk.handle(ord("r"), state)
            state.entries()
        self.assertEqual(len(asks), 2, "r must relist")


class ManualPlaceTests(unittest.TestCase):
    """System ▸ Manual: the stack's help book, paged in place."""

    def test_manual_is_a_system_place(self):
        state = make_state()
        state.section = desk.SECTIONS.index("System")
        entry = next(e for e in state.entries() if e.label == "Manual")
        self.assertEqual(entry.submenu, "manual")

    def test_the_place_lists_every_topic_and_pages_it_in_place(self):
        state = make_state()
        state.path = ["System", "Manual"]
        rows = [e for e in state.entries() if not e.back]
        titles = [title for _key, title in manual.topics()]
        self.assertEqual([e.label for e in rows], titles + ["Man pages"])
        for row, (key, _title) in zip(rows, manual.topics()):
            self.assertEqual(row.argv, (sys.executable, manual.PATH, key))
            self.assertEqual(row.verb, "inplace")

    def test_every_topic_renders_its_title_as_text(self):
        for key, title in manual.topics():
            text = manual.render(key)
            self.assertIn(title, text)
            self.assertTrue(text.endswith("\n"))

    def test_the_recovery_ladder_walks_override_installed_then_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "override.md"
            installed = Path(tmp) / "installed" / "RECOVERY.md"
            source = Path(tmp) / "pleb" / "docs" / "RECOVERY.md"
            source.parent.mkdir(parents=True)
            with mock.patch.object(manual, "PLEB_RECOVERY_DOC",
                                   str(installed)), \
                 mock.patch.object(manual.sources, "component_dir",
                                   return_value=str(Path(tmp) / "pleb")), \
                 mock.patch.dict(os.environ,
                                 {"PLEB_RECOVERY_DOC_DST": str(override)}):
                self.assertIsNone(manual.recovery_path())
                source.write_text("# from source\n")
                self.assertEqual(manual.recovery_path(), str(source))
                installed.parent.mkdir()
                installed.write_text("# installed\n")
                self.assertEqual(manual.recovery_path(), str(installed))
                override.write_text("# relocated\n")
                self.assertEqual(manual.recovery_path(), str(override))

    def test_the_recovery_entry_hints_which_way_the_launch_goes(self):
        state = make_state()
        state.path = ["System", "Manual"]
        with mock.patch.object(manual, "recovery_path", return_value=None):
            row = next(e for e in state.entries()
                       if e.label == "Pleb Recovery Guide")
            self.assertEqual(row.hint, "self-help steps")
            self.assertIsNotNone(row.argv, "the fallback answers, "
                                 "so the entry never disables")
        with mock.patch.object(manual, "recovery_path",
                               return_value="/doc/RECOVERY.md"):
            row = next(e for e in state.entries()
                       if e.label == "Pleb Recovery Guide")
            self.assertEqual(row.hint, "installed guide")

    def test_a_missing_guide_answers_with_self_help_not_a_refusal(self):
        pages = []
        with mock.patch.object(manual, "recovery_path", return_value=None), \
             mock.patch.object(manual, "_page",
                               side_effect=lambda text: pages.append(text)
                               or 0):
            self.assertEqual(manual.main(["recovery"]), 0)
        self.assertIn("sudo /usr/local/sbin/plebian-os-install-deps",
                      pages[0])
        self.assertIn("libxxhash", pages[0])

    def test_the_program_pages_standalone_the_way_the_desk_runs_it(self):
        # The desk hands the manual an argv and the terminal, nothing else:
        # the file must run outside any package context, and PAGER is the
        # seam a test (or an operator) redirects.
        with tempfile.TemporaryDirectory() as tmp:
            guide = Path(tmp) / "RECOVERY.md"
            guide.write_text("recover by rebooting\n")
            env = dict(os.environ, PAGER="cat",
                       PLEB_RECOVERY_DOC_DST=str(guide))
            done = subprocess.run(
                [sys.executable, manual.PATH, "recovery"], env=env,
                capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertIn("recover by rebooting", done.stdout)

    def test_the_program_lists_its_topics_and_refuses_unknown_ones(self):
        listed = subprocess.run(
            [sys.executable, manual.PATH, "--list"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(
            [line.split("\t")[0] for line in listed.stdout.splitlines()],
            [key for key, _title in manual.topics()])
        unknown = subprocess.run(
            [sys.executable, manual.PATH, "no-such-topic"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("welcome", unknown.stderr)


class VoiceOrientationTests(unittest.TestCase):
    """The Voice place says where speak and dictate actually live."""

    def test_the_voice_place_carries_the_orientation_entry(self):
        state = make_state()
        state.path = ["Programs", "Voice"]
        row = next(e for e in state.entries()
                   if e.label == "Where speak and dictate live")
        self.assertEqual(row.argv, (sys.executable, manual.PATH, "voice"))
        self.assertEqual(row.verb, "inplace")

    def test_the_wording_names_the_host_chrome_boundary(self):
        # The point of the topic, pinned: the widgets belong to Kilix's
        # page strip, and the desktop entries are settings and diagnostics.
        text = manual.render("voice")
        self.assertIn("page strip", text)
        self.assertIn("host chrome", text)
        self.assertIn("click-to-talk", text)

    def test_voice_studio_shows_the_same_entry(self):
        module = load_entry()
        state, action, value = module._focused_state(["--app", "voice"])
        module._apply_action(state, "voice", action, value)
        self.assertIn("Where speak and dictate live",
                      [e.label for e in state.entries()])


class ManPagesPlaceTests(unittest.TestCase):
    """Manual ▸ Man pages: kilix-95's System Manual, as a filterable list."""

    def test_the_place_lists_discovered_pages_and_enter_renders_one(self):
        run_calls = []
        state = make_state(runner=lambda argv: run_calls.append(argv) or 0)
        pages = [{"name": "grep", "section": "1", "label": "grep (1)"}]
        with mock.patch.object(manual, "man_pages", return_value=pages), \
             mock.patch.object(desk, "_resolve_program", lambda name: name):
            state.path = ["System", "Manual", "Man pages"]
            rows = [e for e in state.entries() if not e.back]
            self.assertEqual([e.label for e in rows], ["grep (1)"])
            state.selected = 1                            # past ".."
            desk.handle(10, state)
        self.assertEqual(run_calls, [("man", "1", "grep")])

    def test_discovery_parses_the_manpath_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            man1 = Path(tmp) / "man1"
            man1.mkdir()
            (man1 / "grep.1.gz").write_text("")
            (man1 / "README").write_text("")            # not a page name
            (man1 / "weird.1.tar").write_text("")       # unknown compression
            man5 = Path(tmp) / "de" / "man5"            # localized tree
            man5.mkdir(parents=True)
            (man5 / "crontab.5").write_text("")
            pages = manual.man_pages(roots=[tmp])
        self.assertEqual([page["label"] for page in pages],
                         ["crontab (5)", "grep (1)"])

    def test_the_first_manpath_occurrence_wins(self):
        with tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            for root in (first, second):
                man1 = Path(root) / "man1"
                man1.mkdir()
                (man1 / "grep.1").write_text("")
            pages = manual.man_pages(roots=[first, second])
        self.assertEqual(len(pages), 1)

    def test_the_scan_is_cached_per_visit_and_r_rescans(self):
        asks = []
        pages = [{"name": "grep", "section": "1", "label": "grep (1)"}]
        with mock.patch.object(manual, "man_pages",
                               side_effect=lambda: asks.append(1) or pages):
            state = make_state()
            state.path = ["System", "Manual", "Man pages"]
            state.entries()
            state.entries()
            self.assertEqual(len(asks), 1)
            desk.handle(ord("r"), state)
            state.entries()
        self.assertEqual(len(asks), 2, "r must rescan")

    def test_an_empty_manpath_is_a_state_not_an_error(self):
        with mock.patch.object(manual, "man_pages", return_value=[]):
            state = make_state()
            state.path = ["System", "Manual", "Man pages"]
            rows = [e for e in state.entries() if not e.back]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)
        self.assertIn("manpath", rows[0].reason)


class UpdateRestartTests(unittest.TestCase):
    """`Update and restart desktop`: one confirmation, then a fresh process."""

    def _state(self, runner, restarts):
        state = make_state(runner=runner,
                           restart=lambda: restarts.append(True))
        state.section = desk.SECTIONS.index("System")
        return state

    def _select(self, state, label):
        state.selected = next(
            index for index, entry in enumerate(state.entries())
            if entry.label == label)

    def test_update_then_reexec_after_one_confirmation(self):
        calls, restarts = [], []
        with mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state = self._state(lambda argv: calls.append(tuple(argv)) or 0,
                                restarts)
            self._select(state, "Update and restart desktop")
            desk.handle(10, state)                            # asks first
            self.assertEqual(calls, [])
            self.assertEqual(state.confirm[1], ("/opt/kilix/kilix", "update"))
            desk.handle(ord("y"), state)
        self.assertEqual(calls, [("/opt/kilix/kilix", "update")])
        self.assertEqual(restarts, [True])

    def test_a_failed_update_never_restarts(self):
        restarts = []
        with mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state = self._state(lambda argv: 3, restarts)
            self._select(state, "Update and restart desktop")
            desk.handle(10, state)
            desk.handle(ord("y"), state)
        self.assertEqual(restarts, [])
        self.assertIn("exited 3", state.message)

    def test_cancelling_clears_the_restart_intent(self):
        calls, restarts = [], []
        with mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state = self._state(lambda argv: calls.append(tuple(argv)) or 0,
                                restarts)
            self._select(state, "Update and restart desktop")
            desk.handle(10, state)
            desk.handle(ord("n"), state)                      # cancel
            self.assertEqual(restarts, [])
            # A later plain update must not inherit the intent.
            self._select(state, "Update the stack")
            desk.handle(10, state)
            desk.handle(ord("y"), state)
        self.assertEqual(calls, [("/opt/kilix/kilix", "update")])
        self.assertEqual(restarts, [])

    def test_power_keeps_the_frozen_three_actions(self):
        # The new row is maintenance and lives in System; the privileged
        # contract `kilix power` mirrors is untouched.
        labels = [label for label, _argv, _c in privileged.power_actions()]
        self.assertEqual(len(labels), 3)
        self.assertNotIn("Update and restart desktop", labels)

    def test_the_default_restart_replaces_this_process(self):
        with mock.patch.object(desk.os, "execv") as execv:
            desk._restart_desktop()
        (program, argv), _kwargs = execv.call_args
        self.assertEqual(program, sys.executable)
        self.assertEqual(argv[0], sys.executable)


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


class DurableStateTests(unittest.TestCase):
    """The one durable record: recents and pinned Home rows (durable.py)."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "KILIX_TUI_STATE": os.path.join(scratch.name, "desk.json")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _home(self):
        state = make_state()
        state.path = ["Home"]
        return state, [e for e in state.entries() if not e.back]

    def test_a_resolved_launch_is_remembered_and_offered_on_home(self):
        state = make_state()
        with mock.patch.object(desk, "_resolve_program", lambda name: name):
            desk._launch(state, desk.Entry("Music", ("kilix-music",)))
        _state, rows = self._home()
        self.assertEqual([(e.label, e.argv) for e in rows],
                         [("Music", ("kilix-music",))])
        self.assertEqual(rows[0].hint, "recent")
        self.assertEqual(rows[0].verb, "tab")

    def test_an_unresolvable_launch_is_not_remembered(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        with mock.patch.object(desk.shutil, "which", lambda name: None):
            desk._launch(state, desk.Entry("Ghost", ("no-such-tool-qq",)))
        self.assertEqual(durable.recents(), [])

    def test_recents_are_capped_deduped_and_most_recent_first(self):
        for index in range(10):
            durable.remember_launch(f"tool{index}", (f"tool{index}",))
        durable.remember_launch("tool3", ("tool3",))
        names = [row["label"] for row in durable.recents()]
        self.assertEqual(len(names), durable.MAX_RECENTS)
        self.assertEqual(names[0], "tool3")
        self.assertEqual(len(set(names)), len(names))

    def test_p_pins_the_selected_entry_and_p_on_home_unpins_it(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Machine")
        with mock.patch.object(registry.shutil, "which",
                               lambda name: f"/usr/bin/{name}"):
            entries = state.entries()
            state.selected = next(index for index, e in enumerate(entries)
                                  if e.label == "Network")
            desk.handle(ord("p"), state)
        self.assertIn("pinned", state.message)
        _state, rows = self._home()
        self.assertEqual(rows[0].label, "Network")
        self.assertEqual(rows[0].hint, "pinned")
        home, _rows = self._home()
        home.selected = 1                                  # past ".."
        desk.handle(ord("p"), home)
        self.assertIn("unpinned", home.message)
        self.assertEqual(durable.pinned(), [])

    def test_pinned_rows_shadow_their_recent_twin(self):
        durable.remember_launch("Music", ("kilix-music",))
        durable.toggle_pin("Music", ("kilix-music",))
        _state, rows = self._home()
        self.assertEqual([e.label for e in rows], ["Music"])
        self.assertEqual(rows[0].hint, "pinned")

    def test_home_renders_the_rows_and_r_rereads_them(self):
        durable.toggle_pin("Music", ("kilix-music",))
        state = make_state()
        state.path = ["Home"]
        text = app.render_to_text(desk.render, state)
        self.assertIn("Music", text)
        self.assertIn("pinned", text)
        durable.toggle_pin("Weather", ("kilix-weather",))
        self.assertNotIn("Weather", [e.label for e in state.entries()],
                         "the record is read once per visit")
        desk.handle(ord("r"), state)
        self.assertIn("Weather", [e.label for e in state.entries()])

    def test_confirmed_actions_never_become_home_rows(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Power")
        state.selected = next(index for index, e
                              in enumerate(state.entries())
                              if e.label == "Reboot")
        desk.handle(ord("p"), state)          # must refuse the pin
        self.assertNotIn("pinned", state.message)
        desk.handle(10, state)                # ask…
        desk.handle(ord("y"), state)          # …and run via the stub runner
        self.assertEqual(durable.recents(), [])
        self.assertEqual(durable.pinned(), [])

    def test_typed_commands_run_but_never_become_home_rows(self):
        # The `!` prompt shares the launch verbs, not the durable record: a
        # command typed once went through nobody's list, so it must not be
        # offered again on Home (nor become pinnable there).
        calls = []
        state = make_state(runner=lambda argv: calls.append(tuple(argv)) or 0)
        with mock.patch.object(desk, "_resolve_program", lambda name: name):
            desk.handle(ord("!"), state)
            for ch in "sudo reboot":
                desk.handle(ord(ch), state)
            desk.handle(10, state)
        self.assertEqual(calls, [("sudo", "reboot")])
        self.assertEqual(durable.recents(), [])
        _state, rows = self._home()
        self.assertEqual(rows, [])

    def test_the_record_survives_corruption_and_writes_only_on_change(self):
        with open(durable.state_path(), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(durable.load(), {"recents": [], "pinned": []})
        durable.remember_launch("Music", ("kilix-music",))
        self.assertEqual([row["label"] for row in durable.recents()],
                         ["Music"])
        with mock.patch.object(durable.os, "replace",
                               side_effect=AssertionError("must not write")):
            durable.save(durable.load())                   # unchanged: no I/O
            durable.remember_launch("Music", ("kilix-music",))

    def test_malformed_rows_are_dropped_not_rendered(self):
        durable.save({"recents": [
            {"label": "Good", "argv": ["good"]},
            {"label": "", "argv": ["bad"]},
            {"label": "NoArgv", "argv": []},
            "not a row",
        ], "pinned": []})
        self.assertEqual([row["label"] for row in durable.recents()],
                         ["Good"])

    def test_the_file_lives_under_xdg_state_home_by_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/xdg/state"},
                             clear=False):
            os.environ.pop("KILIX_TUI_STATE", None)
            self.assertEqual(
                durable.state_path(),
                os.path.join("/xdg/state", "kilix-tui", "desk.json"))


class LaunchersPlaceTests(unittest.TestCase):
    """Programs ▸ Launchers: the user's own desktop-folder `.desktop` files."""

    LAUNCHERS = [
        {"id": "mine.desktop", "name": "My Thing", "exec": "mything",
         "terminal": True},
        {"id": "site.desktop", "name": "My Site",
         "exec": "firefox https://example.org", "terminal": False},
    ]

    def test_launchers_is_a_programs_place(self):
        state = make_state()
        state.section = desk.SECTIONS.index("Programs")
        entry = next(e for e in state.entries() if e.label == "Launchers")
        self.assertEqual(entry.submenu, "launchers")

    def test_terminal_launchers_run_directly_and_gui_ones_are_contained(self):
        state = make_state()
        with mock.patch.object(registry, "user_launchers",
                               return_value=self.LAUNCHERS), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            state.path = ["Programs", "Launchers"]
            rows = {e.label: e for e in state.entries() if not e.back}
        self.assertEqual(rows["My Thing"].argv, ("mything",))
        self.assertEqual(rows["My Thing"].verb, "tab")
        self.assertEqual(rows["My Site"].argv,
                         ("/opt/kilix/kilix", "run", "firefox",
                          "https://example.org"))

    def test_gui_launchers_degrade_without_a_kilix_checkout(self):
        state = make_state(runner=lambda argv: self.fail("must not run"))
        with mock.patch.object(registry, "user_launchers",
                               return_value=self.LAUNCHERS), \
             mock.patch.object(registry, "kilix_command", return_value=None):
            state.path = ["Programs", "Launchers"]
            rows = {e.label: e for e in state.entries() if not e.back}
        self.assertIsNone(rows["My Site"].argv)
        self.assertIn("Kilix", rows["My Site"].reason)
        self.assertEqual(rows["My Thing"].argv, ("mything",))

    def test_an_empty_folder_is_a_state_not_an_error(self):
        state = make_state()
        with mock.patch.object(registry, "user_launchers", return_value=[]):
            state.path = ["Programs", "Launchers"]
            rows = [e for e in state.entries() if not e.back]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)
        self.assertIn("desktop folders", rows[0].reason)

    def test_the_folders_are_read_once_per_visit_and_r_rereads(self):
        asks = []
        with mock.patch.object(
                registry, "user_launchers",
                side_effect=lambda: asks.append(1) or self.LAUNCHERS):
            state = make_state()
            state.path = ["Programs", "Launchers"]
            state.entries()
            state.entries()
            self.assertEqual(len(asks), 1)
            desk.handle(ord("r"), state)
            state.entries()
        self.assertEqual(len(asks), 2, "r must reread")

    def test_user_launchers_read_the_folders_through_the_shared_parser(self):
        with tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            (Path(first) / "thing.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Thing\n"
                "Exec=thing\nTerminal=true\n")
            (Path(second) / "thing.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Shadowed\n"
                "Exec=shadowed\nTerminal=true\n")
            (Path(second) / "other.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Other\n"
                "Exec=other\nTerminal=true\n")
            with mock.patch.object(registry, "launcher_dirs",
                                   return_value=[first, second]):
                rows = registry.user_launchers()
        # The first (Kilix 95) folder wins a duplicated file name.
        self.assertEqual([row["name"] for row in rows], ["Thing", "Other"])

    def test_the_launcher_folders_are_the_desktop_data_roots(self):
        with mock.patch.dict(os.environ,
                             {"KILIX_DESKTOP_DIR": "/somewhere/desktop"}):
            self.assertEqual(registry.launcher_dirs(),
                             ["/somewhere/desktop"])
        with mock.patch.dict(os.environ, {"GPU_TERMINAL_HOME": "/gt"},
                             clear=False):
            os.environ.pop("KILIX_DESKTOP_DIR", None)
            self.assertEqual(
                registry.launcher_dirs(),
                [os.path.join("/gt", "kilix-95", "data", "desktop"),
                 os.path.join("/gt", "kilix", "data", "desktop")])


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

    def test_network_prefers_the_canonical_tool_over_nmtui(self):
        # The decided boundary: the Network row lands on kilix-network, from
        # the installed command or this checkout, never straight on nmtui.
        item = next(i for i in registry.MACHINE if i.label == "Network")
        self.assertEqual(item.command, "kilix-network")
        with mock.patch.object(registry.shutil, "which", return_value=None):
            plan = registry.resolve(item)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.argv[1].endswith("tools/network/main.py"))

    def test_nmtui_remains_the_presence_gated_connection_editor(self):
        # The accepted external exception: creating connections and entering
        # secrets stay in NetworkManager's own editor, offered only where it
        # exists and never resolved from a source checkout.
        item = next(i for i in registry.MACHINE
                    if i.label == "Connection editor")
        self.assertEqual(item.command, "nmtui")
        self.assertIsNone(item.sibling)
        with mock.patch.object(registry.shutil, "which", return_value=None):
            self.assertIsNone(registry.resolve(item))
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/nmtui"):
            self.assertEqual(registry.resolve(item).argv,
                             ("/usr/bin/nmtui",))

    def test_kilix_subcommand_is_the_last_resort(self):
        item = registry.Item("x", command="kilix-bonsai", kilix=("bonsai",))
        with mock.patch.object(registry.shutil, "which", return_value=None), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            plan = registry.resolve(item)
        self.assertEqual(plan.argv, ("/opt/kilix/kilix", "bonsai"))

    def test_tmux_manager_falls_back_to_the_kilix_verb(self):
        # `kilix tmux` installs-and-runs the manager — the same command the
        # catalog's Tmux Sessions entry launches — so the row resolves on a
        # machine that has never installed the binary.
        item = next(i for i in registry.SESSION if i.label == "Tmux manager")
        self.assertEqual(item.command, "tmux-tui")
        with mock.patch.object(registry.shutil, "which", return_value=None), \
             mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            plan = registry.resolve(item)
        self.assertEqual(plan.argv, ("/opt/kilix/kilix", "tmux"))
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/tmux-tui"):
            self.assertEqual(registry.resolve(item).argv,
                             ("/usr/bin/tmux-tui",))

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

    def test_pdf_conversion_uses_the_shared_app_verb_in_a_tab(self):
        item = next(
            item for item in registry.PROGRAMS
            if item.label == "PDF Conversion"
        )
        with mock.patch.object(
                registry, "kilix_command",
                return_value=["/opt/kilix/kilix"]):
            plan = registry.resolve(item)
        self.assertEqual(
            plan.argv,
            ("/opt/kilix/kilix", "app", "run", "kilix-pdf-conversion"),
        )
        self.assertEqual(plan.verb, "tab")

    def test_unresolvable_items_carry_a_reason_not_a_crash(self):
        item = registry.Item("x", command="kilix-bonsai", kilix=("bonsai",))
        with mock.patch.object(registry.shutil, "which", return_value=None), \
             mock.patch.object(registry, "kilix_command", return_value=None):
            self.assertIsNone(registry.resolve(item))
        self.assertTrue(registry.disabled_reason(item))

    def test_source_checkout_never_wins_for_non_sibling_tools(self):
        # The Start-menu rule: a working tree is not the pinned closure. Only
        # `command`, `kilix` and OS-helper branches exist for outside tools —
        # assert the registry offers no path-based resolution for them.
        for section in registry.SECTIONS.values():
            for item in section:
                if item.sibling is None and not item.submenu:
                    self.assertTrue(item.command or item.kilix or item.helper)


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


class PaletteFlavorTests(unittest.TestCase):
    """F-FLAVOR: selectable Tango accents, one shared setting, red reserved."""

    def tearDown(self):
        tango.apply("tango")

    def test_a_flavor_swaps_only_the_accent_ramp(self):
        furniture = (tango.WHITE, tango.RED, tango.RED_BRIGHT, tango.GREY,
                     tango.CARD, tango.BG_TOP)
        self.assertEqual(tango.apply("plum"), "plum")
        self.assertEqual(
            (tango.BLUE_DEEP, tango.BLUE, tango.BLUE_BRIGHT),
            ((92, 53, 102), (117, 80, 123), (173, 127, 168)))
        self.assertEqual((tango.WHITE, tango.RED, tango.RED_BRIGHT,
                          tango.GREY, tango.CARD, tango.BG_TOP), furniture)
        self.assertEqual(tango.apply("tango"), "tango")
        self.assertEqual(tango.BLUE, (52, 101, 164))

    def test_the_shared_setting_selects_the_flavor(self):
        from kilix_tui import theme
        chosen = {"KILIX_TUI_FLAVOR": "amber"}
        with mock.patch.object(
                theme, "setting",
                side_effect=lambda key, default: chosen.get(key, default)):
            self.assertEqual(tango.apply(), "amber")
        self.assertEqual(tango.BLUE, (245, 121, 0))

    def test_an_unknown_or_garbage_name_degrades_to_the_default(self):
        from kilix_tui import theme
        with mock.patch.object(theme, "setting",
                               side_effect=lambda key, default: default):
            self.assertEqual(tango.apply("mauve"), "tango")
        with mock.patch.object(theme, "setting",
                               side_effect=lambda key, default: "MAUVE"):
            self.assertEqual(tango.apply(), "tango")
        self.assertEqual(tango.BLUE, (52, 101, 164))

    def test_every_flavor_names_a_real_curses_colour(self):
        import curses
        for name, spec in tango.FLAVORS.items():
            self.assertTrue(hasattr(curses, str(spec["pair"])), name)
            for ramp in ("deep", "mid", "bright"):
                self.assertEqual(len(spec[ramp]), 3, name)

    def test_the_palette_place_lists_flavors_and_marks_the_current(self):
        state = make_state()
        state.path = ["System", "Palette"]
        rows = [entry for entry in state.entries() if not entry.back]
        self.assertEqual([entry.flavor for entry in rows],
                         list(tango.FLAVORS))
        self.assertEqual(
            [entry.hint for entry in rows if entry.flavor == "tango"],
            ["current"])
        self.assertIn("Palette", app.render_to_text(desk.render, state))

    def test_enter_wears_a_flavor_and_names_the_persistent_knob(self):
        state = make_state()
        state.path = ["System", "Palette"]
        labels = [entry.flavor for entry in state.entries()]
        state.selected = labels.index("plum")
        desk.handle(ord("\n"), state)
        self.assertEqual(tango.FLAVOR, "plum")
        self.assertIn("KILIX_TUI_FLAVOR=plum", state.message)
        # The list now marks the worn flavor, not the shipped default.
        rows = [entry for entry in state.entries() if not entry.back]
        self.assertEqual(
            [entry.hint for entry in rows if entry.flavor == "plum"],
            ["current"])

    def test_wearing_a_flavor_is_never_remembered_or_pinnable(self):
        state = make_state()
        state.path = ["System", "Palette"]
        before = durable.load()
        labels = [entry.flavor for entry in state.entries()]
        state.selected = labels.index("amber")
        desk.handle(ord("\n"), state)
        desk.handle(ord("p"), state)
        self.assertEqual(durable.load(), before)


class IdleSaverTests(unittest.TestCase):
    """F-SAVER: an untouched session starts a screensaver on its own."""

    def test_the_default_is_on_only_when_the_desktop_is_the_session(self):
        from kilix_tui import theme
        with mock.patch.object(theme, "setting",
                               side_effect=lambda key, default: default):
            with mock.patch.dict(os.environ, {"KILIX_TUI_SESSION": "1"}):
                self.assertEqual(desk.idle_saver_seconds(), 600.0)
            environment = {key: value for key, value in os.environ.items()
                           if key != "KILIX_TUI_SESSION"}
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertIsNone(desk.idle_saver_seconds())

    def test_the_shared_setting_overrides_the_default_both_ways(self):
        from kilix_tui import theme
        for configured, expected in (("2", 120.0), ("0", None),
                                     ("soon", None)):
            with mock.patch.object(
                    theme, "setting",
                    side_effect=lambda key, default, value=configured: value), \
                 mock.patch.dict(os.environ, {"KILIX_TUI_SESSION": "1"}):
                self.assertEqual(desk.idle_saver_seconds(), expected,
                                 configured)

    def test_the_saver_is_the_same_launch_the_place_offers(self):
        from kilix_tui import theme
        with mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]), \
             mock.patch.object(registry, "screensavers",
                               return_value=["maze", "pipes"]), \
             mock.patch.object(theme, "setting",
                               side_effect=lambda key, default: default):
            self.assertEqual(desk.saver_argv(),
                             ("/opt/kilix/kilix", "screensaver", "maze"))

    def test_a_named_favourite_wins_and_an_unknown_one_degrades(self):
        from kilix_tui import theme
        for wanted, name in (("pipes", "pipes"), ("nonsense", "maze")):
            with mock.patch.object(registry, "kilix_command",
                                   return_value=["kilix"]), \
                 mock.patch.object(registry, "screensavers",
                                   return_value=["maze", "pipes"]), \
                 mock.patch.object(
                     theme, "setting",
                     side_effect=lambda key, default, value=wanted: value):
                self.assertEqual(desk.saver_argv(),
                                 ("kilix", "screensaver", name), wanted)

    def test_no_checkout_or_no_savers_means_no_launch_and_no_error(self):
        with mock.patch.object(registry, "kilix_command", return_value=None):
            self.assertIsNone(desk.saver_argv())
        with mock.patch.object(registry, "kilix_command",
                               return_value=["kilix"]), \
             mock.patch.object(registry, "screensavers", return_value=[]):
            self.assertIsNone(desk.saver_argv())
        ran = []
        state = make_state(runner=lambda argv: ran.append(argv) or 0)
        with mock.patch.object(desk, "saver_argv", return_value=None):
            desk.start_screensaver(state)
        self.assertEqual(ran, [])

    def test_the_idle_start_runs_attached_and_is_never_remembered(self):
        ran = []
        state = make_state(runner=lambda argv: ran.append(argv) or 0)
        before = durable.load()
        argv = ("kilix", "screensaver", "maze")
        with mock.patch.object(desk, "saver_argv", return_value=argv):
            desk.start_screensaver(state)
        self.assertEqual(ran, [argv])
        self.assertEqual(durable.load(), before)

    def test_the_desktop_wires_the_saver_into_the_shared_loop(self):
        module = load_entry()
        with mock.patch.object(module.app, "run", return_value=0) as run, \
             mock.patch.object(module.desk, "idle_saver_seconds",
                               return_value=123.0):
            self.assertEqual(module.main([]), 0)
        self.assertEqual(run.call_args.kwargs.get("idle_after"), 123.0)
        self.assertIs(run.call_args.kwargs.get("on_idle"),
                      desk.start_screensaver)


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


class TerminalRequirementTests(unittest.TestCase):
    """Answering a question must not need a screen, and a missing screen
    must not be a traceback."""

    def test_version_answers_without_a_terminal(self):
        import io
        from contextlib import redirect_stdout
        path = ROOT / "tools" / "plebian_control" / "main.py"
        spec = importlib.util.spec_from_file_location("tool_plebian", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = io.StringIO()
        with mock.patch.object(module.app, "run",
                               side_effect=AssertionError("no curses")), \
                redirect_stdout(out):
            code = module.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("kilix", out.getvalue())

    def test_a_terminal_that_cannot_open_reports_instead_of_raising(self):
        import io
        from contextlib import redirect_stderr
        from kilix_tui import app as app_module
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TERM": "nonesuch-terminal"},
                             clear=False), \
                mock.patch.object(app_module.curses, "wrapper",
                                  side_effect=app_module.curses.error(
                                      "setupterm: could not find terminal")), \
                redirect_stderr(err):
            code = app_module.run(lambda *_: None, object())
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", err.getvalue())
        self.assertIn("nonesuch-terminal", err.getvalue())

    def test_versions_read_a_provisioned_layout(self):
        import io
        from contextlib import redirect_stdout
        path = ROOT / "tools" / "plebian_control" / "main.py"
        spec = importlib.util.spec_from_file_location("tool_plebian2", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            # The provisioned layout: checkouts under .local/gpu_terminal/sources
            root = Path(tmp) / ".local" / "gpu_terminal" / "sources"
            (root / "kilix").mkdir(parents=True)
            (root / "kilix" / "VERSION").write_text("0.1.8\n")
            env = {"HOME": tmp, "GPU_TERMINAL_SOURCE_HOME": str(root)}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                    redirect_stdout(out):
                module.main(["--version"])
        self.assertIn("0.1.8", out.getvalue())
        self.assertNotIn("kilix           not present", out.getvalue())


if __name__ == "__main__":
    unittest.main()
