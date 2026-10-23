"""The normal file-manager entry point must reach its interactive callback."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools/file')]
import main as files


class EntryTests(unittest.TestCase):
    def test_normal_launch_reaches_interactive_application(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(files.app,'run',return_value=0) as run:
            self.assertEqual(files.main([directory]),0)
            self.assertTrue(callable(run.call_args.kwargs['handle']))
            self.assertIs(run.call_args.kwargs['handle'],files.handle)

    def test_screenshot_mode_remains_noninteractive(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(files.app,'run') as run:
            output=Path(directory)/'frame.txt'
            self.assertEqual(files.main([directory,'--screenshot',str(output)]),0)
            self.assertIn('Files',output.read_text())
            run.assert_not_called()


if __name__=='__main__':unittest.main()
