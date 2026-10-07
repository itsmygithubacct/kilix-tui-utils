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
| `kilix-music` | [File and live EnCodec player](tools/music/README.md) controlling the shared Amp backend |
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

Four more Programs surfaces follow the same discipline. **Run a command**
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
a `kilix run` page. **Launchers** lists the user's own desktop-folder
`.desktop` files — the ones a Create Launcher wizard writes on any desktop —
read from the same folders `kilix-launcher` reads
(`kilix_desk.registry.launcher_dirs`), under the same containment. And
**Games** lists the games from that same host response — a game added to the
catalog is listed and playable here with no desktop release — with the SDK's
availability toggles as the on/off hint. Enter launches when the installed
launcher knows `kilix games play` — probed from its own usage line, cached per
visit — while `t` keeps the availability toggle one key away; older launchers
keep Enter as the toggle (or the install, for a game the toggle table does not
know), so the list is never a dead end.

That is the contract as the catalog grows: **there is no second list.** Every
place that offers installable content reads the one cached
`kilix install --json` answer (or the shared Programs registry), fetched once
per visit and re-asked on `r`, so a new catalog entry needs a kilix-content
publish and the kilix pin that ships it — never a desktop release.
`KILIX_TUI_CATALOG` can point that one answer at a catalog file, for tests and
for development against a catalog that is not installed yet; it substitutes
the list, it never adds one. A test audits the real catalog through that
override: every row must be installable from Software, and every app and game
must resolve to its `kilix app run` / `kilix games play` launch.

Home also carries the desk's one piece of durable state: rows pinned with
`p` from any plain entry, and the last few launches, kept in one small JSON
record (`$XDG_STATE_HOME/kilix-tui/desk.json`; `KILIX_TUI_STATE` relocates
it) written atomically and only on change. `src/kilix_desk/durable.py`
records the decision to break the desk's read-only purity exactly this far
and no further — confirmed actions never become one-Enter rows, and there is
no last-place restore: Home stays the fixed landing.

The desk's accent is a choice. System ▸ **Palette** tries on one of four
Tango flavors — sky blue (the default), chameleon green, plum, amber — for
the running session, and `KILIX_TUI_FLAVOR` in the shared `settings.conf`
(the same file every `theme.setting` knob reads, with the environment as the
fallback) makes one permanent. A flavor swaps only the structural/selection
ramp in `src/kilix_desk/tango.py`, in both the text and pixel renderings;
red stays reserved for power and refusal in every flavor.

Left alone, the desk saves the screen. When `kilix-tui` is the whole session
(`KILIX_TUI_SESSION=1`), ten quiet minutes hand the terminal to
`kilix screensaver` — the same launch the Screensavers place offers — and
any key is the way back. `KILIX_TUI_SAVER_MINUTES` changes the span (`0`
switches it off; off is also the default in a pane or over ssh, where taking
the terminal would interrupt the surrounding session), and `KILIX_TUI_SAVER`
names a favourite saver. An idle start is nobody's launch: it never becomes
a Home recents row.

Six sections: Home (status, pinned and recent launches), Programs, Machine,
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

**The network boundary, decided.** Machine ▸ Network lands on `kilix-network`:
the canonical shell over what every link is doing and the saved NetworkManager
connections — up on Enter, down only after a confirmation, because the link
being cut is often the one carrying the keystroke. Creating connections and
entering secrets deliberately stay in `nmtui`, kept one row below as the
presence-gated **Connection editor**: a password prompt belongs to
NetworkManager's own agent, and a reimplementation of it here would be a
second thing to get wrong. Without NetworkManager the tool degrades to a
read-only `/sys/class/net` view rather than an error.

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

Activity is evidence-based, and only **Claude Code** is certified: its validated live
registry supplies `idle`, `waiting` or `working` (see "Where an agent's state comes
from"). Every other coding agent (Codex, Grok, Qwen OMP, Kimi, anything unrecognised)
is shown with its command, directory and title but reads `agent`: it is never
optimistically called idle, working or waiting. Shells, SSH sessions and other
foreground programs are labelled separately.

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

### Where an agent's state comes from

A pane's `activity` (`idle`, `working`, `waiting`; anything the reader cannot
prove is `agent`) is read from the agent's own structured records, never from
the screen, and **only for Claude Code**. Codex, Grok, Qwen OMP, Kimi and any
other provider are always `agent`: none of them writes a record that names the
session a given process runs *now*, and the heuristics that stood in for one (a
Grok registry entry that only needs its PID to exist, the newest OMP file in a
directory, a Codex rollout's directory or timestamp) all named the wrong session in
review. A pane that runs Claude beside any other agent process is `agent`, in
either process order. A screen cannot be read safely: a draft, an approval or a modal can
look like an empty prompt (two independent reviews of a screen reader showed it).

| Provider | Which record names the session | How the state is read |
| --- | --- | --- |
| Claude Code | the registry descriptor `~/.claude/sessions/<pid>.json` of a pane process, accepted only for the pane's single Claude process, from its own config directory, while its recorded `procStart` equals the process's start time (see below) | its `status`: `idle` → idle, `busy` → working, `waiting` → waiting, `shell` → idle. Claude Code derives `shell` as "idle at its prompt while a background shell, monitor or task still runs" (`status === "idle" && <background work> ? "shell" : status`), which is the "1 monitor" case. Any other value is `agent` |
| Codex, Grok, Qwen OMP, Kimi, others | **none** | **no state: always `agent`** |

**Codex state is not available until Codex exposes an exact current-session
identity, and kilix-needle's `tell`/`wait` therefore refuse Codex panes.** Nothing
Codex writes names the session a process runs *now*, and every candidate failed
review:

- a rollout's directory, `originator`, timestamp or file name say when and where
  a session started, not which process still owns it (`/resume` and `/fork` switch
  sessions in place, the writer may have exited, another run may share the
  directory, a PID can be reused);
- `resume <id>` on the command line names the session the process *started*
  with, not the one it runs after a `/resume`;
- a rollout the process holds open (`/proc/<pid>/fd`) is usually absent (Codex
  0.160 opens it per write), and when present it does not establish who owns
  it or whether it is the main session: the PID may now be another program, the
  holder may be a viewer, a read-only reader or a subagent's helper, the file may
  have been deleted and replaced, a second holder may be missing from the pane
  census, and the census can change while it is read;
- `logs_2.sqlite` tags log rows with `pid:<n>:<uuid>` and a `thread_id`, but one
  process logs many threads (a dozen were measured on one instance, subagents
  included), no row says which is the current one, and a fresh or idle process
  has no rows.

So the pane center shows a Codex pane as `agent` (its command, directory and title,
never an idle/working/waiting). The Codex rollout parser in `src/kilix_rollout/codex.py` still serves
the session listings and the `rollout` tools, not pane activity. It fails closed for
every consumer: any record newer than the newest turn boundary that is unreadable,
not valid UTF-8 (display text stays tolerant, state does not), not an object, nested
too deeply, of an unknown kind or event type, or with a payload that is not an object
gives "unknown", never the older boundary; an
approval request is matched only to a *later* resolution that shares a call or
approval id and the same turn; a turn ends once, it must meet its own start, and a start whose
previous boundary is another start (overlapping open turns) is unknown; and the
tail read is limited to 8 MiB, 1 MiB per record and 2 s (a gap before the boundary is
settled is unknown).

What a structured state cannot see: a **draft typed into the composer** (an
agent that is idle with half a line typed is `idle`), or a modal (trust, update,
login) that the records do not mention. This was already true of every state
Kilix names.

Claude Code's descriptor is believed only when all of this holds. Nothing is carried
between panes or snapshots: for each pane the process is observed (start time, then
command line and environment, then the start time again), its registry row is read
**from disk now**, the process is observed again and the row read again, and
everything must be equal; once every pane is done they are all checked once more, so
a process that changed while a later pane was inspected is dropped.

- the pane has **exactly one** process that mentions any agent at all. A process
  *mentions* an agent when any path component of any of its arguments (program
  included) is `claude`, `claude-code`, `codex`, `grok`, `omp`, `kimi` or `kimi-code`
  (or `name-…`/`name.…`): `less claude`, `env claude`, `node …/claude-code/cli.js`
  all count. `tmux`, `screen`, `ssh`, `mosh` and `kitten` host a command and are not counted
  (the needle tmux hint covers `tmux … claude`; Kilix starts every coding pane as
  `kitten run-shell … claude …`, whose child, the agent itself, is listed beside it). Two such processes, or one that
  mentions two agents, make the pane `agent`;
- that process is Claude in its **native form only**: `argv[0]` is exactly `claude`
  (`claude`, `/path/to/claude`). `node`/`nodejs`/`bun` running the npm entrypoint or
  a script, aliases, differently spelled names and wrappers are *possible* Claudes
  (they make a pane ambiguous) but are never named: they read `agent`;
- its **whole live command line** (`/proc/<pid>/cmdline`) equals the pane's. Every
  read of `/proc/<pid>/{cmdline,environ,stat}` and of a registry file is bounded and
  an overflow means "incomplete", so a prefix is never compared as if it were whole
  (1 MiB for a command line, 4 MiB for an environment); text that is not UTF-8 is
  refused;
- the row is in the registry of the process's **own** `CLAUDE_CONFIG_DIR` (else
  `$HOME/.claude`, both from its `/proc/<pid>/environ`; an unreadable, unknown or
  relative context is `agent`), its `pid` is a JSON integer in range, its
  `procStart` is a non-negative integer or a string of decimal digits (the form
  Claude writes) equal to the process's start time, and no other row names the pid.
  A malformed file (float, string, boolean or out-of-range numbers, nesting too deep,
  not UTF-8, too large) is that row refused, never an error for the other panes.

- the pid is listed **once** in the whole pane census. A pid that appears in two panes,
  or twice in one, is a contradictory listing: every entry carrying it is `agent`, so
  the observation of one entry is never lent to another with a different command line.
- the environment does not carry two *different* values for `HOME` or
  `CLAUDE_CONFIG_DIR` (which one a program honours is unknown); identical repeats are
  harmless.

Anything else is `agent`.

**What the ambiguity guard is, exactly.** It is a textual rule, not semantic detection:
a process "mentions" an agent when one **path component** of one of its arguments (the
argument split on `/`) is exactly one of the names above, or begins with `name-` or
`name.`, compared case-insensitively. It does not parse shell strings, aliases, scripts
or wrappers: `sh -c 'exec codex'` is a single argument with no such component, and an
alias or a differently named binary is invisible to it. A pane can therefore still be
named while an agent it cannot recognise runs beside Claude; the guard narrows the
doubt, it does not remove it. Only the hosts listed above are exempt.

**What cannot be removed.** Every check above reads the process and the registry at some
moment. A process or row can change after the last read and before the answer is used
(or before a person acts on it); no reader can close that window, and this one narrows
it by re-reading every certified pane once more at the end of the snapshot.

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
