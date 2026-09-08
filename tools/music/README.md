# Music

Music controls the shared Kilix Amp player. The desktop's Music entry opens a
Kilix tab, using the ordinary terminal handoff when tabs are unavailable.
It attaches to an existing healthy user backend, or starts a headless backend
when none exists. If Amp is absent, the existing pinned Kilix installer runs
off the UI thread. Closing Music stops only a backend that Music started;
an attached player keeps running.

```sh
kilix-music                         # current player and playlist
kilix-music /path/to/music.kenc      # open a local source
kilix-music --encodec-socket /private/audio.sock
```

Use `o` to open a file, `u` to open a live EnCodec Unix stream, and `a` to add
a file or folder to the ordinary playlist. Space toggles playback, `v` stops,
`+`/`-` change volume, and left/right seek five seconds in a seekable file.
EnCodec files seek to the preceding verified epoch boundary.
The footer and `?` show the controls for the current view. Paths containing
spaces remain one argument; no shell interprets source paths.

EnCodec sources show bitrate, mono/stereo profile, selected inference threads,
model readiness and buffering state reported by Amp. Live sources show elapsed
time, recovery and errors; they have no finite duration or seek control. An
ended or failed Unix source reconnects only after `r` or another explicit open.
Music can also control an Amp instance launched with framed EnCodec stdin;
changing stdin requires a new backend launch. The stream framing and decoder
are owned by Amp and `libkilix-encodec`, with the same output-device, EQ, volume
and pan path as windowed Amp.

The control client first sends a read-only protocol-2 ping. A legacy protocol-1
reply triggers a separate protocol-1 ping before ordinary file commands.
Explicit source opens require protocol 2; a mutating command is never retried
as part of negotiation. Protocol-2 build capabilities do not establish model
installation or hardware qualification. The displayed model state comes from
the selected decoder, not from an environment variable or directory check.
Installed-model admission and acquisition belong to Kilix's shared content
service; this front end does not download graphs itself.

The client requires an owned mode-0600 Unix socket inside a private owned
directory. Every ancestor is traversed without following symbolic links, and
the connected peer's UID, PID and socket identity are checked before paths or
commands are sent. A replaced endpoint refuses the command; another request
must negotiate anew. Each exchange has a five-second total deadline and a
one-MiB reply bound, with strict UTF-8 JSON, typed source metadata and exact
unsigned-64 wire timestamps. Closing interrupts this client's pending socket
requests. Existing endpoint files, links and permissions are never repaired or
removed by the TUI.

The default socket is `$XDG_RUNTIME_DIR/kilix-amp.sock`, falling back to
`~/.local/gpu_terminal/kilix/session/kilix-amp.sock`. `KILIX_AMP_SOCKET` selects
another private endpoint. `KILIX_AMP` selects the executable for development;
normal launches resolve the installed, Kilix-pinned player.

```sh
python3 tests/run.py music
python3 tests/music_amp_integration.py --help
```

The music suites exercise real Unix peers, fragmented/trickled/malformed
replies, version negotiation, replacement and permission refusal, bounded
shutdown, live presentation, asynchronous input and the shared tab route.
