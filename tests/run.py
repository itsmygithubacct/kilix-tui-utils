#!/usr/bin/env python3
"""Run every tool's suite in a fresh subprocess.

Same shape as Kilix 95's runner: one process per file so a tool that leaves
curses or an import in a bad state cannot affect the next one, and so a crash
is attributed to the tool that caused it.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    names = sorted(
        name for name in os.listdir(HERE)
        if name.startswith("test_") and name.endswith(".py")
    )
    if sys.argv[1:]:
        names = [n for n in names if any(a in n for a in sys.argv[1:])]
    failed = []
    for name in names:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, name)],
            capture_output=True, text=True, env=env,
        )
        if result.returncode == 0:
            print(f"PASS  {name}")
        else:
            failed.append(name)
            print(f"FAIL  {name}")
            for line in (result.stdout + result.stderr).splitlines():
                print(f"  {line}")
    print(f"{len(names) - len(failed)}/{len(names)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
