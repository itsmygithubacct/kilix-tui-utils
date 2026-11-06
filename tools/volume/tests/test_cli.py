"""kilix-volume commands print and exit without opening the mixer."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support


def run_tool(fake: support.FakeBin, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(support.TOOL / "main.py"), *argv],
        env=fake.child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )


def load_tool():
    name = "kilix_volume_tool_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, support.TOOL / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CliTests(unittest.TestCase):
    def test_get_prints_one_line(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            result = run_tool(fake, "get")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "42%\n")
            self.assertEqual(result.stderr, "")

    def test_get_prints_muted(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body("yes")}) as fake:
            result = run_tool(fake, "get")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "42% muted\n")

    def test_wpctl_set_mute_and_unmute_are_silent(self) -> None:
        with support.FakeBin({"wpctl": support.wpctl_body()}) as fake:
            for argv in (["set", "30"], ["mute"], ["unmute"]):
                result = run_tool(fake, *argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
            self.assertEqual(fake.names(), [
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "30%"],
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            ])

    def test_signed_integers_are_clamped(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            for text in ("+30", "-2", "150"):
                result = run_tool(fake, "set", text)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
            self.assertEqual(fake.names(), [
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "30%"],
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "0%"],
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "150%"],
            ])

    def test_sinks_marks_the_default(self) -> None:
        with support.FakeBin({"pactl": support.pactl_body()}) as fake:
            result = run_tool(fake, "sinks")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                "* 42%  Speakers  alsa_output.speakers",
                "  100% muted  HDMI  alsa_output.hdmi",
            ])

    def test_wpctl_sinks_marks_the_default(self) -> None:
        with support.FakeBin({"wpctl": support.wpctl_body()}) as fake:
            result = run_tool(fake, "sinks")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                "* 42%  Speakers  alsa_output.speakers",
                "  100% muted  HDMI  alsa_output.hdmi",
            ])

    def test_missing_control_exits_1(self) -> None:
        with support.FakeBin({}) as fake:
            for argv in (["get"], ["set", "30"], ["mute"], ["unmute"], ["sinks"]):
                result = run_tool(fake, *argv)
                self.assertEqual(result.returncode, 1, (argv, result.stderr))
                self.assertEqual(result.stdout, "")
                self.assertIn("pactl", result.stderr)
                self.assertIn("wpctl", result.stderr)
                self.assertNotIn("pulsemixer", result.stderr.lower())
                self.assertNotIn("alsamixer", result.stderr.lower())
            self.assertEqual(fake.names(), [])

    def test_a_non_integer_volume_exits_2(self) -> None:
        with support.FakeBin({
            "pactl": support.pactl_body(),
            "wpctl": support.wpctl_body(),
        }) as fake:
            for argv in (
                ["set"],
                ["set", "30.5"],
                ["set", "30%"],
                ["set", "abc"],
                ["set", "30", "1"],
                ["set", "--"],
                ["get", "extra"],
                ["mute", "on"],
            ):
                result = run_tool(fake, *argv)
                self.assertEqual(result.returncode, 2, (argv, result.stderr))
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith("kilix-volume:"), result.stderr)
            self.assertEqual(fake.names(), [])

    def test_a_refused_server_exits_1(self) -> None:
        with support.FakeBin({"pactl": support.FAIL_BODY}) as fake:
            result = run_tool(fake, "set", "30")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("Connection refused", result.stderr)

    def test_no_default_sink_exits_1_and_sinks_can_be_empty(self) -> None:
        with support.FakeBin({"pactl": support.EMPTY_PACTL_BODY}) as fake:
            listed = run_tool(fake, "sinks")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(listed.stdout, "")
            missing = run_tool(fake, "get")
            self.assertEqual(missing.returncode, 1)
            self.assertIn("No default audio sink", missing.stderr)
            self.assertEqual(missing.stdout, "")


class MixerTests(unittest.TestCase):
    def test_bare_and_popup_flags_open_the_mixer(self) -> None:
        tool = load_tool()
        with support.FakeBin({"pactl": support.pactl_body()}), \
                mock.patch.object(tool.app, "run", return_value=0) as run, \
                mock.patch.object(tool.curses, "wrapper",
                                  side_effect=AssertionError("curses started")):
            for argv, mode in (
                ([], "full"),
                (["--compact"], "compact"),
                (["--settings"], "settings"),
                (["--full"], "full"),
            ):
                self.assertEqual(tool.main(argv), 0, argv)
                self.assertEqual(run.call_args.args[1].mode, mode)
                self.assertIs(run.call_args.kwargs["mouse"], True)

    def test_commands_do_not_open_the_mixer(self) -> None:
        tool = load_tool()
        with support.FakeBin({"pactl": support.pactl_body()}), \
                mock.patch.object(tool.app, "run",
                                  side_effect=AssertionError("mixer")), \
                mock.patch.object(tool.curses, "wrapper",
                                  side_effect=AssertionError("curses started")):
            output = io.StringIO()
            with redirect_stdout(output):
                code = tool.main(["get"])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "42%\n")
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(tool.main(["set", "nope"]), 2)
            self.assertIn("integer", errors.getvalue())

    def test_screenshot_modes_render_without_curses(self) -> None:
        with support.FakeBin({"wpctl": support.wpctl_body()}) as fake, \
                tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ([], "frame.txt", ("Speakers", "HDMI")),
                (["--compact"], "compact.txt", ("Quick adjustment", "Speakers")),
                (["--settings"], "settings.txt",
                 ("Volume settings", "Open full volume control")),
                (["--full"], "full.txt", ("Speakers", "HDMI")),
            )
            for flags, name, needles in cases:
                target = root / name
                result = run_tool(fake, *flags, "--screenshot", str(target))
                self.assertEqual(result.returncode, 0, result.stderr)
                frame = target.read_text(encoding="utf-8")
                for needle in needles:
                    self.assertIn(needle, frame, name)
            self.assertTrue(any(call[0] == "wpctl" for call in fake.names()))

    def test_mixer_writes_through_wpctl_when_pactl_is_absent(self) -> None:
        tool = load_tool()
        with support.FakeBin({"wpctl": support.wpctl_body()}) as fake:
            state = tool.State()
            self.assertEqual(state.control, "wpctl")
            self.assertEqual(state.sinks[0]["description"], "Speakers")
            self.assertEqual(state.sinks[0]["volume"], 42)
            self.assertEqual(state.sinks[0]["name"], "48")
            self.assertTrue(state.sinks[0]["default"])
            self.assertEqual(state.sinks[1]["volume"], 100)
            self.assertTrue(state.sinks[1]["muted"])
            fake.reset()
            tool.set_volume("48", 180)
            tool.toggle_mute("48")
            tool.make_default("49")
            self.assertEqual(fake.names(), [
                ["wpctl", "set-volume", "48", "150%"],
                ["wpctl", "set-mute", "48", "toggle"],
                ["wpctl", "set-default", "49"],
            ])


if __name__ == "__main__":
    unittest.main()
