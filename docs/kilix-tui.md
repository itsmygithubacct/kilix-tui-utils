# The desktop: `kilix-tui`

Moved verbatim from the README, with the episode that shows this desktop.

## Watch the episode

https://github.com/user-attachments/assets/be594a53-03b3-466f-8d8e-f1687c92ca0e

**[Desktop Three: Kilix TUI](https://github.com/itsmygithubacct/kilix-tui-utils/releases/download/media-v1/07-kilix-tui.mp4)**
— part seven of *Kilix, Pleb, and Plebian-OS: A Desktop Built Inside a Terminal*, the ten-part
stack series (1920×1080, 3m14s, 9.2 MB; published as a
[media release](https://github.com/itsmygithubacct/kilix-tui-utils/releases/tag/media-v1) so a
clone stays small). The [full series](https://github.com/itsmygithubacct/plebian-os#watch-the-series) (31m22s)
lives on `plebian-os` and plays at [plebian-os.com](https://plebian-os.com/#watch).

## The desktop

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
