# kilix-tui-utils

Every Kilix terminal utility in one repository: one version, one test suite, one
installer, one shared core.

Before this, each dashboard was its own repo pinned by SHA, and each pinned
three further helper repos at their own SHAs — eight pins for two tools. This
collapses that into one checkout pinned once by Kilix’s dependency closure.

## Commands

| Command | What it does |
|---|---|
| `plebian-os` | OS control: status, update, kiosk/autologin, **power**, health |
| `kilix-cpu` | Load, per-core use, frequency, heaviest processes |
| `kilix-memory` | Live RAM, swap, pressure, paging, and process-memory [dashboard](tools/memory/README.md) |
| `kilix-disk` | Filesystem usage and an interruptible directory scan |
| `kilix-system` | Static machine facts (`--print`) and a combined CPU, memory, disk, network, and process health report (`--json`) |
| `kilix-volume` | Clickable output mixer, plus `--compact` slider and `--settings` mute card |
| `kilix-file` | File manager — navigate and open, never delete or move |
| `kilix-system-center` | Focused machine center over CPU, memory, thermal, disk, network, audio, camera, package, and VM tools |
| `kilix-settings-center` | Shared Kilix settings, display, audio, voice, and default-desktop center |
| `kilix-software-center` | Catalog browser and confirmed installer using `kilix install` |
| `kilix-session-center` | Pane Center, PTYs, logs, and remote-session tools in one place |
| `kilix-voice-studio` | Speech commands, settings, models, status, and diagnostics |
| `kilix-launcher` | Launcher catalog: stack programs, discovered XDG apps, your `.desktop` launchers, stack scripts, a run-a-command row, and the laptop session profiles (running ones marked, Enter opens or closes them through `kilix laptop`); `kilix launcher` opens it |
| `kilix-package` | Installed packages, read-only |
| `kilix-rollout-resume` | Recover Claude Code, Codex, and Kimi Code sessions; install and update those agents |
| `kilix-session-log` | Pane transcripts across the live and archived tiers |
| `kilix-panes` / `kilix-switch` | Pane Center TUI plus pane/session/broker CLI, live text, idle detection, and bounded messaging |
| `kilix-weather` | Forecast from Open-Meteo |
| `kilix-cameras` | Camera views and stream profiles for kilix-rtsp — view a camera, mosaic a group, `n` writes a profile to `cameras.conf` |
| `kilix-calculator` | Calculator (also scriptable: `kilix-calculator '2+2'`) |
| `kilix-music` | Player driving kilix-amp over its control socket |
| `kilix-character-map` | Search Unicode names/codepoints and copy with OSC 52 |
| `kilix-notepad` | Portable UTF-8 editor with atomic saves and guarded discard |
| `kilix-find-files` | Bounded filename/glob search that does not follow directory symlinks |
| `kilix-temps` | Live temperature, fan, and thermal-headroom [dashboard](tools/temps/README.md) |
| `kilix-virtualbox-manager` | Discover, launch, focus, and control VirtualBox VPN machines in Kilix tabs |
| `kilix-tui` | **The text-native desktop** — see below |

The shared Programs registry includes **PDF Conversion** as a direct tab/pane
application through `kilix app run kilix-pdf-conversion`. The same registry
feeds both the TUI desktop and `kilix-launcher`, which is also the Programs
object used by Kilix Land.

The five center commands are focused applications over that same state and
registry, not parallel menus. They stop at their own root when the user walks
back, accept catalog action deep links, and retain the same in-place floor and
Kilix-page launch behavior as the full desktop. The accessory tools share the
shell, keymap, document opener, and OSC-52 clipboard support with the rest of
the suite.

CPU, Memory, and Temperatures consume one versioned `kilix-telemetry` snapshot
when the suite is launched by Kilix. That record supplies global and per-core
CPU use, frequency, RAM, swap, PSI, VM counters, processes, temperatures, and
fan speeds to every surface without each pane walking `/proc` and `/sys`
again. The same utilities retain their direct read-only collectors when run
standalone or when the shared sampler is unavailable, so TUI, IceWM, Land, and
remote-shell launches have the same data model without a hard service
dependency.

### Machine-readable system health

`kilix-system` keeps its interactive facts view and its line-oriented
`--print` output. For automation, `--json` emits one combined health snapshot:

```sh
kilix-system --json
kilix-system --json --top 5
```

`--top N` accepts 1 through 50 and limits the process records; it is meaningful
only with `--json`. The report has `schema_version: 1` and contains:

- `cpu`: aggregate and per-core utilization measured over a short sample,
  logical-core count, and the 1/5/15-minute load averages;
- `memory`: RAM availability and use plus swap totals and percentages, in
  bytes;
- `disks`: device, mount point, filesystem type, byte totals, and use for each
  readable real filesystem;
- `network`: aggregate byte, packet, and error counters from `/proc/net/dev`;
- `top_processes`: processes ranked by cumulative CPU time, including PID,
  name, resident bytes, memory percentage, CPU time, and kernel state; and
- numeric and UTC ISO-8601 timestamps describing when the snapshot was taken.

Network values are counters since the kernel initialized each interface, not
transfer rates. Process `cpu_time` is likewise cumulative rather than an
instantaneous CPU percentage. The command reads Linux `/proc` directly and
uses only the Python standard library; unreadable or unavailable sources
degrade to empty or zero values instead of adding a monitoring-service or
`psutil` dependency.

## Watch the episode

https://github.com/user-attachments/assets/be594a53-03b3-466f-8d8e-f1687c92ca0e

**[Desktop Three: Kilix TUI](https://github.com/itsmygithubacct/kilix-tui-utils/releases/download/media-v1/07-kilix-tui.mp4)**
— part seven of *Kilix, Pleb, and Plebian-OS: A Desktop Built Inside a Terminal*, the ten-part
stack series (1920×1080, 3m14s, 9.2 MB; published as a
[media release](https://github.com/itsmygithubacct/kilix-tui-utils/releases/tag/media-v1) so a
clone stays small). The [full series](https://github.com/itsmygithubacct/plebian-os#watch-the-series) (31m22s)
lives on `plebian-os` and plays at [plebian-os.com](https://plebian-os.com/#watch).

## The desktop: `kilix-tui`

`kilix-tui/main.py` (deliberately not under `tools/` — those are what it
launches) is a desktop provider in the same sense as Kilix 95, Kilix Cap, and
Kilix Land: it composes the commands above rather than containing any
application of its own. Its default is the canonical Tango text shell shared
by every utility: `KILIX TUI`, one divider, one status row, the application
body, a tip, and a key line. An optional pixel rendering remains
available with `--graphics`.

The desktop navigates by **place**, not by focus: one cursor, always in the
list on screen, with a breadcrumb saying where that is, `..` as a real row so
going back is somewhere the cursor can reach, and `/` to filter a long list.
The utilities keep their numbered section strip, which is a tab bar rather
than a second focus ring. Everything else is shared from one place, so the
whole suite gains it at once:

* the key line is **fitted, never clipped** — narrow terminals drop bindings
  from the middle outwards so `q quit` always survives;
* `?` opens a help overlay in any tool, built from that tool's *own* key line
  so the two cannot disagree, and listing only keys that actually work there;
* one tip per tool, in `kilix_tui/shell.TIPS`, which a test keeps complete.

Tools where typing is text — the calculator, the package and pane filters —
pass `help_key=False` and keep `?` as an ordinary character rather than
advertising a key that would do nothing.

Programs ▸ **Install software** is a place, not a shortcut out to a command:
it lists everything installable — the pinned catalog and the coding agents —
with its installed state, filterable like any other list, and Enter installs
the highlighted entry. The list comes from `kilix install --json` rather than
from a catalogue kept here, because a second reader is a second thing to keep
true. It is fetched once per visit and re-asked on `r`.

Programs ▸ **Kilix applications** filters that same host response to
applications and launches each through `kilix app run ID`. The row is generic:
new catalog apps appear in TUI panes and in Kilix Land's shared Programs
computer without another desktop-specific edit.

Three more Programs surfaces follow the same discipline. **Run a command**
(also `!` from anywhere) opens a one-line prompt on the summary row; what you
type is split like a shell would split it but never given to one — argv only,
into a Kilix page when remote control is live and in place otherwise, with
pipes refused by name rather than half-working. **Applications** lists what
the machine itself advertises: freedesktop `.desktop` entries discovered
exactly the way Kilix 95's Start menu discovers them (`kilix_tui/xdgapps.py`,
a byte-identical mirror of the host SDK's `kilix_sdk.xdgapps` kept by
`tools/sync_xdgapps.py` and pinned by a parity test, so no desktop or
catalog tool can disagree with this list), bucketed by
category; terminal apps launch like any tool, graphical ones are contained in
a `kilix run` page. And **Games** launches on Enter when the installed
launcher knows `kilix games play` — probed from its own usage line, cached per
visit — while `t` keeps the availability toggle one key away; older launchers
keep Enter as the toggle, so the list is never a dead end.

Six sections: Home (status), Programs, Machine,
System, Session, and Power — the last being the point: it closes the stack's
no-desktop-provider power gap with confirmed `systemctl`/`loginctl` actions
shared verbatim with `plebian-os` (`src/kilix_tui/privileged.py` is the one list
of what "Shut down" runs).

Three verbs, one rule. An entry is drawn in the well, handed the terminal in
place, or opened in a Kilix page (`kitty_rc.launch_tab`) — and in-place is the
floor: everything works with no terminal to talk to, and the page affordances
appear only when `kitty_rc.available()`. Launch resolution follows the
Start-menu discipline: installed command first, this checkout's own tools
second, a `kilix` subcommand third, and a foreign source checkout never.

Inside Kilix, select it like the other desktops: `kilix kilix-tui`,
`kilix desktop kilix-tui`, or `KILIX_DESKTOP_PROVIDER=tui` in the runtime
config. When it is the whole session
(`KILIX_TUI_SESSION=1`), quitting asks first.

## Install

```sh
./install.sh                      # into ~/.local/bin
KILIX_TUI_UTILS_PREFIX=/usr/local ./install.sh
make runtime-check                # immutable-package launcher closure
```

Each command is a small launcher that runs the tool from this checkout, so
updating is `git pull` rather than a reinstall.
`make runtime` builds the same closure under `.runtime/bin` without mutating
the user's Start menu; `kilix-content` uses that explicit target when one
package provides the full application suite.

The pixel interfaces use workspace checkouts under
`<source-root>/kilix-modules` (`../../kilix-modules` from this repository), or
normally installed copies of `kitty-frame-presenter`, `soft-raster-py`, and
`soft-raster` libraries. The Python binding is maintained under
[`soft-raster/python`](https://github.com/itsmygithubacct/soft-raster/tree/main/python),
not in the archived standalone `soft-raster-py` repository. Their text
fallbacks remain available when the graphical dependencies are absent.

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
- `telemetry.py` — the optional client for Kilix's private shared-memory ring;
  daemon startup requests are rate-limited and do not wait for readiness, and
  every utility keeps its direct fallback.
- `proc.py` — resilient `/proc` and `/sys` fallback readers shared by the
  monitors. Readers never raise on a missing path.
- `kitty_rc.py` — the authenticated client for the terminal's own remote
  control. It is a convenience, never a privilege: Kilix scopes the credential
  it hands each pane at the terminal, so a tool asking for anything outside
  that set is refused even though it holds the credential.
- `pane_center.py` — joins that live page tree with one PTY-broker snapshot and
  the coding-agent conversation owned by each reported process. It reads only
  `/proc` descriptors for live pane PIDs rather than walking saved history.
- `shell.py` — the one four-row frame used by the desktop, managers, and every
  installed text utility.
- `openers.py` — argv-only document dispatch shared by Files and Find Files.
- `clipboard.py` — bounded OSC-52 copy support for text-native applications.
- `kilix_desk/desk.py` and `kilix_desk/tango.py` — the one canonical text
  layout and palette used by `kilix-tui/main.py` and the interactive managers.

Text/curses is the default for every utility, including the time-series
monitors, so the suite has one visual and navigation language over SSH, in
`tmux`, and inside Kilix. Memory, Temperatures, and the desktop retain optional
framebuffer renderings behind `--graphics`.

## Pane Center

`kilix-panes` (also installed as the compatible `kilix-switch`) replaces the
terminal's two built-in choosers, which were the
same thing twice: a numbered list of titles, one for pages and one for panes. A
title is a poor handle on a pane — several are `bash` and several more are
whatever directory they started in — so the list told you least exactly when you
had enough windows to need it.

It shows one tree of pages and their panes, with activity, coding-agent type,
process, working directory, PTY-broker health, current task, and a live view of
what the highlighted pane is showing. `/` filters across all of those fields;
`s` writes a message to the highlighted pane and submits it; `Tab` cycles the
scope between everything, this page, and everywhere else. Kilix binds `F12` to
open on everything and its tmux-style leader `q` to open on this page.

Activity is evidence-based. A live Codex process is `working` when its newest
turn boundary is `task_started`, and `idle` only when the same process still
owns the rollout and the newest boundary is `task_complete`. Claude's validated
live registry supplies `idle`, `waiting`, or active state. A recognized agent
without an explicit signal stays `agent`; it is never optimistically called
idle. Shells, SSH sessions, and other foreground programs are labelled
separately.

The same snapshot is a scriptable `kilix panes` interface:

```sh
kilix panes list                         # compact table
kilix panes --json                       # stable kilix.panes/v1 record
kilix panes dump 338 --lines 60          # last 60 lines, including scrollback
kilix panes dump 338 -n 20 --screen      # visible screen only
kilix panes wait 338 --for idle --timeout 300
kilix panes send 338 --enter 'continue with the next item'
printf 'status please' | kilix panes send 338 --enter

kilix panes new --name codex-office --panes 4 \
  --pane-name administrator,assistant,engineer,worker \
  --pane-dir ~/office/administrator,~/office/assistant,~/office/engineer,~/office/worker \
  --command 'codex --yolo' \
  --initial-prompt 'familiarize yourself with your role' --enter

kilix panes close 338                    # one pane
kilix panes close 338 --page             # the whole page it sits in
```

Targets may be a pane ID, a unique title/substring, a broker-session prefix, or
a coding-session ID prefix. Ambiguity is rejected with the matching pane IDs.
`send` refuses the caller's own pane unless `--allow-self` is explicit, targets
the exact per-pane broker marker, and splits UTF-8 input into the authorizer's
1024-byte chunks. `--enter` emits carriage return, which submits both a shell
line and the current Codex prompt. An accepted send remains fire-and-forget;
read the pane back with `dump` when delivery must be proved.

"This page" means the page the tool is *running* on, resolved from its own
`KITTY_WINDOW_ID`, not whichever page the terminal currently considers active —
an overlay takes the focus the moment it opens, so the two are rarely the same
question.

Renaming and closing are here because a chooser that can see everything and
change nothing sends you somewhere else to finish the job. Closing always asks
first, and both go through the terminal's remote control, which refuses them
outright unless Kilix's scoped credential has been widened to allow them.

### Creating panes

`new` builds a page and fills it, which is the one verb here that makes something
rather than reading it. Three refusals are deliberate:

- **A per-pane list must match the pane count exactly.** Four panes and three
  directories is an error, not a cue to invent the fourth — padding would build a
  different surface than the one asked for, and would succeed while doing it.
- **`--command` is split into a fixed argv here and never handed to a shell.** A
  title, a path or a command containing `;` stays one inert argument. This is the
  same rule `launch_tab` already followed for the desktop launchers.
- **A created pane is not yet an addressable one.** The PTY broker marker that
  `send-text` matches on is written by the pane's own startup, so `--initial-prompt`
  waits for it per pane (`--ready-timeout`) and reports the panes it could not
  reach instead of assuming the text landed. Unreachable panes make the command
  exit non-zero with the ids named.

The TUI offers only the narrow case — `n`, type a title, Enter creates one shell
pane in a new page. A four-pane office with per-pane directories carries more
arguments than a one-line prompt can hold honestly, so it stays a scripted act.

## Recovering coding sessions

`kilix-rollout-resume` exists because a coding agent's terminal and its
transcript have separate lifetimes. Claude Code, Codex, and Kimi Code all
persist a conversation to disk as it happens and all three can reload one by
ID, so closing a window does not destroy the work — it only makes it hard to
find. The picker lists all three together and hands the current Kilix tab over
to the session you choose, so the tab becomes the resumed agent.

Each agent stores conversations differently, and each states its own turn
boundaries, so the recovery state is read rather than guessed:

- **cut-off** — the transcript stops mid-turn. Codex says so outright (a
  `task_started` with no `task_complete`); Kimi shows a `step.begin` with no
  matching end; Claude Code ends on a tool call nothing answered. This is the
  strongest sign a terminal disappeared rather than the operator leaving.
- **idle** — the last turn finished. Resumable, but possibly a clean exit.
- **live** — a process still owns the conversation, so recovery is refused.
  Codex and Kimi hold their transcript open, which `/proc` proves; Claude Code
  publishes a file per process, believed only when the process start time still
  matches, so a recycled PID cannot resurrect a dead session.

A picker is no use without the agent that owns the transcript, so `Tab` opens
an agent list where `Enter` installs a missing agent or updates an installed
one. Installs run the vendor's own documented command and show it, and the page
it came from, before anything happens. Updates delegate to each agent's own
updater (`claude update`, `codex update`, `kimi upgrade`) rather than
re-running an install script.

### Skipping approval prompts

A resumed agent normally asks before it acts. `y` in the picker, or `--yolo` on
the command line, starts it without those prompts — using whichever flag that
agent actually accepts, since they disagree:

| Agent | Flag |
|---|---|
| Claude Code | `--dangerously-skip-permissions` |
| Codex | `--yolo`, before the subcommand |
| Kimi Code | `--yolo` |

The default comes from `KILIX_CODING_YOLO` in the shared
`~/.local/gpu_terminal/settings.conf`, set from **Kilix Settings → Tools**. It
belongs there rather than in this tool because it decides whether an agent asks
before it acts, which the user should be able to find and audit next to every
other stack-wide preference. It is off unless the file says otherwise, turning
it on in the picker is confirmed once, and the header reads `YOLO` for as long
as it is on. `--no-yolo` overrides the setting for one command.

The Start menu tracks reality: the picker entry is always installed, and an
"Update <agent>" entry is written per agent only while that agent is present,
and removed when it isn't. `./install.sh` syncs them, and so does an install
done from inside the tool.

```sh
kilix-rollout-resume                     # the picker
kilix-rollout-resume list --since 24h --state candidates --query gpu_terminal
kilix-rollout-resume show 019faaad       # transcript, cwd, size, state, live PIDs
kilix-rollout-resume resume 019faaad     # hand this terminal to the agent
kilix-rollout-resume resume 019faaad --detached --name repair --dry-run
kilix-rollout-resume restore --limit 5   # several, into detached tmux sessions
kilix-rollout-resume restore --limit 5 --dry-run --json
kilix-rollout-resume doctor
kilix-rollout-resume prune               # stale Claude process descriptors
kilix-rollout-resume status
```

The former Claude-only and Codex-only recovery tools are folded into this
command. Their useful CLI features remain: named or attached tmux resumes,
working-directory overrides, live-owner overrides, exact dry-run/JSON plans,
per-user executable paths, and diagnostics. Claude resumes also accept
`--fork`, `--permission-mode`, `--model`, and `--prompt`. Configure persistent
overrides with, for example,
`kilix-rollout-resume configure --tmux /usr/bin/tmux --gap 45`; the mode-0600
file is shown by `configure`. Existing standalone Claude/Codex executable,
launch-interval, and `tb` settings are read as migration fallbacks. Native
tmux is the default backend; a configured `tb` adapter keeps tmux-cli's pane
logging for users who relied on it, and `--native-tmux` bypasses it for one
invocation.

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
- **Session recovery never runs an arbitrary command.** Launching only ever
  shells out to `tmux`, asserted from the AST and again from the calls a stubbed
  runner actually receives. An install is a pipe from the network into a shell —
  the vendors' documented method, and still the most consequential thing here —
  so the exact command is pinned in a test, printed with its source URL, and
  never runs without an explicit yes. A live session is never offered for
  recovery, and batch restores wait between launches so several agents waking up
  together do not trip one account-wide rate limit.
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

This repository is a Kilix-pinned desktop and utility closure, not a
coordinated Plebian-OS release-core member. Selecting Kilix TUI as the desktop
is optional; managed Plebian-OS installs the same checkout eagerly because it
also supplies the unified utility suite. Its `VERSION` may advance
independently, and it does not receive the core’s coordinated `v<x.y.z>` tags.
A Plebian-OS release inherits the exact reviewed commit through the Kilix
commit that pins it.
