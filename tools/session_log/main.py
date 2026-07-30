"""kilix-session-log — browse pane transcripts across both storage tiers.

Kilix keeps recent transcripts uncompressed and archives older ones with zstd.
Which tier a session landed in is an implementation detail, so this lists both
together and opens either without the reader needing to know.
"""
from __future__ import annotations

import subprocess

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402


def transcript_dir() -> str:
    if explicit := os.environ.get("KILIX_TRANSCRIPT_DIR"):
        return explicit
    state = os.environ.get("KILIX_STATE_DIRECTORY") or os.path.join(
        os.path.expanduser("~"), ".local/gpu_terminal/kilix/state")
    return os.path.join(state, "transcripts")


def entries(directory: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for tier, folder, suffix in (("live", directory, ".log"),
                                 ("archived", os.path.join(directory, "archive"),
                                  ".log.zst")):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(suffix):
                continue
            path = os.path.join(folder, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            found.append({
                "id": name[: -len(suffix)], "tier": tier, "path": path,
                "size": info.st_size, "mtime": info.st_mtime,
            })
    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found


def read_transcript(entry: dict[str, object], limit: int = 200000) -> str:
    """Return a transcript's text, decompressing an archived one."""
    path = str(entry["path"])
    if entry["tier"] == "archived":
        try:
            result = subprocess.run(["zstd", "-dcq", "--", path],
                                    capture_output=True, timeout=60, check=False)
            data = result.stdout
        except (OSError, subprocess.SubprocessError):
            return "(zstd is required to read this archived transcript)"
    else:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as error:
            return f"({error})"
    return data[-limit:].decode("utf-8", errors="replace")


class State:
    def __init__(self) -> None:
        self.directory = transcript_dir()
        self.items = entries(self.directory)
        self.selected = 0
        self.viewing: list[str] | None = None
        self.offset = 0


def render(surface, state: State) -> None:
    if state.viewing is not None:
        item = state.items[state.selected]
        body = shell.draw(
            surface,
            title="Session Logs",
            sections=("Sessions", "Transcript"),
            active=1,
            summary=f"{item['id']} · {item['tier']}",
            footer="↑/↓ scroll · Esc back · q quit",
        )
        for index, line in enumerate(
                state.viewing[state.offset:state.offset + body.height]):
            shell.put(surface, body.top + index, body.left,
                      line.replace("\t", "    "))
        return
    live = sum(int(i["size"]) for i in state.items if i["tier"] == "live")
    archived = sum(int(i["size"]) for i in state.items if i["tier"] == "archived")
    body = shell.draw(
        surface,
        title="Session Logs",
        sections=("Sessions", "Transcript"),
        active=0,
        summary=(
            f"live {proc.human_bytes(live)} · "
            f"archived {proc.human_bytes(archived)} · "
            f"{len(state.items)} panes"
        ),
        footer="Enter open · r refresh · q quit",
    )
    if not state.items:
        shell.put(surface, body.top, body.left,
                  f"no transcripts in {state.directory}",
                  shell.tango.attr("muted"))
    visible = max(1, body.height)
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(state.items) - visible)))
    import time as _time
    for index, item in enumerate(state.items[start:start + visible]):
        row = body.top + index
        selected = start + index == state.selected
        marker = "▶" if selected else " "
        stamp = _time.strftime("%Y-%m-%d %H:%M",
                               _time.localtime(float(item["mtime"])))
        tag = "[archived]" if item["tier"] == "archived" else ""
        shell.put(
            surface, row, body.left,
            f"{marker} {stamp}  {proc.human_bytes(int(item['size'])):>8}  "
            f"{item['id']} {tag}",
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
        if s.viewing is not None:
            if key == 27:
                s.viewing = None
            elif keymap.is_quit(key):
                return False
            elif step := keymap.direction(key):
                s.offset = max(0, s.offset + step)
            return True
        if keymap.is_quit(key):
            return False
        if step := keymap.direction(key):
            if s.items:
                s.selected = max(0, min(len(s.items) - 1, s.selected + step))
        elif key in keymap.SELECT and s.items:
            s.viewing = read_transcript(s.items[s.selected]).splitlines()
            s.offset = 0
        elif keymap.is_refresh(key):
            s.items = entries(s.directory)
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
