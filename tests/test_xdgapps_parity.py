"""The vendored discovery module is a byte-identical mirror of the SDK's.

`src/kilix_tui/xdgapps.py` is authored in the Kilix host SDK
(`config/kilix_sdk/xdgapps.py`) and carried here byte-for-byte so this
repository installs and tests standalone. This gate is what keeps the copy a
mirror instead of a fork: whenever a Kilix git checkout is present the two
files must match exactly — matched against Kilix's *committed* HEAD, the same
discipline as `tools/sync_xdgapps.py`, so a host mid-edit cannot fail this
suite. On a machine with no Kilix checkout the test skips cleanly; the mirror
is then simply the shipped code.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_sync_tool():
    spec = importlib.util.spec_from_file_location(
        "sync_xdgapps", ROOT / "tools" / "sync_xdgapps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class XdgappsParityTests(unittest.TestCase):
    def test_mirror_is_byte_identical_to_the_sdk_module(self):
        sync = load_sync_tool()
        kilix_home = sync.resolve_kilix()
        if kilix_home is None:
            self.skipTest(
                "no Kilix git checkout found (set KILIX_HOME to check parity)")
        try:
            expected = sync.committed_sdk_bytes(kilix_home)
        except sync.SyncError as error:
            self.skipTest(
                f"Kilix checkout predates the SDK scanner: {error}")
        mirror = (ROOT / "src" / "kilix_tui" / "xdgapps.py").read_bytes()
        self.assertTrue(
            mirror == expected,
            "src/kilix_tui/xdgapps.py differs from kilix's committed "
            f"{sync.SDK_RELATIVE}. If the SDK side is newer, run "
            "tools/sync_xdgapps.py --write; if the mirror is newer, land the "
            "change in Kilix first — the SDK file is the authored one. An "
            f"outdated Kilix checkout ({kilix_home}) fails the same way.")


if __name__ == "__main__":
    unittest.main()
