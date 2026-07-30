"""VirtualBox manager tests; no VM is launched or controlled."""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "kilix-virtualbox-manager"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(MANAGER))

from kilix_tui import app, kitty_rc  # noqa: E402
from kilix_virtualbox_manager import backend, model, tui  # noqa: E402


VM_UUID = "4927cbe2-f82a-442a-889b-28dfe928aeac"

INFO = f"""\
name="ubuntuvm6"
ostype="Ubuntu (64-bit)"
UUID="{VM_UUID}"
memory=8192
vram=128
cpus=4
VMState="running"
VMStateChangeTime="2026-07-30T08:45:16.339000000"
CfgFile="/srv/virtualbox/ubuntuvm6/ubuntuvm6.vbox"
nic1="nat"
natnet1="nat"
macaddress1="080027EC9ECE"
cableconnected1="on"
Forwarding(0)="ssh,tcp,127.0.0.1,6999,10.0.2.15,22"
SnapshotName="clean"
CurrentSnapshotName="clean"
GuestAdditionsVersion="7.0.26 r168464"
"""

GUEST = """\
/VirtualBox/GuestInfo/Net/0/Name           = 'enp0s3' @ 2026-07-30T08:45:44Z
/VirtualBox/GuestInfo/Net/0/Status         = 'Up' @ 2026-07-30T08:45:44Z
/VirtualBox/GuestInfo/Net/0/V4/IP          = '10.0.2.15' @ 2026-07-30T08:45:44Z
/VirtualBox/GuestInfo/Net/1/Name           = 'tun0' @ 2026-07-30T08:45:44Z
/VirtualBox/GuestInfo/Net/1/Status         = 'Up' @ 2026-07-30T08:45:44Z
/VirtualBox/GuestInfo/Net/1/V4/IP          = '10.9.4.90' @ 2026-07-30T08:45:44Z
"""


def running_vm() -> model.VirtualMachine:
    return backend.machine_from_info(
        "fallback",
        VM_UUID,
        backend.parse_machine_readable(INFO),
        backend.parse_guest_properties(GUEST),
    )


def stopped_vm() -> model.VirtualMachine:
    return model.VirtualMachine(
        uuid="eecb4b04-fdb2-4b98-9da0-e6042cb772ba",
        name="plebian-shared-libraries",
        state="poweroff",
        os_type="Debian (64-bit)",
        cpus=4,
        memory_mb=4096,
    )


class ParserTests(unittest.TestCase):
    def test_registered_vm_list_preserves_names_and_uuids(self):
        payload = (
            f'"ubuntuvm6" {{{VM_UUID}}}\n'
            '"name with spaces" {eecb4b04-fdb2-4b98-9da0-e6042cb772ba}\n'
        )
        self.assertEqual(backend.parse_vm_list(payload), [
            ("ubuntuvm6", VM_UUID),
            ("name with spaces", "eecb4b04-fdb2-4b98-9da0-e6042cb772ba"),
        ])

    def test_machine_info_includes_tunnel_network_and_forward(self):
        vm = running_vm()
        self.assertEqual(vm.name, "ubuntuvm6")
        self.assertEqual(vm.state, "running")
        self.assertEqual(vm.tunnel_status, "up 10.9.4.90")
        self.assertEqual(vm.guest_addresses, ("10.0.2.15",))
        self.assertEqual(vm.port_forwards[0].host_port, "6999")
        self.assertEqual(vm.current_snapshot, "clean")
        self.assertEqual(vm.host_adapters[0].mode, "nat")

    def test_powered_off_machine_is_startable_and_offline(self):
        vm = stopped_vm()
        self.assertTrue(vm.startable)
        self.assertFalse(vm.active)
        self.assertEqual(vm.tunnel_status, "offline")


class InventoryTests(unittest.TestCase):
    def test_inventory_uses_only_fixed_vboxmanage_queries(self):
        calls: list[tuple[str, ...]] = []

        def runner(argv, _timeout):
            command = tuple(argv)
            calls.append(command)
            suffix = command[1:]
            if suffix == ("list", "vms"):
                return backend.subprocess.CompletedProcess(
                    argv, 0, f'"ubuntuvm6" {{{VM_UUID}}}\n', "")
            if suffix == ("showvminfo", VM_UUID, "--machinereadable"):
                return backend.subprocess.CompletedProcess(argv, 0, INFO, "")
            if suffix == ("guestproperty", "enumerate", VM_UUID):
                return backend.subprocess.CompletedProcess(argv, 0, GUEST, "")
            raise AssertionError(command)

        client = backend.VirtualBoxClient(
            vboxmanage="/usr/bin/VBoxManage",
            virtualboxvm="/usr/lib/virtualbox/VirtualBoxVM",
            kilix="/opt/kilix/bin/kilix",
            runner=runner,
        )
        machines = client.inventory()
        self.assertEqual([vm.name for vm in machines], ["ubuntuvm6"])
        self.assertEqual([call[1] for call in calls],
                         ["list", "showvminfo", "guestproperty"])


class LaunchAndFocusTests(unittest.TestCase):
    def test_stopped_vm_launches_with_kilix_run_in_a_new_tab(self):
        seen: dict[str, object] = {}

        def runner(argv, _timeout):
            seen["probe"] = tuple(argv)
            return backend.subprocess.CompletedProcess(
                argv, 0, 'VMState="running"\n', "")

        def launch_runner(argv, environ, _timeout):
            seen["argv"] = tuple(argv)
            seen["env"] = dict(environ)
            return backend.subprocess.CompletedProcess(argv, 0, "", "")

        client = backend.VirtualBoxClient(
            vboxmanage="/usr/bin/VBoxManage",
            virtualboxvm="/usr/lib/virtualbox/VirtualBoxVM",
            kilix="/opt/kilix/bin/kilix",
            runner=runner,
            launch_runner=launch_runner,
        )
        environment = {
            "KITTY_WINDOW_ID": "10",
            "KITTY_LISTEN_ON": "unix:@kilix-test",
            "KILIX_RC_PASSWORD_FILE": "/tmp/test-password",
            "KILIX_IN_OVERLAY": "1",
        }
        vm = stopped_vm()
        result = client.launch(
            vm,
            run_options=("--size", "1280x800", "--fps", "30"),
            environ=environment,
        )
        self.assertTrue(result.ok)
        self.assertEqual(seen["argv"], (
            "/opt/kilix/bin/kilix",
            "run",
            "--refit-windows",
            "--size", "1280x800",
            "--fps", "30",
            "/usr/lib/virtualbox/VirtualBoxVM",
            "--comment", "plebian-shared-libraries",
            "--startvm", vm.uuid,
            "--no-startvm-errormsgbox",
        ))
        self.assertNotIn("KILIX_IN_OVERLAY", seen["env"])
        self.assertEqual(seen["probe"], (
            "/usr/bin/VBoxManage", "showvminfo", vm.uuid,
            "--machinereadable",
        ))

    def test_launch_does_not_claim_success_for_an_exited_vm_tab(self):
        def runner(argv, _timeout):
            return backend.subprocess.CompletedProcess(
                argv, 0, 'VMState="poweroff"\n', "")

        def launch_runner(argv, _environ, _timeout):
            return backend.subprocess.CompletedProcess(argv, 0, "73\n", "")

        client = backend.VirtualBoxClient(
            vboxmanage="/usr/bin/VBoxManage",
            virtualboxvm="/usr/lib/virtualbox/VirtualBoxVM",
            kilix="/opt/kilix/bin/kilix",
            runner=runner,
            launch_runner=launch_runner,
            launch_wait_seconds=0,
        )
        result = client.launch(stopped_vm(), environ={
            "KITTY_WINDOW_ID": "10",
            "KITTY_LISTEN_ON": "unix:@kilix-test",
            "KILIX_RC_PASSWORD_FILE": "/tmp/test-password",
        })
        self.assertFalse(result.ok)
        self.assertIn("did not enter an active VirtualBox state", result.message)

    def test_launch_refuses_to_take_over_a_non_kilix_terminal(self):
        client = backend.VirtualBoxClient(
            vboxmanage="/usr/bin/VBoxManage",
            virtualboxvm="/usr/lib/virtualbox/VirtualBoxVM",
            kilix="/opt/kilix/bin/kilix",
        )
        result = client.launch(stopped_vm(), environ={})
        self.assertFalse(result.ok)
        self.assertIn("inside Kilix", result.message)

    def test_active_vm_focuses_the_pane_found_by_uuid(self):
        seen: list[str] = []
        client = backend.VirtualBoxClient(
            vboxmanage="/usr/bin/VBoxManage",
            virtualboxvm="/usr/lib/virtualbox/VirtualBoxVM",
            kilix="/opt/kilix/bin/kilix",
            tab_focuser=lambda uuid: seen.append(uuid) or 73,
        )
        result = client.focus(running_vm())
        self.assertTrue(result.ok)
        self.assertEqual(seen, [VM_UUID])
        self.assertEqual(
            result.command,
            ("/opt/kilix/bin/kilix", "focus", "73"),
        )

    def test_kitty_tree_matches_exact_uuid_argument_with_or_without_braces(self):
        payload = [{
            "id": 1,
            "is_focused": True,
            "tabs": [{
                "id": 2,
                "title": "VirtualBoxVM",
                "is_active": True,
                "windows": [{
                    "id": 73,
                    "title": "VirtualBoxVM",
                    "is_focused": True,
                    "foreground_processes": [{
                        "cmdline": [
                            "python3", "apprun.py", "VirtualBoxVM",
                            "--startvm", "{" + VM_UUID + "}",
                        ],
                    }],
                }],
            }],
        }]
        tree = kitty_rc.parse(payload)
        pane = tree.pane_with_argument(VM_UUID)
        self.assertIsNotNone(pane)
        self.assertEqual(pane.id, 73)
        self.assertIsNone(tree.pane_with_argument(VM_UUID[:-1] + "0"))


class FakeClient:
    def __init__(self, machines):
        self.machines = list(machines)
        self.calls: list[tuple[str, str]] = []

    def inventory(self):
        return list(self.machines)

    def execute(self, vm, action, **_kwargs):
        self.calls.append((vm.uuid, action))
        return backend.OperationResult(True, f"did {action}", ("test", action))

    def preview(self, vm, action, **_kwargs):
        return ("VBoxManage", action, vm.uuid)


class TuiTests(unittest.TestCase):
    def test_render_is_bounded_at_normal_and_tiny_sizes(self):
        state = tui.State(
            FakeClient([stopped_vm(), running_vm()]), refresh_seconds=0)
        for height, width in ((30, 110), (24, 80), (8, 30), (3, 8)):
            with self.subTest(size=(height, width)):
                frame = app.render_to_text(
                    tui.render, state, height=height, width=width)
                self.assertLessEqual(len(frame.splitlines()), height)
                for line in frame.splitlines():
                    self.assertLessEqual(len(line), width)
        self.assertIn("VPN MACHINES", app.render_to_text(tui.render, state))
        frame = app.render_to_text(tui.render, state, height=24, width=100)
        self.assertIn("KILIX TUI", frame.splitlines()[0])
        self.assertNotIn("▗", frame)

    def test_enter_launches_stopped_and_focuses_active(self):
        fake = FakeClient([stopped_vm(), running_vm()])
        state = tui.State(fake, refresh_seconds=0)
        state.primary()
        self._wait(state)
        state.selected = 1
        state.primary()
        self._wait(state)
        self.assertEqual(
            [action for _uuid, action in fake.calls],
            ["launch", "focus"],
        )

    def test_dangerous_power_action_requires_confirmation(self):
        state = tui.State(FakeClient([running_vm()]), refresh_seconds=0)
        state.open_actions()
        actions = state.actions
        state.action_index = next(
            index for index, action in enumerate(actions)
            if action.ident == "poweroff")
        state.choose_action()
        self.assertEqual(state.modal, "confirm")
        self.assertEqual(state.confirm_action.ident, "poweroff")
        self.assertIn("VBoxManage poweroff", state.preview(state.confirm_action))

    @staticmethod
    def _wait(state):
        deadline = time.monotonic() + 1
        while state.pending and time.monotonic() < deadline:
            state.maintain()
            time.sleep(0.005)
        if state.pending:
            raise AssertionError("background operation did not complete")


class IntegrationTests(unittest.TestCase):
    def test_installer_publishes_the_manager(self):
        source = (ROOT / "install.sh").read_text()
        self.assertIn(
            '"kilix-virtualbox-manager:kilix-virtualbox-manager"', source)

    def test_legacy_launcher_now_opens_the_manager(self):
        source = (Path.home() / "kilix_launch_vpn.sh").read_text()
        self.assertIn("kilix-virtualbox-manager", source)
        self.assertNotIn(".virtualbox_vpn", source)
        self.assertNotIn('exec "$kilix" run', source)

    def test_manager_never_uses_a_shell_command_string(self):
        source = (MANAGER / "kilix_virtualbox_manager/backend.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)


if __name__ == "__main__":
    unittest.main()
