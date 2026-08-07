"""kilix-cameras: the menu, the validation, and the secret-file discipline."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "cameras"))

import main as cameras  # noqa: E402
from kilix_tui import app  # noqa: E402

SAMPLE = """\
# a comment
[camera "front"]
main = rtsp://user:secret1@192.0.2.10:554/stream1
sub  = rtsp://user:secret2@192.0.2.10:554/stream2

[camera "garage"]
sub  = rtsp://user:secret3@192.0.2.12:10554/tcp/av0_1

[group "outside"]
cameras = front, garage
"""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config", "cameras.conf")

    def write(self, text=SAMPLE):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(self.path, 0o600)

    def test_missing_file_is_an_empty_menu_not_an_error(self):
        self.assertEqual(cameras.load(self.path), ([], []))

    def test_load_reads_names_and_tiers_but_never_urls(self):
        self.write()
        found_cameras, found_groups = cameras.load(self.path)
        self.assertEqual(found_cameras, [
            cameras.Camera("front", main=True, sub=True),
            cameras.Camera("garage", main=False, sub=True),
        ])
        self.assertEqual(found_groups, [
            cameras.Group("outside", ("front", "garage")),
        ])

    def test_add_camera_appends_a_stanza_kilix_rtsp_can_read(self):
        cameras.add_camera(self.path, "side",
                           "rtsp://u:p@192.0.2.13/stream1",
                           "rtsp://u:p@192.0.2.13/stream2")
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('[camera "side"]', text)
        self.assertIn("main = rtsp://u:p@192.0.2.13/stream1", text)
        self.assertIn("sub  = rtsp://u:p@192.0.2.13/stream2", text)
        found, _groups = cameras.load(self.path)
        self.assertEqual(found, [cameras.Camera("side", True, True)])

    def test_add_camera_creates_a_private_file_from_the_first_byte(self):
        cameras.add_camera(self.path, "side", "rtsp://u:p@h/1", "")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        parent = stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode)
        self.assertEqual(parent & 0o077, 0)

    def test_add_camera_repairs_a_loose_existing_file(self):
        self.write()
        os.chmod(self.path, 0o644)
        cameras.add_camera(self.path, "side", "", "rtsp://u:p@h/2")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_add_camera_refuses_what_kilix_rtsp_would_refuse(self):
        self.write()
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "front", "rtsp://h/1", "")
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "outside", "rtsp://h/1", "")
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "new", "", "")
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "new", "http://h/1", "")
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, 'a"b', "rtsp://h/1", "")
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "x" * 64, "rtsp://h/1", "")
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "new", "rtsp://h/" + "1" * 512, "")
        # Nothing partial was written.
        found, _groups = cameras.load(self.path)
        self.assertEqual([camera.name for camera in found], ["front", "garage"])

    def test_add_camera_enforces_the_camera_count_limit(self):
        self.write("".join(f'[camera "c{index}"]\nsub = rtsp://h/{index}\n\n'
                           for index in range(cameras.CAMERAS_MAX)))
        with self.assertRaises(ValueError):
            cameras.add_camera(self.path, "overflow", "rtsp://h/x", "")


class ValidationTests(unittest.TestCase):
    def test_name_error(self):
        self.assertIsNone(cameras.name_error("front", set()))
        self.assertIsNotNone(cameras.name_error("", set()))
        self.assertIsNotNone(cameras.name_error(" front", set()))
        self.assertIsNotNone(cameras.name_error("a]b", set()))
        self.assertIsNotNone(cameras.name_error("front", {"front"}))

    def test_url_error(self):
        self.assertIsNone(cameras.url_error(""))
        self.assertIsNone(cameras.url_error("rtsp://u:p@host:554/stream1"))
        self.assertIsNotNone(cameras.url_error("rtsp://host/has space"))
        self.assertIsNotNone(cameras.url_error("http://host/1"))


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_home = os.environ.get("KILIX_RTSP_HOME")
        os.environ["KILIX_RTSP_HOME"] = self.tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self.old_home is None:
            os.environ.pop("KILIX_RTSP_HOME", None)
        else:
            os.environ["KILIX_RTSP_HOME"] = self.old_home

    def write(self, text=SAMPLE):
        path = os.path.join(self.tmp.name, "config", "cameras.conf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)

    def test_empty_state_guides_to_the_config_routine(self):
        frame = app.render_to_text(cameras.render, cameras.State())
        self.assertIn("No cameras configured yet", frame)
        self.assertIn("New camera profile", frame)

    def test_menu_lists_views_mosaics_and_the_profile_routine(self):
        self.write()
        state = cameras.State()
        state.command = ["/bin/echo"]          # a stand-in for kilix-rtsp
        frame = app.render_to_text(cameras.render, state)
        self.assertIn("front", frame)
        self.assertIn("main+sub", frame)
        self.assertIn("garage", frame)
        self.assertIn("sub only", frame)
        self.assertIn("mosaic: outside", frame)
        self.assertIn("mosaic: everything", frame)
        self.assertIn("New camera profile", frame)

    def test_rows_build_kilix_rtsp_argv(self):
        self.write()
        state = cameras.State()
        state.command = ["/usr/bin/kilix-rtsp"]
        argv = {row.label: row.argv for row in state.rows()}
        self.assertEqual(argv["front"], ("/usr/bin/kilix-rtsp", "view", "front"))
        self.assertEqual(argv["mosaic: outside"],
                         ("/usr/bin/kilix-rtsp", "mosaic", "outside"))
        self.assertEqual(argv["mosaic: everything"],
                         ("/usr/bin/kilix-rtsp", "mosaic"))
        self.assertEqual(argv["New camera profile"], ())

    def test_without_kilix_rtsp_views_are_disabled_with_a_reason(self):
        self.write()
        state = cameras.State()
        state.command = None
        rows = state.rows()
        self.assertIsNone(rows[0].argv)
        self.assertIn("kilix-rtsp", rows[0].reason)
        frame = app.render_to_text(cameras.render, state)
        self.assertIn("needs kilix-rtsp installed", frame)

    def test_credentials_never_reach_the_frame(self):
        self.write()
        frame = app.render_to_text(cameras.render, cameras.State())
        for secret in ("secret1", "secret2", "secret3", "rtsp://"):
            self.assertNotIn(secret, frame)

    def test_handle_moves_and_quits(self):
        self.write()
        state = cameras.State()
        self.assertTrue(cameras.handle(ord("j"), state))
        self.assertEqual(state.selected, 1)
        self.assertFalse(cameras.handle(ord("q"), state))

    def test_enter_on_a_disabled_row_explains_instead_of_launching(self):
        self.write()
        state = cameras.State()
        state.command = None
        cameras.handle(ord("\n"), state)
        self.assertIn("kilix-rtsp", state.status)


if __name__ == "__main__":
    unittest.main()
