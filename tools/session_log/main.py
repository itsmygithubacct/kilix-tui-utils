"""kilix-session-log — browse named pane transcripts across all storage tiers.

Kilix keeps recent transcripts uncompressed and archives older ones with zstd.
Which tier a session landed in is an implementation detail, so this lists both
together and opens either without the reader needing to know.
"""
from __future__ import annotations

import subprocess
from collections import deque
import select
import time

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402
from kilix_rollout.inventory import raw_entries, transcript_root  # noqa: E402


def transcript_dir() -> str:
    return str(transcript_root())


def entries(directory: str) -> list[dict[str, object]]:
    return [item | {"id": item["session_id"], "mtime": item["updated"]}
            for item in raw_entries(directory)]


def read_transcript(entry: dict[str, object], limit: int = 200000) -> str:
    """Return a transcript's text, decompressing an archived one."""
    path = str(entry["path"])
    if path.endswith(".zst"):
        process = None
        try:
            process = subprocess.Popen(["zstd", "-dcq", "--", path], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            parts, size = deque(), 0
            deadline = time.monotonic() + 30
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    return "(archive read timed out)"
                part = os.read(process.stdout.fileno(), 65536)
                if not part:
                    break
                parts.append(part)
                size += len(part)
                while parts and size - len(parts[0]) >= limit:
                    size -= len(parts.popleft())
            if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                return "(archive could not be decoded)"
            data = b"".join(parts)[-limit:]
        except (OSError, subprocess.SubprocessError):
            return "(zstd is required to read this archived transcript)"
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
    else:
        try:
            with open(path, "rb") as handle:
                handle.seek(max(0, os.fstat(handle.fileno()).st_size - limit))
                data = handle.read(limit)
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
        self.filter = shell.Filter()

    def view(self) -> list:
        """The transcripts on screen; every reader goes through this so the
        cursor cannot index past a filtered list."""
        return self.filter.apply(self.items, key=lambda row: " ".join(str(row.get(key, ""))
                                 for key in ("id", "title", "cwd", "command", "tier")))


def render(surface, state: State) -> None:
    if state.viewing is not None:
        item = state.view()[state.selected]
        body = shell.draw(
            surface,
            title="Session Logs",
            sections=("Sessions", "Transcript"),
            active=1,
            summary=f"{item.get('title', item['id'])} · {item['id']} · {item['tier']}",
            footer="↑/↓ scroll · Esc back · q quit",
        )
        for index, line in enumerate(
                state.viewing[state.offset:state.offset + body.height]):
            shell.put(surface, body.top + index, body.left,
                      line.replace("\t", "    "))
        return
    rows = state.view()
    state.selected = max(0, min(state.selected, max(0, len(rows) - 1)))
    live = sum(int(i["size"]) for i in state.items if i["tier"] == "live")
    recent = sum(int(i["size"]) for i in state.items if i["tier"] == "recent")
    archived = sum(int(i["size"]) for i in state.items if i["tier"] == "archived")
    body = shell.draw(
        surface,
        title="Session Logs",
        sections=("Sessions", "Transcript"),
        active=0,
        summary=state.filter.summary(len(rows)) or (
            f"live {proc.human_bytes(live)} · "
            f"recent {proc.human_bytes(recent)} · "
            f"archived {proc.human_bytes(archived)} · "
            f"{len(state.items)} panes"
        ),
        summary_role="accent" if state.filter.active() else "muted",
        footer=(state.filter.footer() if state.filter.typing
                else "Enter open · / filter · r refresh · q quit"),
    )
    if not rows:
        shell.put(surface, body.top, body.left,
                  f"no transcripts in {state.directory}",
                  shell.tango.attr("muted"))
    visible = max(1, body.height)
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(rows) - visible)))
    import time as _time
    for index, item in enumerate(rows[start:start + visible]):
        row = body.top + index
        selected = start + index == state.selected
        marker = "▶" if selected else " "
        stamp = _time.strftime("%Y-%m-%d %H:%M",
                               _time.localtime(float(item["mtime"])))
        tag = f"[{item['tier']}]"
        shell.put(
            surface, row, body.left,
            f"{marker} {stamp}  {proc.human_bytes(int(item['size'])):>8}  "
            f"{item.get('title', item['id'])} {tag} · {str(item['id'])[:13]}",
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
        if s.filter.typing:
            s.filter.handle(key)
            return True
        if keymap.is_quit(key):
            return False
        if s.filter.handle(key):          # `/` opens it
            return True
        rows = s.view()
        if step := keymap.direction(key):
            if rows:
                s.selected = max(0, min(len(rows) - 1, s.selected + step))
        elif key in keymap.SELECT and rows:
            s.viewing = read_transcript(rows[s.selected]).splitlines()
            s.offset = 0
        elif keymap.is_refresh(key):
            s.items = entries(s.directory)
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
