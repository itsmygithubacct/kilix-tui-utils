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
| `kilix-rollout-resume` | Recover Claude Code, Codex, and Kimi Code sessions; install and update those agents |
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
kilix-rollout-resume list --since 24h
kilix-rollout-resume resume 019faaad     # hand this terminal to the agent
kilix-rollout-resume restore --limit 5   # several, into detached tmux sessions
kilix-rollout-resume status
```

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

This repo follows the coordinated stack version alongside plebian-os, pleb,
kilix, and kilix-95. Its `v<x.y.z>` tag is created only by the release
procedure, never pushed on its own.
