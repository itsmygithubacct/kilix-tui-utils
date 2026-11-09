# kilix-tui-utils

Every Kilix terminal utility in one repository: one version, one test suite, one
installer, one shared core.

Ships in Plebian-OS 0.2.3: Kilix installs it on first use, and the content
catalog carries it as its one package. This README describes `main`; the
pinned `b6321de9` is 32 commits ahead on `integration/qemu-tabs` and adds
`kilix-qemu-manager`.

## What it does

- The commands below on one shared core: monitors, OS control, files,
  packages, network, audio, five centers, a launcher and the `kilix-tui` desktop.
- Pane Center: every Kilix page and pane, agent activity, live text, messaging.
- Session recovery: resume coding-agent sessions whose terminal is gone
  ([tool notes](tools/rollout_resume/README.md)).
- Plain-text rendering first, so tools work over SSH, in tmux and in tests.

## Quick start

```sh
./install.sh                    # launchers into ~/.local/bin, running this checkout
kilix-system --json --top 5     # one combined health snapshot
kilix-tui                       # the text desktop
python3 tests/run.py            # all suites
```

Inside Kilix: `kilix tui`, `kilix launcher`, `kilix panes` (or `F12`), and
`kilix app run <id>` for catalog applications.

## Commands

| Command | What it does |
|---|---|
| `plebian-os` | OS control: status, update, kiosk/autologin, **power**, health |
| `kilix-cpu` | Load, per-core use, frequency, heaviest processes |
| `kilix-memory` | Live RAM, swap, pressure, paging, and process-memory [dashboard](tools/memory/README.md) |
| `kilix-disk` | Filesystem usage and an interruptible directory scan |
| `kilix-system` | Static machine facts (`--print`) and a combined CPU, memory, disk, network, and process health report (`--json`) |
| `kilix-volume` | Clickable output mixer, plus `--compact` slider and `--settings` mute card |
| `kilix-network` | Links and saved NetworkManager connections — Enter brings one up, `d` (confirmed) takes one down; read-only without nmcli |
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
| `kilix-music` | [File and live EnCodec player](tools/music/README.md) sharing the Amp/mpv session, queue and controls |
| `kilix-character-map` | Search Unicode names/codepoints and copy with OSC 52 |
| `kilix-notepad` | Portable UTF-8 editor with atomic saves and guarded discard |
| `kilix-find-files` | Bounded filename/glob search that does not follow directory symlinks |
| `kilix-temps` | Live temperature, fan, and thermal-headroom [dashboard](tools/temps/README.md) |
| `kilix-virtualbox-manager` | Discover, launch, focus, and control VirtualBox VPN machines in Kilix tabs |
| `kilix-tui` | **The text-native desktop** — see below |

## How it works

Each command is a small launcher into one checkout, and every tool is a thin
`main()` over `src/kilix_tui/` ([design](docs/design.md); `make runtime` builds
the catalog's copy, [install](docs/install.md)). Monitors read Kilix's shared
telemetry snapshot when it exists ([suite](docs/suite.md)). `kilix-tui` composes
the tools into a desktop and lists installable content from
`kilix install --json` ([desktop](docs/kilix-tui.md)). Tests enforce the
[safety properties](docs/safety.md).

### Pane Center activity

Activity is evidence-based. A live Codex process is `working` when its newest
turn boundary is `task_started`, and `idle` only when the same process still
owns the rollout and the newest boundary is `task_complete`. Claude's validated
live registry supplies `idle`, `waiting`, or active state. A recognized agent
without an explicit signal stays `agent`; it is never optimistically called
idle. Shells, SSH sessions, and other foreground programs are labelled
separately.

Claude activity uses its native process's own config directory, an exact
`procStart` registry match, and repeated command-line, environment and registry
checks. A mixed-agent pane or duplicate process identity is ambiguous. Its
`idle` and `shell` statuses map to idle, `busy` to working, and `waiting` to
waiting. Other statuses remain `agent`.

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

## Documentation

| Document | Contents |
|---|---|
| [docs/suite.md](docs/suite.md) | Programs registry, centers, shared telemetry, `kilix-system --json` |
| [docs/kilix-tui.md](docs/kilix-tui.md) | The `kilix-tui` desktop, and its episode video |
| [docs/pane-center.md](docs/pane-center.md) | Pane Center, `kilix panes` commands, creating panes, tell/wait |
| [docs/install.md](docs/install.md) | `install.sh`, `make runtime`, pixel-rendering dependencies |
| [docs/design.md](docs/design.md) | The shared core module by module |
| [docs/safety.md](docs/safety.md) | Safety properties the tests enforce |
| [docs/development.md](docs/development.md) | Tests, versioning, and why this is one repository |

## Related repositories

- Used by [kilix](https://github.com/itsmygithubacct/kilix#readme) and
  [kilix-content](https://github.com/itsmygithubacct/kilix-content#readme).
- Uses [kilix-telemetry](https://github.com/itsmygithubacct/kilix-telemetry#readme),
  [kitty-pty-broker](https://github.com/itsmygithubacct/kitty-pty-broker#readme),
  [kitty-frame-presenter](https://github.com/itsmygithubacct/kitty-frame-presenter#readme)
  and [soft-raster](https://github.com/itsmygithubacct/soft-raster#readme).

## Part of Plebian-OS

kilix-tui-utils is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
Kilix's installer clones the pinned commit and puts its launchers in
`~/.local/bin`, during `pleb install` and on first use of the TUI desktop, the
launcher or a monitor. The map of every repository and
how it ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

MIT. See [LICENSE](LICENSE).
