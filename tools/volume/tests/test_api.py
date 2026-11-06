"""The volume library reads and writes through a fake pactl or wpctl."""

from __future__ import annotations

import subprocess
import sys
import unittest

import support
import kilix_volume
from kilix_volume import control
from kilix_volume.control import VolumeError


class ParseTests(unittest.TestCase):
    def test_pactl_uses_the_first_percent_and_ignores_base_volume(self) -> None:
        sinks = control.parse_pactl_sinks(
            support.PACTL_LISTING.format(mute="no"),
            "alsa_output.speakers\n",
        )
        self.assertEqual(
            [(sink.name, sink.description, sink.percent, sink.muted, sink.default)
             for sink in sinks],
            [
                ("alsa_output.speakers", "Speakers", 42, False, True),
                ("alsa_output.hdmi", "HDMI", 100, True, False),
            ],
        )

    def test_wpctl_keeps_audio_sinks_and_joins_node_names(self) -> None:
        sinks = control.parse_wpctl_status(support.WPCTL_STATUS, support.WPCTL_NAMES)
        self.assertEqual([sink.target for sink in sinks], ["48", "49"])
        self.assertEqual(sinks[0].name, "alsa_output.speakers")
        self.assertEqual(sinks[0].description, "Speakers")
        self.assertEqual(sinks[0].percent, 42)
        self.assertFalse(sinks[0].muted)
        self.assertTrue(sinks[0].default)
        self.assertEqual(sinks[1].name, "alsa_output.hdmi")
        self.assertEqual(sinks[1].description, "HDMI")
        self.assertEqual(sinks[1].percent, 100)
        self.assertTrue(sinks[1].muted)
        self.assertFalse(sinks[1].default)

    def test_wpctl_without_names_uses_the_description(self) -> None:
        sinks = control.parse_wpctl_status(support.WPCTL_STATUS)
        self.assertEqual(sinks[0].name, "Speakers")
        self.assertEqual(sinks[0].description, "Speakers")

    def test_ascii_wpctl_tree_parses_the_same_sinks(self) -> None:
        sinks = control.parse_wpctl_status(support.ASCII_STATUS)
        self.assertEqual(
            [(sink.target, sink.description, sink.percent, sink.muted, sink.default)
             for sink in sinks],
            [("48", "Speakers", 42, False, True), ("49", "HDMI", 100, True, False)],
        )

    def test_linear_volume_rounds_half_up(self) -> None:
        self.assertEqual(control._percent_from_linear("0.42"), 42)
        self.assertEqual(control._percent_from_linear("1"), 100)
        self.assertEqual(control._percent_from_linear("1.5"), 150)
        self.assertEqual(control._percent_from_linear("0.425"), 43)
        self.assertEqual(control._percent_from_linear("0"), 0)


class PactlCommandTests(unittest.TestCase):
    def test_get_returns_the_default_sink(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            sink = kilix_volume.get()
            self.assertEqual(sink.name, "alsa_output.speakers")
            self.assertEqual(sink.description, "Speakers")
            self.assertEqual(sink.percent, 42)
            self.assertIs(sink.muted, False)
            self.assertTrue(sink.default)
            self.assertTrue(all(call[0] == "pactl" for call in fake.names()))

    def test_set_clamps_to_the_mixer_range(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            kilix_volume.set(30)
            kilix_volume.set(200)
            kilix_volume.set(-4)
            kilix_volume.set(0)
            kilix_volume.set(150)
            self.assertEqual(fake.names(), [
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "30%"],
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "150%"],
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "0%"],
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "0%"],
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "150%"],
            ])

    def test_mute_and_unmute_set_a_direction(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            kilix_volume.mute()
            kilix_volume.unmute()
            self.assertEqual(fake.names(), [
                ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"],
                ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
            ])

    def test_sinks_marks_the_default(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            sinks = kilix_volume.sinks()
            self.assertEqual([sink.default for sink in sinks], [True, False])
            self.assertEqual(sinks[1].name, "alsa_output.hdmi")
            self.assertTrue(all(call[0] == "pactl" for call in fake.names()))

    def test_a_refused_server_raises(self) -> None:
        with support.FakeBin({"pactl": support.FAIL_BODY}):
            with self.assertRaises(VolumeError) as caught:
                kilix_volume.get()
            self.assertIn("Connection refused", str(caught.exception))

    def test_a_non_integer_is_refused_before_a_write(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            with self.assertRaises(VolumeError):
                kilix_volume.set(True)  # type: ignore[arg-type]
            with self.assertRaises(VolumeError):
                kilix_volume.set(30.5)  # type: ignore[arg-type]
            self.assertEqual(fake.names(), [])


class WpctlCommandTests(unittest.TestCase):
    def test_get_set_mute_and_unmute_use_wpctl(self) -> None:
        with support.FakeBin({"wpctl": support.wpctl_body()}) as fake:
            sink = kilix_volume.get()
            self.assertEqual(sink.name, "alsa_output.speakers")
            self.assertEqual(sink.description, "Speakers")
            self.assertEqual(sink.percent, 42)
            self.assertIs(sink.muted, False)
            self.assertEqual(sink.target, "48")
            fake.reset()
            kilix_volume.set(30)
            kilix_volume.mute()
            kilix_volume.unmute()
            self.assertEqual(fake.names(), [
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "30%"],
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            ])

    def test_a_failed_name_lookup_keeps_the_description(self) -> None:
        with support.FakeBin({"wpctl": support.wpctl_body(names=False)}) as fake:
            sink = kilix_volume.get()
            self.assertEqual(sink.name, "Speakers")
            self.assertEqual(sink.description, "Speakers")
            self.assertEqual(sink.percent, 42)
            self.assertTrue(any(call[1:] == ["status", "--name"] for call in fake.names()))

    def test_sinks_skip_sources_filters_and_video(self) -> None:
        with support.FakeBin({"wpctl": support.wpctl_body()}) as fake:
            sinks = kilix_volume.sinks()
            self.assertEqual([sink.name for sink in sinks], [
                "alsa_output.speakers", "alsa_output.hdmi",
            ])
            self.assertEqual([sink.default for sink in sinks], [True, False])
            self.assertTrue(all(call[0] == "wpctl" for call in fake.names()))


class SelectionTests(unittest.TestCase):
    def test_pactl_wins_when_both_are_installed(self) -> None:
        with support.FakeBin({
            "pactl": support.pactl_body(),
            "wpctl": support.wpctl_body(),
        }) as fake:
            sink = kilix_volume.get()
            self.assertEqual(sink.percent, 42)
            self.assertTrue(fake.names())
            self.assertTrue(all(call[0] == "pactl" for call in fake.names()))

    def test_neither_control_fails_without_running_a_command(self) -> None:
        with support.FakeBin({}) as fake:
            for action in (
                kilix_volume.get,
                kilix_volume.mute,
                kilix_volume.unmute,
                kilix_volume.sinks,
            ):
                with self.assertRaises(VolumeError) as caught:
                    action()
                message = str(caught.exception)
                self.assertIn("pactl", message)
                self.assertIn("wpctl", message)
                self.assertNotIn("pulsemixer", message.lower())
                self.assertNotIn("alsamixer", message.lower())
            with self.assertRaises(VolumeError) as caught:
                kilix_volume.set(10)
            self.assertIn("pactl", str(caught.exception))
            self.assertEqual(fake.names(), [])

    def test_only_pactl_and_wpctl_are_invoked(self) -> None:
        files = [
            support.TOOL / "kilix_volume" / "control.py",
            support.TOOL / "kilix_volume" / "cli.py",
            support.TOOL / "kilix_volume" / "__init__.py",
            support.TOOL / "main.py",
        ]
        found: set[str] = set()
        for path in files:
            found |= support.command_literals(path)
        self.assertEqual(found, {"pactl", "wpctl"})
        for path in files[:3]:
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("pulsemixer", text)
            self.assertNotIn("alsamixer", text)


class ImportTests(unittest.TestCase):
    def test_importing_the_library_does_not_query_audio_or_curses(self) -> None:
        with support.FakeBin({
            "pactl": support.FAIL_BODY,
            "wpctl": support.FAIL_BODY,
        }) as fake:
            env = fake.child_env()
            env["PYTHONPATH"] = str(support.TOOL)
            result = subprocess.run(
                [sys.executable, "-c",
                 "import sys\n"
                 "import kilix_volume\n"
                 "import kilix_volume.cli\n"
                 "import kilix_volume.control\n"
                 "raise SystemExit(1 if 'curses' in sys.modules else 0)\n"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fake.names(), [])

    def test_importing_the_tool_does_not_query_audio(self) -> None:
        with support.FakeBin({
            "pactl": support.FAIL_BODY,
            "wpctl": support.FAIL_BODY,
        }) as fake:
            result = subprocess.run(
                [sys.executable, "-c",
                 "import importlib.util, sys\n"
                 "path = sys.argv[1]\n"
                 "spec = importlib.util.spec_from_file_location('volume_import', path)\n"
                 "module = importlib.util.module_from_spec(spec)\n"
                 "sys.modules[spec.name] = module\n"
                 "spec.loader.exec_module(module)\n"
                 "print('imported')\n",
                 str(support.TOOL / "main.py")],
                env=fake.child_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imported", result.stdout)
            self.assertEqual(fake.names(), [])


if __name__ == "__main__":
    unittest.main()
