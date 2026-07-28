# kilix-tui-utils

Every Kilix terminal utility in one repository: one version, one test suite, one
installer, one shared core.

Before this, each dashboard was its own repo pinned by SHA, and each pinned
three further helper repos at their own SHAs — eight pins for two tools. This
collapses that into a single component the coordinated release pins once.

## Commands

| Command | What it does |
|---|---|
| `plebian-os` | OS control: status, update, kiosk/autologin, **power**, health |
| `kilix-cpu` | Load, per-core use, frequency, heaviest processes |
| `kilix-memory` | RAM, swap, pressure, heaviest processes |
| `kilix-disk` | Filesystem usage and an interruptible directory scan |
| `kilix-system` | Static machine facts (`--print` for plain output) |
| `kilix-volume` | Output volume and sink selection |
| `kilix-file` | File manager — navigate and open, never delete or move |
| `kilix-package` | Installed packages, read-only |
| `kilix-session-log` | Pane transcripts across the live and archived tiers |
| `kilix-weather` | Forecast from Open-Meteo |
| `kilix-calculator` | Calculator (also scriptable: `kilix-calculator '2+2'`) |
| `kilix-music` | Player driving kilix-amp over its control socket |
| `kilix-temps` | Thermal dashboard *(moving in from its own repo)* |

## Install

```sh
./install.sh                      # into ~/.local/bin
KILIX_TUI_UTILS_PREFIX=/usr/local ./install.sh
```

Each command is a small launcher that runs the tool from this checkout, so
updating is `git pull` rather than a reinstall.

## Design

**One shared core, in `src/kilix_tui/`.** A tool is a thin `main()` over it. If
a tool needs something the core lacks, the change belongs in the core so the
next tool gets it free.

- `app.py` — the event loop, guaranteed teardown, and a headless `TextSurface`.
  Every tool renders to plain text, which is what makes them all testable and
  `--screenshot`-able without a terminal.
- `keys.py` — one keymap. Thirteen tools inventing their own quit key would be
  thirteen things to learn.
- `theme.py` — reads the shared `settings.conf` every Kilix component already
  uses, and falls back to built-in defaults when Kilix is not installed, so the
  tools still work over SSH or from a bare checkout.
- `proc.py` — `/proc` and `/sys` readers shared by the monitors, so they agree
  on what a number means. Readers never raise on a missing path.

**Two rendering idioms.** Text/curses for anything that is a list you act on, so
it works over SSH and inside `tmux`. Framebuffer over the Kitty graphics
protocol for anything whose value is a time series or a shape. `kilix-temps`
is the framebuffer case and arrives with the move.

## Safety properties, enforced by tests

These tools are reachable from a desktop menu on an OS whose desktop is a
terminal, so a few properties are asserted rather than assumed:

- **The calculator does not `eval()`.** It parses with `ast` and walks an
  explicit operator allowlist, so `__import__('os').system(...)` is rejected at
  parse time rather than caught afterwards. It also bounds `2**2**30`, which is
  valid arithmetic that would otherwise hang the pane.
- **The package viewer only ever runs `dpkg-query`.** The test reads the AST and
  asserts the set of external commands, because release images pin an apt
  snapshot and a tool that installed or removed packages would silently drift a
  machine off its pinned closure.
- **The file manager cannot delete, move, rename, or chmod.** The test walks the
  AST for those calls.
- **The weather tool has no API key and no IP geolocation.** Open-Meteo needs no
  account, the location is configured rather than derived from the address, and
  the last good response is cached so an offline machine still renders.
- **The control TUI confirms before power and autologin**, and shells out to
  `pleb` / `plebian-os-update` / `systemctl` rather than reimplementing them, so
  the update transaction, lock, and rollback keep one implementation.
- **Every tool clips to its surface** at sizes down to 8×3, and quits on `q`.

## Tests

```sh
python3 tests/run.py              # all suites, one subprocess each
python3 tests/run.py calculator   # one
```

## Versioning

This repo follows the coordinated stack version alongside plebian-os, pleb,
kilix, and kilix-95. Its `v<x.y.z>` tag is created only by the release
procedure, never pushed on its own.
