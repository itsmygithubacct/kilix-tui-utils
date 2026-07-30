# Kilix Temps

`kilix-temps` is a live thermal-headroom dashboard for **Kilix**, **Pleb**, and
**Plebian-OS**. It reads Linux thermal zones, hardware-monitor temperatures,
fan tachometers, CPU utilization, load, memory, and uptime without requiring
root access or a background service. A low-frequency `/proc` sampler also
groups the hottest CPU consumers, so an alert identifies likely heat sources
such as several concurrent compiler, emulator, game, or agent processes.

The default interface is the canonical Kilix text TUI, with the same header,
numbered navigation, status row, content well, and footer as the desktop and
VirtualBox manager. It gives firmware-facing ACPI thermal zones the same
visibility as CPU, GPU, NVMe, PCH, and Wi-Fi sensors. It is a monitor only: it
never kills jobs, changes fan policy, throttles the CPU, or powers off the
computer.

The optional pixel view uses `soft-raster-py`, the Python binding for Kilix's
native `soft-raster` graphics library, and hands RGB frames to
`kitty-frame-presenter`. Unchanged frames are skipped and changing frames use
bounded rectangular updates. The default text TUI has no graphical dependency.

## Run it

```bash
cd ~/.local/gpu_terminal/sources/kilix-tui-utils
python3 tools/temps/main.py
```

Python 3.10 or newer is required. The pixel dashboard needs sibling checkouts
of `soft-raster-py`, `soft-raster`, and `kitty-frame-presenter` when run from
source; it has no Pillow dependency. Text, JSON, list, and snapshot modes remain
usable if the graphical libraries are unavailable. `lm-sensors` is useful for
the separate `sensors` command but is not required by this dashboard; Kilix
Temps reads `/sys/class/thermal` and `/sys/class/hwmon` directly.

Install it with every other utility in this repository:

```bash
./install.sh
kilix-temps
```

The generated command is a small launcher back into
`kilix-tui-utils/tools/temps`, so this checkout remains the source of truth.
The pixel dashboard discovers installed graphical libraries first, then sibling
checkouts at `../kitty-frame-presenter`, `../soft-raster-py`, and
`../soft-raster`. The installed command can be launched from any Kilix pane.
Run the complete repository test suite with `python3 tests/run.py`, or only
this tool's suite with `python3 tests/run.py temps`.

Graphical mode is optional and selected explicitly:

```bash
kilix-temps --graphics       # require the Kitty pixel dashboard
kilix-temps --text           # explicitly select the default text TUI
```

`--graphics` exits with a useful dependency error instead of silently falling
back. `--no-color` applies to the text dashboard.

Temperatures default to Fahrenheit. A territory-qualified locale that normally
uses Celsius, such as `en_GB.UTF-8` or `de_DE.UTF-8`, automatically selects
Celsius instead. Override that choice at startup with `--fahrenheit` or
`--celsius`, or press `u` while the dashboard is running. This affects display
only: sensor input, alert calculations, threshold arguments, JSON, and CSV
remain in Celsius.

## Controls

| Key | Action |
|---|---|
| `q` / `Esc` | Quit and restore the terminal |
| `Space` | Pause/resume sampling |
| `r` | Reset min, peak, and graph history |
| `+` / `-` | Sample faster/slower |
| `Up` / `Down`, `j` / `k` | Scroll sensors |
| `s` | Sort by risk or hardware source |
| `u` | Toggle Fahrenheit/Celsius display |
| `l` | Toggle CSV logging |
| `h` / `?` | Help |

CSV logging defaults to
`~/.local/gpu_terminal/kilix-temps/state/temperatures.csv`, matching the
Kilix/Pleb source-vs-runtime storage convention. Override the whole runtime
root with `KILIX_TEMPS_STORAGE_HOME` or start at an explicit path:

```bash
kilix-temps --log ~/thermal-run.csv --log-interval 1
```

The screen still samples every 0.5 seconds by default, while CSV writes are
coalesced to one sample per second and flushed as a batch. This preserves a
useful incident trail without flushing a disk write for every sensor twice per
second. Set `--log-interval` between 0.2 and 60 seconds when a different
resolution is needed.

## Alert policy

The defaults are deliberately conservative:

- **WARM:** 80°C / 176°F
- **HOT:** 90°C / 194°F
- **LIMIT:** 100°C / 212°F

When a kernel driver exports a lower safe/critical limit, that lower value
wins. For example, an NVMe drive with an 84.8°C critical limit is never treated
as safe up to the generic 100°C policy limit. Thresholds can be tightened;
these arguments are always Celsius:

```bash
kilix-temps --warning 75 --hot 85 --critical 95
```

A bell is emitted once when a sensor escalates into HOT or CRITICAL. Disable it
with `--no-bell`. A 1.5°C downgrade hysteresis prevents threshold jitter from
repeatedly changing state or ringing the bell. The dashboard does not take
automatic remedial action, so a HOT/CRITICAL alert still requires the operator
to reduce the workload and investigate cooling. Its heat-sources row updates
from a two-second CPU baseline and aggregates multiple processes with the same
name.

## Snapshot and automation modes

```bash
kilix-temps --once --no-color       # one human-readable frame
kilix-temps --json                  # one structured sample
kilix-temps --list                  # discovered sensors and driver limits
kilix-temps --demo                  # animated synthetic data
kilix-temps --demo --graphics       # force the graphical demo
```

The JSON mode is suitable for status scripts and contains current level,
temperature, effective thresholds, hardware critical threshold, headroom, fan
RPM, CPU load, memory use, uptime, and sampled top CPU consumers.

## Why both thermal zones and hwmon entries appear

Some readings intentionally appear through two kernel interfaces. A CPU package
temperature may be available through `coretemp` while the firmware's shutdown
decision is driven by an ACPI thermal zone. Kilix Temps keeps both: the hwmon
entry explains the component, while the thermal-zone entry shows the exact path
that can trigger a `HARDWARE PROTECTION shutdown`.
