# Safety properties, enforced by tests

Moved verbatim from the README.

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
