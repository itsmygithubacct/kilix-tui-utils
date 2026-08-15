"""The network tool keeps its boundary: show, up, confirmed down, nmcli only.

The decision under test: the canonical tool owns the everyday surface —
links, saved connections, up and down — while creating connections and
entering secrets stay in nmtui. So the suite pins the terse parser, the
read-only degradation without NetworkManager, the confirmation on the one
action that can cut the session it is typed into, and that nothing but
`nmcli` is ever executed.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import app  # noqa: E402

DEVICES = (
    "eth0:ethernet:connected:Wired connection 1\n"
    "wlan0:wifi:disconnected:\n"
    "lo:loopback:unmanaged:\n"
)
CONNECTIONS = (
    "Wired connection 1:aaaa-1111:802-3-ethernet:eth0\n"
    "Home Wi-Fi:bbbb-2222:802-11-wireless:\n"
)


def load():
    path = ROOT / "tools" / "network" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_network", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_state(module, *, control="nmcli"):
    with mock.patch.object(module, "control", return_value=control), \
         mock.patch.object(module, "_nmcli",
                           side_effect=lambda args: DEVICES
                           if args[-1] == "device" else CONNECTIONS):
        return module.State()


class TerseParserTests(unittest.TestCase):
    def test_plain_fields_split_on_colons(self):
        module = load()
        self.assertEqual(module.split_terse("eth0:ethernet:connected:x"),
                         ["eth0", "ethernet", "connected", "x"])

    def test_escaped_colons_stay_inside_their_field(self):
        # An SSID may contain a colon; terse mode escapes it as `\:`.
        module = load()
        self.assertEqual(module.split_terse(r"Cafe\: upstairs:wifi"),
                         ["Cafe: upstairs", "wifi"])

    def test_escaped_backslash_is_one_literal_backslash(self):
        module = load()
        self.assertEqual(module.split_terse(r"a\\b:c"), ["a\\b", "c"])


class ParseTests(unittest.TestCase):
    def test_devices_are_parsed_and_loopback_is_excluded(self):
        module = load()
        with mock.patch.object(module, "_nmcli", return_value=DEVICES):
            rows = module.devices()
        self.assertEqual([row["device"] for row in rows], ["eth0", "wlan0"])
        self.assertEqual(rows[0]["connection"], "Wired connection 1")
        self.assertEqual(rows[1]["state"], "disconnected")

    def test_connections_mark_active_by_carrying_device(self):
        module = load()
        with mock.patch.object(module, "_nmcli", return_value=CONNECTIONS):
            rows = module.connections()
        self.assertEqual([row["active"] for row in rows], [True, False])
        self.assertEqual(rows[1]["uuid"], "bbbb-2222")

    def test_a_failed_query_is_an_empty_list_not_an_error(self):
        module = load()
        with mock.patch.object(module, "_nmcli", return_value=""):
            self.assertEqual(module.devices(), [])
            self.assertEqual(module.connections(), [])


class DegradationTests(unittest.TestCase):
    """Without NetworkManager the same place still answers, read-only."""

    def test_links_come_from_sys_when_nmcli_is_absent(self):
        module = load()
        with mock.patch.object(module.proc, "network_links",
                               return_value=[("eth0", "up")]):
            state = make_state(module, control=None)
        self.assertEqual(state.devices[0]["device"], "eth0")
        self.assertEqual(state.connections, [])
        text = app.render_to_text(module.render, state)
        self.assertIn("read-only view", text)

    def test_actions_are_neither_offered_nor_taken_without_nmcli(self):
        module = load()
        with mock.patch.object(module.proc, "network_links",
                               return_value=[("eth0", "up")]):
            state = make_state(module, control=None)
        text = app.render_to_text(module.render, state)
        self.assertNotIn("Enter up", text)
        with mock.patch.object(module, "_act",
                               side_effect=AssertionError("must not run")):
            module.handle(ord("\n"), state)
            module.handle(ord("d"), state)
        self.assertIsNone(state.confirm)


class ActionTests(unittest.TestCase):
    def test_enter_brings_a_device_up_without_a_confirmation(self):
        module = load()
        state = make_state(module)
        ran = []
        with mock.patch.object(module, "_act",
                               side_effect=lambda argv:
                               ran.append(argv) or "done"), \
             mock.patch.object(module, "devices", return_value=[]), \
             mock.patch.object(module, "connections", return_value=[]):
            module.handle(ord("\n"), state)
        self.assertEqual(ran, [["nmcli", "device", "connect", "eth0"]])

    def test_down_asks_first_and_y_runs_exactly_the_shown_argv(self):
        # Disconnect can cut the SSH session the keystroke arrived on.
        module = load()
        state = make_state(module)
        module.handle(ord("d"), state)
        self.assertIsNotNone(state.confirm)
        expected = ["nmcli", "device", "disconnect", "eth0"]
        self.assertEqual(state.confirm[1], expected)
        text = app.render_to_text(module.render, state)
        self.assertIn("y to proceed", text)
        ran = []
        with mock.patch.object(module, "_act",
                               side_effect=lambda argv:
                               ran.append(argv) or "done"), \
             mock.patch.object(module, "devices", return_value=[]), \
             mock.patch.object(module, "connections", return_value=[]):
            module.handle(ord("y"), state)
        self.assertEqual(ran, [expected])
        self.assertIsNone(state.confirm)

    def test_any_other_key_cancels_the_confirmation(self):
        module = load()
        state = make_state(module)
        module.handle(ord("d"), state)
        with mock.patch.object(module, "_act",
                               side_effect=AssertionError("must not run")):
            module.handle(ord("n"), state)
        self.assertIsNone(state.confirm)
        self.assertIn("cancelled", state.message)

    def test_connections_act_by_uuid_not_display_name(self):
        # Two saved connections can share a name; the uuid cannot collide.
        module = load()
        state = make_state(module)
        state.section = 1
        state.selected = 1
        _label, up = module.up_action(state.section, state.active)
        self.assertEqual(up, ["nmcli", "connection", "up", "uuid",
                              "bbbb-2222"])
        _label, down = module.down_action(state.section, state.active)
        self.assertEqual(down, ["nmcli", "connection", "down", "uuid",
                                "bbbb-2222"])


class ShellTests(unittest.TestCase):
    def test_the_canonical_frame_carries_both_sections(self):
        module = load()
        state = make_state(module)
        lines = app.render_to_text(module.render, state).splitlines()
        self.assertIn("KILIX TUI", lines[0])
        self.assertIn("▶1 Devices", lines[1])
        self.assertIn("2 Connections", lines[1])

    def test_sections_switch_by_digit_and_tab(self):
        module = load()
        state = make_state(module)
        module.handle(ord("2"), state)
        self.assertEqual(state.section, 1)
        text = app.render_to_text(module.render, state)
        self.assertIn("saved connections", text)
        self.assertIn("*", text)                     # the active one is marked
        module.handle(ord("\t"), state)
        self.assertEqual(state.section, 0)

    def test_quit_key_exits_and_confirmation_swallows_it_first(self):
        module = load()
        state = make_state(module)
        module.handle(ord("d"), state)
        self.assertTrue(module.handle(ord("q"), state))   # cancels, stays
        self.assertFalse(module.handle(ord("q"), state))  # now quits


class SafetyTests(unittest.TestCase):
    def test_the_tool_executes_nothing_but_nmcli(self):
        # Same AST walk as the package viewer's guarantee: every list literal
        # handed to subprocess starts with nmcli, so no shell and no editor
        # can be reached from here.
        import ast
        tree = ast.parse((ROOT / "tools/network/main.py").read_text())
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in ("run", "Popen", "call", "check_output",
                            "check_call"):
                continue
            for argument in node.args:
                if isinstance(argument, (ast.List, ast.Tuple)) \
                        and argument.elts:
                    first = argument.elts[0]
                    if isinstance(first, ast.Constant) \
                            and isinstance(first.value, str):
                        found.add(first.value)
        self.assertEqual(found, {"nmcli"})

    def test_every_action_argv_is_nmcli_with_a_known_verb(self):
        module = load()
        state = make_state(module)
        for section in (0, 1):
            state.section = section
            state.selected = 0
            for build in (module.up_action, module.down_action):
                _label, argv = build(section, state.active)
                self.assertEqual(argv[0], "nmcli")
                self.assertIn(argv[1], ("device", "connection"))


if __name__ == "__main__":
    unittest.main()
