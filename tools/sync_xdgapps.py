#!/usr/bin/env python3
"""Keep src/kilix_tui/xdgapps.py a byte-identical mirror of the Kilix SDK's.

The freedesktop discovery module is authored once, in the Kilix host SDK
(config/kilix_sdk/xdgapps.py, SDK 1.9); this repository carries a
byte-for-byte copy so the TUI stack stays standalone-installable and its
tests self-contained. This tool is what makes the copy a mirror instead of a
fork — the same committed-history discipline as kilix-land-desktop's
tools/sync_source_parity.py: the mirror is taken from the source repository's
*committed* history, never its working tree, so a host checkout mid-edit
cannot reach this repository until Kilix itself accepts the change.

Default is check mode: exit nonzero when the mirror is stale. `--write`
rewrites the mirror from Kilix's HEAD. The Kilix checkout is resolved from
`--kilix-home`, then `$KILIX_HOME`, then the same workspace search the rest
of this repository uses (`kilix_desk.sources.component_dir`), and only a git
checkout counts — parity against nothing is not parity.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from kilix_desk import sources  # noqa: E402

SDK_RELATIVE = "config/kilix_sdk/xdgapps.py"
MIRROR = os.path.join(ROOT, "src", "kilix_tui", "xdgapps.py")


class SyncError(Exception):
    """A resolved Kilix checkout cannot supply the committed SDK module."""


def kilix_candidates(explicit: str | None = None) -> list[str]:
    """Every plausible Kilix checkout, best first, duplicates dropped."""
    out: list[str] = []
    for home in (explicit or "", os.environ.get("KILIX_HOME", ""),
                 sources.component_dir("kilix")):
        if not home:
            continue
        path = os.path.abspath(os.path.expanduser(home))
        if path not in out:
            out.append(path)
    return out


def resolve_kilix(explicit: str | None = None) -> str | None:
    """The first candidate that is a git checkout carrying the SDK module.

    None means no usable checkout on this machine — the caller decides
    whether that is an error (`--write` has nothing to copy) or a clean skip
    (the parity test on a standalone install).
    """
    for home in kilix_candidates(explicit):
        if (os.path.exists(os.path.join(home, ".git"))
                and os.path.isfile(
                    os.path.join(home, *SDK_RELATIVE.split("/")))):
            return home
    return None


def committed_sdk_bytes(kilix_home: str) -> bytes:
    """The SDK module exactly as the Kilix checkout's HEAD committed it."""
    result = subprocess.run(
        ["git", "-C", kilix_home, "show", f"HEAD:{SDK_RELATIVE}"],
        capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SyncError(
            f"kilix at {kilix_home} has no committed {SDK_RELATIVE}: {detail}")
    return result.stdout


def mirror_bytes() -> bytes:
    try:
        with open(MIRROR, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mirror kilix_sdk.xdgapps into src/kilix_tui/xdgapps.py")
    parser.add_argument(
        "--kilix-home",
        help="Kilix checkout to mirror from (default: $KILIX_HOME, then the "
             "workspace search)")
    parser.add_argument(
        "--write", action="store_true",
        help="rewrite the mirror from Kilix's committed HEAD")
    args = parser.parse_args()

    kilix_home = resolve_kilix(args.kilix_home)
    if kilix_home is None:
        print("xdgapps parity error: no Kilix git checkout found "
              "(pass --kilix-home or set KILIX_HOME)", file=sys.stderr)
        return 1
    try:
        expected = committed_sdk_bytes(kilix_home)
    except SyncError as error:
        print(f"xdgapps parity error: {error}", file=sys.stderr)
        return 1

    if args.write:
        if mirror_bytes() != expected:
            with open(MIRROR, "wb") as handle:
                handle.write(expected)
        print(f"PASS xdgapps mirror synchronized: kilix={kilix_home}")
        return 0
    if mirror_bytes() != expected:
        print(
            "xdgapps parity error: src/kilix_tui/xdgapps.py differs from "
            f"kilix's committed {SDK_RELATIVE}\n"
            "  if the SDK side is newer: run tools/sync_xdgapps.py --write\n"
            "  if the mirror is newer: land the change in Kilix first — the "
            "SDK file is the authored one\n"
            f"  (an outdated Kilix checkout at {kilix_home} fails the same "
            "way; update it)", file=sys.stderr)
        return 1
    print(f"PASS xdgapps mirror verified: kilix={kilix_home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
