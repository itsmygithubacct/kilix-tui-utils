#!/usr/bin/env python3
"""Point generated launchers at the installed tree."""

import pathlib
import sys


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: rewrite-launchers.py ROOT BINDIR")
    root = sys.argv[1]
    bindir = pathlib.Path(sys.argv[2])
    final = "/usr/share/kilix-tui-utils"
    count = 0
    for path in sorted(bindir.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text()
        if root not in text:
            raise SystemExit(f"{path.name} does not point at the checkout")
        rewritten = text.replace(root, final)
        if "/home/" in rewritten or root in rewritten:
            raise SystemExit(f"{path.name} kept a build path")
        path.write_text(rewritten)
        count += 1
    if count < 20:
        raise SystemExit(f"only {count} launchers were rewritten")
    print(f"rewrite-launchers: {count}")


if __name__ == "__main__":
    main()
