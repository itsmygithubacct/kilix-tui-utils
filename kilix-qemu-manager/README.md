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

To tell whether two paths are one file, import and the loader resolve every
symlink, in the directories and in the file name itself, so `/var/run/user/…`
and `/run/user/…` name the same socket.

A hand-edited definition (for example `alpha.json` copied to `bravo.json`)
can still open another VM's files. The loader decides who keeps a control
file from the path itself, never from the order the files load in:

| A pidfile or QMP socket… | Loaded | Refused, listed as a problem |
|---|---|---|
| in the runtime directory, named `<id>.pid` or `<id>.qmp` | the definition with that id | every other definition that opens it, whether or not a VM with that id is catalogued |
| any other file in the runtime directory (for example `<run>/alpha-serial.sock`) | none | every definition that opens it: the manager names everything there after a VM id, and import never puts anything else there |
| elsewhere, opened by more than one definition | none | all of them: nothing tells the original from the copy |

Three hand-made shapes that the r2 loader accepted are refused by these rules.
Import never produces any of them:

- a VM renamed by hand (`alpha.json` → `alpha2.json` with the id edited) that
  keeps `alpha.pid`/`alpha.qmp`. Re-import it under its new id;
- one VM that also opens another socket in the runtime directory (a
  `-chardev socket,path=<run>/…` serial port). Put that socket elsewhere;
- two definitions (two launch profiles) of one build VM that share its
  `qemu.pid` outside the runtime directory. Keep one, or give each its own
  pidfile.

A definition file is also refused, as damage to that file alone, when a path
or argv word holds a NUL byte or text that is not UTF-8 (the OS cannot take
the first; the second cannot be shown). Any other error reading one file
becomes that file's problem line; the other VMs still load.

Each problem line names the file and the path, and says to edit or delete
the file and import the VM again. A socket that two VMs share but that is
neither one's pidfile nor QMP socket (a `-chardev` link between guests) is
not a control file and does not stop either from loading.

The runtime directory is `$XDG_RUNTIME_DIR/kilix-qemu`. Without an absolute
`XDG_RUNTIME_DIR` it falls back to `<tmp>/kilix-qemu-<uid>` (`<tmp>` is
Python's `tempfile.gettempdir()`, normally `/tmp`). Before a launch, either
directory is created mode 0700 or, if it exists, must be a real directory
(not a symlink), owned by you, with no group or other access; otherwise the
launch is refused with the reason, because a shared `/tmp` lets another user
create the fallback first and plant sockets in it. Status reads follow the
same rule without creating anything: in a runtime directory that exists but
is not safely yours, no pidfile, QMP socket or launch marker is read, the VM
shows as stopped with the reason, and no QMP command is sent through it.

Whether a path is in the runtime directory is decided by the directory that
holds the name as written, never by where a symlink in its last component
points, because the owner of that directory is who could have put it there.
For every read and control the directory is opened without following
symlinks, checked through that open descriptor (yours, mode 0700), and the
file is reached through it (`/proc/self/fd/N/<name>`), so the directory
cannot be swapped between the check and the use. The manager creates only
sockets, pidfiles and launch markers there, never symlinks, so a symlink it
finds there is not followed even in a safe directory: the VM reads as stopped
with "… is a symlink", no command is sent through it, and a launch is refused
until you remove it. A path outside the runtime directory that is a symlink
into it is refused the same way.

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
reported, not shown as set. Each update holds a lock, across processes too
(an empty `state.json.lock` beside the state file), so the CLI and a running
picker never lose each other's change. If another manager process holds
that lock for more than five seconds (it was stopped with Ctrl-Z, say), the
update is not made: a favorite says it could not be saved, and a recent is
simply not recorded. A write that crashed between its swap file and the
replace leaves a swap file behind; the next write in that directory removes
such files of yours that are older than ten minutes. Only the manager's own
names are touched: `.<id>.json.XXXXXXXX.tmp` in the `vms` directory and
`.state.json.XXXXXXXX.tmp` beside the state file, which matters when
`KILIX_QEMU_HOME` points at a directory that holds other files.

`install.sh` publishes the manager as `kilix-qemu-manager` and writes a
`Terminal=true` XDG entry so Kilix 95 lists it under Start > Programs >
System. The kilix-tui desktop lists it under Machine beside VirtualBox, and
under Programs, which is the list `kilix-launcher` reads.
