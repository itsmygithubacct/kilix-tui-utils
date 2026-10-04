#!/usr/bin/env python3
"""Ask the shipped Kilix documentation lookup and show its cited excerpts."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from kilix_desk import registry  # noqa: E402


def main(argv=None, *, ask=input, run=None, output=None, errors=None):
    import subprocess

    argv = list(sys.argv[1:] if argv is None else argv)
    run = subprocess.run if run is None else run
    output = sys.stdout if output is None else output
    errors = sys.stderr if errors is None else errors
    interactive = not argv
    try:
        question = " ".join(argv) if argv else ask("Help question: ")
    except (EOFError, KeyboardInterrupt):
        print(file=output)
        return 0
    question = question.strip()
    if not question or len(question) > 2000:
        print("Enter a help question of 1–2000 characters.", file=errors)
        return 2
    launcher = registry.kilix_command()
    if launcher is None:
        print("Kilix is unavailable; install or select the Kilix launcher.", file=errors)
        return 69
    try:
        result = run([*launcher, "help-search", "-k", "5", "--full", "--", question],
                     check=False)
    except OSError as error:
        print(f"Could not launch Kilix help search: {error}", file=errors)
        return 69
    if interactive:
        try:
            ask("\nPress Enter to return to the desktop…")
        except (EOFError, KeyboardInterrupt):
            print(file=output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
