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
| `kilix-system` | Static machine facts (`--print` for plain output) |
| `kilix-volume` | Output volume and sink selection |
| `kilix-file` | File manager — navigate and open, never delete or move |
| `kilix-package` | Installed packages, read-only |
| `kilix-rollout-resume` | Recover Claude Code, Codex, and Kimi Code sessions; install and update those agents |
| `kilix-session-log` | Pane transcripts across the live and archived tiers |
| `kilix-switch` | Go to any page or pane, with a live look at what each one is showing |
| `kilix-weather` | Forecast from Open-Meteo |
| `kilix-calculator` | Calculator (also scriptable: `kilix-calculator '2+2'`) |
| `kilix-music` | Player driving kilix-amp over its control socket |
| `kilix-temps` | Live temperature, fan, and thermal-headroom [dashboard](tools/temps/README.md) |
| `kilix-virtualbox-manager` | Discover, launch, focus, and control VirtualBox VPN machines in Kilix tabs |
| `kilix-tui` | **The text-native desktop** — see below |

## The desktop: `kilix-tui`

`kilix-tui/main.py` (deliberately not under `tools/` — those are what it
launches) is a desktop provider in the same sense as Kilix 95, Kilix Cap, and
Kilix Land: it composes the commands above rather than containing any
application of its own. Its default is the canonical Tango text shell shared
by every utility: `KILIX TUI`, numbered navigation, one divider, one status
row, the application body, and a footer. An optional pixel rendering remains
available with `--graphics`. Six sections: Home (status), Programs, Machine,
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
```

Each command is a small launcher that runs the tool from this checkout, so
updating is `git pull` rather than a reinstall.

The pixel interfaces use workspace checkouts under
`<source-root>/kilix-modules` (`../../kilix-modules` from this repository), or
normally installed copies of `kitty-frame-presenter`, `soft-raster-py`, and
`soft-raster` libraries. Their text fallbacks remain available when the graphical
dependencies are absent.

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
- `kitty_rc.py` — the authenticated client for the terminal's own remote
  control. It is a convenience, never a privilege: Kilix scopes the credential
  it hands each pane at the terminal, so a tool asking for anything outside
  that set is refused even though it holds the credential.
- `shell.py` — the one four-row frame used by the desktop, managers, and every
  installed text utility.
- `kilix_desk/desk.py` and `kilix_desk/tango.py` — the one canonical text
  layout and palette used by `kilix-tui/main.py` and the interactive managers.

Text/curses is the default for every utility, including the time-series
monitors, so the suite has one visual and navigation language over SSH, in
`tmux`, and inside Kilix. Memory, Temperatures, and the desktop retain optional
framebuffer renderings behind `--graphics`.

## Going to a page or a pane

`kilix-switch` replaces the terminal's two built-in choosers, which were the
same thing twice: a numbered list of titles, one for pages and one for panes. A
title is a poor handle on a pane — several are `bash` and several more are
whatever directory they started in — so the list told you least exactly when you
had enough windows to need it.

It shows one tree of pages and their panes, with the process and working
directory that actually identify a pane, a filter (`/`) across all of it, and a
live view of what the highlighted pane is currently showing. `Tab` cycles the
scope between everything, this page, and everywhere else; Kilix binds `F12` to
open on everything and its tmux-style leader `q` to open on this page.

"This page" means the page the tool is *running* on, resolved from its own
`KITTY_WINDOW_ID`, not whichever page the terminal currently considers active —
an overlay takes the focus the moment it opens, so the two are rarely the same
question.

Renaming and closing are here because a chooser that can see everything and
change nothing sends you somewhere else to finish the job. Closing always asks
first, and both go through the terminal's remote control, which refuses them
outright unless Kilix's scoped credential has been widened to allow them.

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
