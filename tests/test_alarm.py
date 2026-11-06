"""kilix-alarm: four styles, a clock time, and a snooze that stays on the clock."""
import importlib.util
import os
import sys
import tempfile
import unittest
import wave
from array import array
from datetime import datetime
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "alarm"))

# Loaded by file so this suite does not claim the shared module name `main`.
_SPEC = importlib.util.spec_from_file_location(
    "kilix_alarm_tool",
    ROOT / "tools" / "alarm" / "main.py",
)
alarm_app = importlib.util.module_from_spec(_SPEC)
sys.modules["kilix_alarm_tool"] = alarm_app
_SPEC.loader.exec_module(alarm_app)
from kilix_alarm.model import (  # noqa: E402
    Alarm,
    AlarmError,
    cycle_seconds,
    parse_clock,
    pattern,
)
from kilix_alarm.sound import frames, wav_bytes  # noqa: E402
from kilix_tui import app  # noqa: E402


def _peak(style: str) -> int:
    samples = array("h")
    samples.frombytes(frames(pattern(style)))
    return max(abs(sample) for sample in samples)


class StyleTests(unittest.TestCase):
    def test_hum_is_long_and_quiet_and_alert_is_loud(self):
        hum = pattern("hum")
        self.assertGreaterEqual(hum[0].seconds, 1.5)
        self.assertLess(hum[0].hz, 200)
        self.assertLess(hum[0].amplitude, 0.1)
        self.assertGreater(_peak("alert"), _peak("hum") * 4)
        self.assertGreater(pattern("alert")[0].amplitude, 0.4)
        self.assertEqual(pattern("flash"), ())
        self.assertGreater(cycle_seconds("clock"), 1)

    def test_wav_is_a_quiet_hum_and_not_played_here(self):
        payload = wav_bytes("hum")
        self.assertTrue(payload.startswith(b"RIFF"))
        with wave.open(BytesIO(payload)) as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertGreater(handle.getnframes(), 22050)


class ClockTests(unittest.TestCase):
    def test_clock_text_and_the_next_day(self):
        self.assertEqual(parse_clock("7:30pm"), (19, 30))
        self.assertEqual(parse_clock("12am"), (0, 0))
        self.assertEqual(parse_clock("12:15 pm"), (12, 15))
        with self.assertRaises(AlarmError):
            parse_clock("25:00")
        now = datetime(2026, 11, 6, 18, 0, 0).timestamp()
        alarm = Alarm()
        alarm.arm_clock("19:00", now)
        self.assertEqual(datetime.fromtimestamp(alarm.fire_at).hour, 19)
        self.assertEqual(datetime.fromtimestamp(alarm.fire_at).date(),
                         datetime.fromtimestamp(now).date())
        alarm.arm_clock("7:00am", now)
        self.assertEqual(datetime.fromtimestamp(alarm.fire_at).date(),
                         datetime(2026, 11, 7).date())

    def test_snooze_belongs_to_the_alarm_clock(self):
        now = datetime(2026, 11, 6, 12, 0, 0).timestamp()
        alarm = Alarm(style="clock", snooze_minutes=9)
        alarm.arm_in(1, now)
        self.assertIsNone(alarm.advance(now))
        self.assertEqual(alarm.advance(now + 1), "play")
        self.assertTrue(alarm.snooze(now + 1))
        self.assertEqual(alarm.phase, "snoozed")
        self.assertIsNone(alarm.advance(now + 2))
        self.assertEqual(alarm.advance(now + 1 + 9 * 60), "play")
        quiet = Alarm(style="hum")
        quiet.phase = "ringing"
        self.assertFalse(quiet.snooze(now))
        self.assertEqual(quiet.message, "snooze is part of the alarm clock")

    def test_flash_toggles_and_does_not_ask_for_sound(self):
        now = 1_000.0
        alarm = Alarm(style="flash")
        alarm.arm_in(1, now)
        self.assertEqual(alarm.advance(now + 1), "flash")
        self.assertTrue(alarm.flash_lit)
        self.assertEqual(alarm.advance(now + 1.2), "flash")
        self.assertTrue(alarm.flash_lit)
        self.assertEqual(alarm.advance(now + 1.5), "flash")
        self.assertFalse(alarm.flash_lit)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._old = os.environ.get("KILIX_ALARM_SETTINGS")
        os.environ["KILIX_ALARM_SETTINGS"] = str(Path(self._home.name) / "alarm.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KILIX_ALARM_SETTINGS", None)
        else:
            os.environ["KILIX_ALARM_SETTINGS"] = self._old
        self._home.cleanup()

    def test_the_screen_lists_every_style_and_q_quits(self):
        state = alarm_app.State()
        state.player.argv = None
        frame = app.render_to_text(alarm_app.render, state)
        for label in ("Flashing white", "Quiet hum", "Loud alert", "Alarm clock"):
            self.assertIn(label, frame)
        self.assertIn("KILIX TUI", frame)
        self.assertFalse(alarm_app.handle(ord("q"), state))

    def test_enter_arms_a_typed_time_and_snooze_waits(self):
        state = alarm_app.State()
        state.player.argv = None
        state.alarm.when_text = "7:30pm"
        state.alarm.style = "clock"
        self.assertTrue(alarm_app.handle(ord("\n"), state))
        self.assertEqual(state.alarm.phase, "waiting")
        state.alarm.fire_at = state.now() - 1
        clips = []
        state.player.play = clips.append
        state.advance()
        self.assertEqual(state.alarm.phase, "ringing")
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].startswith(b"RIFF"))
        self.assertTrue(alarm_app.handle(ord("s"), state))
        self.assertEqual(state.alarm.phase, "snoozed")

    def test_white_flash_fills_the_screen(self):
        state = alarm_app.State()
        state.player.argv = None
        state.alarm.style = "flash"
        state.alarm.phase = "ringing"
        state.alarm.flash_lit = True
        surface = app.TextSurface()
        alarm_app.render(surface, state)
        self.assertNotEqual(surface.attrs[0][0], 0)
        self.assertIn("Alarm", str(surface))


if __name__ == "__main__":
    unittest.main()
