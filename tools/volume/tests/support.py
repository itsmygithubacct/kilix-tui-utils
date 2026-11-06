"""Fake pactl and wpctl binaries for the volume tests."""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
ROOT = TOOL.parents[1]
sys.path.insert(0, str(TOOL))
sys.path.insert(0, str(ROOT / "src"))

PACTL_LISTING = """\
Sink #1
	State: RUNNING
	Name: alsa_output.speakers
	Description: Speakers
	Mute: {mute}
	Volume: front-left: 27524 /  42% / -22.62 dB,   front-right: 27524 /  99% / -0.26 dB
	        balance 0.00
	Base Volume: 65536 / 100% / 0.00 dB
Sink #2
	State: SUSPENDED
	Name: alsa_output.hdmi
	Description: HDMI
	Mute: yes
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
	        balance 0.00
	Base Volume: 65536 / 100% / 0.00 dB
"""

WPCTL_STATUS = """\
PipeWire 'pipewire-0' [1.4.2, pleb@host, cookie:1]
 └─ Clients:
        33. WirePlumber                         [1.4.2, pleb@host, pid:1]
Audio
 ├─ Devices:
 │      42. Built-in Audio                      [alsa]
 ├─ Sinks:
 │  *   48. Speakers                            [vol: 0.42]
 │      49. HDMI                                [vol: 1.00 MUTED]
 ├─ Sources:
 │  *   50. Microphone                          [vol: 0.80]
 ├─ Filters:
 │      51. Easy Effects                        [vol: 0.10]
 └─ Streams:
Video
 ├─ Devices:
 │      60. Integrated Camera                   [v4l2]
 ├─ Sinks:
 │      61. Video Sink                          [vol: 0.20]
 ├─ Sources:
 │  *   44. Integrated Camera (V4L2)
 └─ Streams:
Settings
 └─ Default Configured Devices:
"""

WPCTL_NAMES = """\
Audio
 ├─ Sinks:
 │  *   48. alsa_output.speakers                [vol: 0.42]
 │      49. alsa_output.hdmi                    [vol: 1.00 MUTED]
Video
 ├─ Sinks:
 │      61. video_sink                          [vol: 0.20]
"""

ASCII_STATUS = """\
Audio
 |- Devices:
 |      42. Built-in Audio [alsa]
 |- Sinks:
 |  *   48. Speakers [vol: 0.42]
 |      49. HDMI [vol: 1.00 MUTED]
 |- Sources:
 |  *   50. Microphone [vol: 0.80]
Video
 |- Sinks:
 |      61. Video Sink [vol: 0.20]
"""

_PREAMBLE = "\n".join([
    "import json, os, sys",
    "from pathlib import Path",
    "with Path(os.environ['KILIX_VOLUME_FAKE_LOG']).open('a', encoding='utf-8') as handle:",
    "    handle.write(json.dumps(sys.argv) + chr(10))",
    "",
])


def pactl_body(mute: str = "no") -> str:
    listing = PACTL_LISTING.format(mute=mute)
    return f"""
args = sys.argv[1:]
if args == ["get-default-sink"]:
    print("alsa_output.speakers")
elif args == ["list", "sinks"]:
    sys.stdout.write({listing!r})
elif args[:1] in (["set-sink-volume"], ["set-sink-mute"], ["set-default-sink"]):
    pass
else:
    sys.stderr.write("unexpected pactl command\\n")
    raise SystemExit(1)
"""


def wpctl_body(*, names: bool = True) -> str:
    name_branch = (
        f"sys.stdout.write({WPCTL_NAMES!r})"
        if names else
        "sys.stderr.write('unknown option')\n    raise SystemExit(1)"
    )
    return f"""
args = sys.argv[1:]
if args == ["status"]:
    sys.stdout.write({WPCTL_STATUS!r})
elif args == ["status", "--name"]:
    {name_branch}
elif args[:1] in (["set-volume"], ["set-mute"], ["set-default"]):
    pass
else:
    sys.stderr.write("unexpected wpctl command\\n")
    raise SystemExit(1)
"""


FAIL_BODY = """
sys.stderr.write("Connection failure: Connection refused\\n")
raise SystemExit(1)
"""

EMPTY_PACTL_BODY = """
args = sys.argv[1:]
if args == ["get-default-sink"]:
    print("")
elif args == ["list", "sinks"]:
    print("")
else:
    pass
"""


def command_literals(path: Path) -> set[str]:
    """First string of every list literal passed to a call."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in node.args:
            if isinstance(argument, ast.List) and argument.elts:
                first = argument.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
        for keyword in node.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                if keyword.value.value:
                    found.add("shell=True")
    return found


class FakeBin:
    """A PATH containing only the named fake control commands."""

    def __init__(self, scripts: dict[str, str]) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.log = root / "log"
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in scripts.items():
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{_PREAMBLE}{body}", encoding="utf-8")
            path.chmod(0o755)
        self._patch = mock.patch.dict(os.environ, {
            "PATH": str(self.bin),
            "KILIX_VOLUME_FAKE_LOG": str(self.log),
        })

    def __enter__(self) -> "FakeBin":
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._patch.stop()
        self._tmp.cleanup()
        return False

    def names(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows: list[list[str]] = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            argv = json.loads(line)
            argv[0] = Path(argv[0]).name
            rows.append(argv)
        return rows

    def reset(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TERM"] = "dumb"
        return env
