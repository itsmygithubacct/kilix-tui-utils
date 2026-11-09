# Design

Moved verbatim from the README.

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
