# Kilix VirtualBox VPN Manager

A text-native manager for every VM registered with VirtualBox. It obtains the
machine list, power state, resources, snapshots, adapters, guest addresses, and
tunnel interfaces from `VBoxManage`; there is no separate VM list or
`~/.virtualbox_vpn` file to keep synchronized.

Selecting a stopped or saved VM launches it in a new streamed Kilix tab:

```text
kilix run --refit-windows VirtualBoxVM \
  --comment <current VM name> --startvm <uuid> --no-startvm-errormsgbox
```

Selecting a running or paused VM finds the Kilix tab whose foreground command
contains that exact UUID and focuses it. If a VM was started outside Kilix, the
manager reports that it is live but has no Kilix-owned tab rather than opening a
duplicate display.

## Keys

| Key | Action |
|---|---|
| `Up` / `Down` | Select a registered VM |
| `Enter` | Launch a stopped VM, or focus an active VM's tab |
| `a` | Open pause/resume, ACPI shutdown, save, reset, and power controls |
| `Tab`, `1`-`4` | Overview, network, snapshots, and help |
| `r` | Refresh immediately |
| `q` | Quit the manager without changing running VMs |

Shutdown, save-state, reset, and immediate power-off actions show the exact
fixed-argument `VBoxManage` command and require confirmation. Reset and
power-off are additionally marked as destructive.

## Command line

```sh
./kilix-virtualbox-manager/main.py
./kilix-virtualbox-manager/main.py --status
./kilix-virtualbox-manager/main.py --json
./kilix-virtualbox-manager/main.py --vm ubuntuvm6
./kilix-virtualbox-manager/main.py --size 1280x800 --fps 30
./kilix-virtualbox-manager/main.py --screenshot /tmp/vbox-manager.txt
```

The manager is stdlib-only and uses the shared `src/kilix_tui` event loop and
the same Tango text grammar as `kilix-tui/main.py`. `install.sh` publishes it
as `kilix-virtualbox-manager`.
