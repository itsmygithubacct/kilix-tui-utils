# Kilix QEMU Manager

A text-native picker and controller for QEMU virtual machines, the QEMU
sibling of `kilix-virtualbox-manager`. QEMU keeps no registry of machines, so
this manager keeps one: one JSON definition per VM under
`$XDG_CONFIG_HOME/kilix-qemu/vms/` (`KILIX_QEMU_HOME` replaces the root).
A definition holds an id, a display name, the `qemu-system-*` binary, the argv
as a list (never a shell string), the directory relative paths resolve
against, an optional SSH forward, and notes.

Selecting a stopped VM opens it in a new Kilix tab titled `QEMU: <name>`
through the shared `kitty_rc` helper, without taking focus:

```text
kitten @ launch --type=tab --tab-title "QEMU: <name>" --keep-focus -- \
  env KILIX_IN_OVERLAY=1 kilix run [--size WxH] qemu-system-x86_64 <argv…>
```

`KILIX_IN_OVERLAY=1` makes `kilix run` draw in that tab rather than relaunch
itself into a second one. Selecting a running VM focuses the tab whose
foreground command opens that VM's QMP socket. A VM whose pidfile, QMP socket
or disk lock says it is live, but which no Kilix tab owns, is reported as
"live, no Kilix tab", and a second copy is never started.

## Adding VMs

```sh
kilix-qemu-manager import "Scratch" -- qemu-system-x86_64 -m 256 -nodefaults
kilix-qemu-manager import "Dev box" --ssh dev@2222 -- qemu-system-x86_64 …
kilix-qemu-manager discover                  # proposals only
kilix-qemu-manager discover --accept plebian:lab --as "Lab"
kilix-qemu-manager discover --accept pid:4242
```

Discovery proposes `build_vm_image.py` VMs (`qemu-vms/<name>/disk.qcow2`) and
running `qemu-system-*` processes of this user; nothing is added until one
proposal is accepted. Listings never print a full command line.

Import normalises the argv and prints every change: it removes `-daemonize`
and any `-display` other than gtk or sdl, keeps an existing `-pidfile` or
assigns one, and adds `-qmp unix:$XDG_RUNTIME_DIR/kilix-qemu/<id>.qmp,server=on,wait=off`.
It refuses an argv it cannot represent faithfully: stdio-bound serial ports,
monitors, chardevs or gdb stubs, `-nographic`, `-readconfig`,
`-runas`/`-chroot`, and option values that spell an option the manager acts
on. The manager's own QMP socket in another spelling (`server,nowait`) is
recognised and rewritten, never doubled.

Every VM gets its own pidfile and QMP socket, because status and controls go
through them. An argv copied from another VM (`list --json` shows one) is
fixed on import, and each change is printed:

| In the argv | Policy |
|---|---|
| `-pidfile` of another catalogued VM, or in the runtime directory under another VM's name | replaced by this VM's own `<runtime>/<id>.pid` |
| `-qmp`, `-qmp-pretty` or `-monitor` on such a socket | removed; this VM's own `-qmp` is added |
| `-chardev`, `-serial` or another device bound to such a socket | refused: other options can refer to it by id, so it cannot simply be dropped |
| a `-pidfile` or socket outside the runtime directory that no catalogued VM uses | kept: it is the user's or a build's own (for example `build_vm_image.py`'s `qemu.pid`) |

A definition file whose argv opens another catalogued VM's pidfile or QMP
socket (a hand edit) is not loaded; the picker lists it as a problem.

The runtime directory is `$XDG_RUNTIME_DIR/kilix-qemu`. Without an absolute
`XDG_RUNTIME_DIR` it falls back to `<tmp>/kilix-qemu-<uid>` (`<tmp>` is
Python's `tempfile.gettempdir()`, normally `/tmp`). Before a launch, either
directory is created mode 0700 or, if it exists, must be a real directory
(not a symlink), owned by you, with no group or other access; otherwise the
launch is refused with the reason, because a shared `/tmp` lets another user
create the fallback first and plant sockets in it.

## Keys

| Key | Action |
|---|---|
| `Up` / `Down` | Select a VM; favorites sort first, then recent launches |
| `Enter` | Launch a stopped VM, or focus a live VM's Kilix tab |
| `f` | Mark or unmark a favorite |
| `a` | Pause/resume, ACPI shutdown, reset, power off, Open SSH |
| `Tab`, `1`-`3` | Overview, disks and snapshots, help |
| `r` | Refresh now |
| `q` | Quit; running VMs and their tabs are untouched |

Controls go over QMP (`system_powerdown`, `stop`/`cont`, `system_reset`,
`quit`); state comes from `query-status`. Shutdown, reset and power off show
the exact QMP command and need `y`; reset and power off are marked
destructive. A confirmation is bound to the VM it was shown for: if that VM's
definition is removed or changed before `y`, nothing is sent and the manager
says why. It never acts on whichever VM the selection moved to. Disk facts and snapshots come from `qemu-img info -U
--output=json` and are read-only.

## Command line

```sh
kilix-qemu-manager                 # the picker
kilix-qemu-manager --status        # live table
kilix-qemu-manager --json          # live state as JSON
kilix-qemu-manager list [--json]   # the catalog
kilix-qemu-manager --vm Scratch [--size 1280x800]   # launch or focus
kilix-qemu-manager favorite Scratch / unfavorite Scratch
kilix-qemu-manager remove Scratch  # the definition only, never a disk
kilix-qemu-manager --screenshot /tmp/qemu-manager.txt
```

Favorites and the last five launches live in one small state file,
`$XDG_STATE_HOME/kilix-qemu/state.json`, written atomically and only when it
changes; a damaged file reads as empty. A favorite that cannot be saved is
reported, not shown as set.

`install.sh` publishes the manager as `kilix-qemu-manager` and writes a
`Terminal=true` XDG entry so Kilix 95 lists it under Start > Programs >
System. The kilix-tui desktop lists it under Machine beside VirtualBox, and
under Programs, which is the list `kilix-launcher` reads.
