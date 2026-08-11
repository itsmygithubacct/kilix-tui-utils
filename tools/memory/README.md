# Kilix Memory

Kilix Memory is a read-only Linux memory dashboard for terminals. Its default
interface is the canonical Kilix text TUI; an optional native pixel view is
available with `--graphics`:

- RAM used/available and a physical-memory composition
- swap use and swap-in/swap-out rates
- Linux PSI memory pressure (`some` and `full`)
- page faults, major faults, scanning, reclaim, and allocation stalls
- a live process table sortable by RSS, PID, name, or user
- history graphs, compact-pane rendering, ANSI fallback, JSON, and one-shot CLI

The graphical path is built from the shared
[`soft-raster-py`](https://github.com/itsmygithubacct/soft-raster-py),
[`soft-raster`](https://github.com/itsmygithubacct/soft-raster), and
[`kitty-frame-presenter`](https://github.com/itsmygithubacct/kitty-frame-presenter)
libraries used by other Kilix graphical applications.

Kilix Memory monitors only. It never kills processes, changes priorities,
reclaims memory, or modifies kernel settings.

## Run from source

Python 3.10 or newer is required. For the pixel dashboard, keep the shared
libraries as sibling checkouts and build the native rasterizer:

```bash
cd ~/.local/gpu_terminal/sources/kilix-desktops/kilix-tui-utils
python3 tools/memory/main.py --graphics
```

Text mode is the default everywhere; graphics is explicit:

```bash
python3 tools/memory/main.py
python3 tools/memory/main.py --text
python3 tools/memory/main.py --once
python3 tools/memory/main.py --json
python3 tools/memory/main.py --demo --graphics
```

Interactive keys:

| Key | Action |
| --- | --- |
| `q`, `Esc` | Quit |
| `Space` | Pause/resume sampling |
| `r` | Reset graph/rate history |
| `s` | Cycle RSS/PID/name/user sorting |
| arrows, `j`/`k`, Page Up/Down | Scroll processes |
| `h`, `?` | Toggle help |

## Install

Install it with every other utility in this repository:

```bash
./install.sh
kilix-memory --graphics
```

The generated command is a small launcher back into
`kilix-tui-utils/tools/memory`, so this checkout remains the source of truth.
The graphical dashboard discovers installed libraries first, then sibling
checkouts at `../../kilix-modules/kitty-frame-presenter`,
`../../kilix-modules/soft-raster-py`, and
`../../kilix-modules/soft-raster`. Run the
complete repository test suite with
`python3 tests/run.py`, or only this tool's suite with
`python3 tests/run.py memory`.

## CLI

```text
--interval SECONDS       refresh interval (0.2 through 60)
--history SAMPLES        graph history length
--sort rss|pid|name|user initial process order
--graphics               require the Kitty pixel UI
--text                   explicitly select the default text TUI
--once                   emit one dashboard frame
--json                   emit one machine-readable snapshot
--demo                    use animated synthetic data
```

`--once` and `--json` work without a TTY. `NO_COLOR` disables ANSI styling.

## Accounting

Primary usage is `MemTotal - MemAvailable`, matching the practical semantics
used by modern Linux monitors: filesystem cache that can be reclaimed is not
treated like permanently unavailable RAM. The composition panel separately
shows application/kernel use, cache, buffers, and completely free pages.

The process table reads RSS from `/proc/<pid>/status`. RSS is inexpensive and
available without elevated privileges, but shared pages can appear in more
than one process. Scanning every process's `smaps` for PSS would make the
monitor itself materially heavier, so Kilix Memory does not do that in its
continuous process table.

Inside Kilix 0.1.9 and later, these fields and the process table come from the
single per-user `kilix-telemetry` snapshot already used by terminal chrome and
the other system dashboards. A standalone launch uses the direct procfs reader
described above; the presentation and accounting are identical.

Some process rows can disappear during a sample because Linux processes are
created and exit while `/proc` is scanned. Permission-restricted processes are
skipped rather than failing the dashboard.

## Privacy and storage

Kilix Memory reads local kernel statistics and writes no history by default.
It has no network client, process-control action, or configuration database.
Shared telemetry is a private local mmap ring; no measurements leave the
machine, and the direct reader remains available without its daemon.

## License

MIT. The optional graphical path uses kitty-frame-presenter, soft-raster-py,
and soft-raster. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
