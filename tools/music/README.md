# Music

Music controls the shared Kilix Amp player. The desktop's Music entry opens a
Kilix tab, using the ordinary terminal handoff when tabs are unavailable.
It attaches to an existing healthy user backend, or starts a headless backend
when none exists. If Amp is absent, the existing pinned Kilix installer runs
off the UI thread. Closing Music stops only a backend that Music started;
an attached player keeps running. A canceled or expired startup cannot publish
a successful connection or peer identity after its reply arrives. State construction, imports and rendering
perform no player lookup, setup or process launch.

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
If a state poll is pending when Enter is pressed, the entry stays visible with
a prompt to press Enter again when ready. Source paths use UTF-8 on the wire
within the 4,095-byte path and 8,192-byte request bounds.

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
another private endpoint. Normal owned startup asks the selected host's
`scripts/install-kilix-amp.py --resolve` for the packaged catalog executable.
It does not use an Amp found on PATH or in a development checkout. A missing
host query API refuses with an update/setup message.

The launching host or embedding desktop passes its actual application store
in `KILIX_CONTENT_ROOT`. Music uses that same normalized absolute root for
readiness, explicit setup and the owned backend environment. A bare standalone
launch uses the host's ordinary storage derivation; a declared95 embedding
without its root refuses. For a direct installed `kilix-tui` launch, the caller
sets that environment variable. For the host fallback it must use
`kilix kilix-tui --content-root /absolute/apps` (or `kilix tui` with the same
option immediately after the alias). The host validates and normalizes that
explicit path; an ordinary host launch ignores inherited `KILIX_CONTENT_ROOT`
and uses its actual host store. The query does not create a missing root,
install/build an application, or write model receipts. Content readiness may
refresh byte-identical Git index metadata in an existing checkout; it is not a
promise of zero filesystem writes. Setup uses the same
host installer in an owned supervised process and respects its auto-install
setting. Cancellation/deadline waits for its owned cleanup; it never installs
system/native prerequisites or accepts licenses. Each query is bounded by five
seconds and setup by fifteen minutes, with cancellation support and a separate
bounded allowance to finish owned teardown after interruption.
Owned startup keeps one original five-second budget through catalog query and
protocol negotiation, including the legacy fallback. Cancellation is checked
again after host resolution before any setup/query process starts. A cancel
racing with a real spawn still waits for that owned process to be reaped.

`KILIX_AMP` is an explicit trusted operator executable override. It bypasses
catalog selection and provides no catalog, enabled-codec or model-admission
guarantee. An invalid override refuses instead of falling through to another
binary. Attached existing backends retain their identity/negotiation contract.
Content's general same-ref build-flag cache behavior is unchanged; final Amp
source/build selection belongs to the release catalog.

```sh
python3 tests/run.py music
python3 tests/music_amp_integration.py --help
```

The music suites exercise real Unix peers, fragmented/trickled/malformed
replies, version negotiation, replacement and permission refusal, bounded
shutdown, live presentation, asynchronous input and the shared tab route.
