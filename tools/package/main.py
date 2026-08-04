"""kilix-package — a read-only view of installed packages.

Deliberately read-only. Plebian-OS release images pin an apt snapshot so a build
is reproducible; a tool that installed or removed packages would silently drift
a machine off that closure. Viewing carries none of that risk, so this queries
dpkg and never mutates.
"""
from __future__ import annotations

import subprocess

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402

FORMAT = r"${Package}\t${Version}\t${Installed-Size}\t${binary:Summary}\n"


def installed() -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=" + FORMAT.replace(r"\$", "$")],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or not fields[0]:
            continue
        try:
            size = int(fields[2] or 0) * 1024
        except ValueError:
            size = 0
        rows.append({"name": fields[0], "version": fields[1], "size": size,
                     "summary": fields[3] if len(fields) > 3 else ""})
    rows.sort(key=lambda item: item["name"])
    return rows


class State:
    def __init__(self) -> None:
        self.all = installed()
        self.filter = ""
        self.selected = 0
        self.sort_by_size = False

    @property
    def rows(self) -> list[dict[str, object]]:
        rows = [r for r in self.all
                if self.filter.lower() in str(r["name"]).lower()]
        if self.sort_by_size:
            rows = sorted(rows, key=lambda r: r["size"], reverse=True)
        return rows


def render(surface, state: State) -> None:
    rows = state.rows
    total = sum(int(r["size"]) for r in state.all)
    summary = (
        f"{len(state.all)} installed · {proc.human_bytes(total)} on disk"
        f"{f' · filter /{state.filter}' if state.filter else ''}"
        f"{' · largest first' if state.sort_by_size else ''}"
    )
    body = shell.draw(
        surface,
        help_key=False,   # '?' is text here, not help
        title="Packages",
        sections=("Installed",),
        summary=summary,
        footer="type to filter · Tab sort by size · Esc quit",
    )
    if not state.all:
        shell.put(surface, body.top, body.left,
                  "dpkg-query unavailable on this system",
                  shell.tango.attr("alert"))
    visible = max(1, body.height)
    start = max(0, min(state.selected - visible // 2, max(0, len(rows) - visible)))
    for index, item in enumerate(rows[start:start + visible]):
        row = body.top + index
        selected = start + index == state.selected
        marker = "▶" if selected else " "
        shell.put(
            surface, row, body.left,
            f"{marker} {str(item['name']):<28.28} "
            f"{str(item['version']):<18.18} "
            f"{proc.human_bytes(int(item['size'])):>8}  "
            f"{item['summary']}",
            shell.tango.attr("selected") if selected else 0,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        rows = s.rows
        if key == 27:
            return False
        if key in (263, 127, 8):
            s.filter = s.filter[:-1]
        elif key == ord("\t"):
            s.sort_by_size = not s.sort_by_size
        elif (step := keymap.direction(key)) and rows:
            s.selected = max(0, min(len(rows) - 1, s.selected + step))
        elif 32 <= key < 127:
            s.filter += chr(key)
            s.selected = 0
        return True

    # Typing filters the package list, so '?' is text here.
    return app.run(render, state, handle=handle, help_key=False)


if __name__ == "__main__":
    raise SystemExit(main())
