"""QEMU manager tests.

No real VM runs here. A fake ``qemu-system-x86_64`` serves real QMP over a
unix socket, writes its pidfile and locks its disk the way QEMU does; a fake
``kitty_rc`` records tabs instead of opening them; a fake ``qemu-img``
answers ``info``. Every path points into a temporary directory, and the live
Kilix credentials are removed from the environment before anything imports.
"""
from __future__ import annotations

import fcntl
import io
import itertools
import json
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

for _name in ("KITTY_LISTEN_ON", "KILIX_RC_PASSWORD_FILE", "KILIX_RC_PASSWORD",
              "KITTY_WINDOW_ID", "KILIX_LAUNCHER", "KILIX_QEMU_IMG"):
    os.environ.pop(_name, None)

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "kilix-qemu-manager"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(MANAGER))

from kilix_tui import app, kitty_rc, shell, xdgapps  # noqa: E402
from kilix_qemu_manager import (  # noqa: E402
    backend, cli, desktop_entry, discovery, model, qmp, store, tui,
)

KILIX = "/opt/kilix/kilix"
ENTER, ESC = 10, 27

FAKE_QEMU = r'''#!/usr/bin/env python3
"""A stand-in for qemu-system-*: pidfile, QMP on unix sockets, disk locks."""
import fcntl, json, os, signal, socket, struct, sys, threading, time

args = sys.argv[1:]
pidfile, sockets, disks = None, [], []
i = 0
while i < len(args):
    word = args[i]
    if word in ("-pidfile", "-qmp", "-drive") and i + 1 < len(args):
        value = args[i + 1]
        if word == "-pidfile":
            pidfile = value
        elif word == "-qmp" and value.startswith("unix:"):
            sockets.append(value[5:].replace(",,", "\0").split(",")[0]
                           .replace("\0", ","))
        elif word == "-drive":
            for part in value.split(","):
                if part.startswith("file="):
                    disks.append(part[5:])
        i += 2
        continue
    i += 1

log_path = os.environ.get("FAKE_QEMU_LOG", "")
acpi = os.environ.get("FAKE_QEMU_ACPI", "exit")
serve = os.environ.get("FAKE_QEMU_NO_QMP") != "1"
state = {"status": "running"}
held = []

def log(entry):
    if log_path:
        with open(log_path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")

def cleanup(*_):
    for path in sockets + ([pidfile] if pidfile else []):
        try:
            os.unlink(path)
        except OSError:
            pass
    os._exit(0)

signal.signal(signal.SIGTERM, cleanup)
for disk in disks:
    fd = os.open(disk, os.O_RDWR)
    fcntl.fcntl(fd, fcntl.F_OFD_SETLK,
                struct.pack("hhqqi4x", fcntl.F_RDLCK, 0, 100, 1, 0))
    held.append(fd)
if pidfile:
    with open(pidfile, "w") as handle:
        handle.write(f"{os.getpid()}\n")

def reply(conn, payload):
    conn.sendall((json.dumps(payload) + "\r\n").encode())

def session(conn):
    reply(conn, {"QMP": {"version": {"qemu": {"major": 10}}, "capabilities": []}})
    buffer = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            command = json.loads(line).get("execute")
            log({"execute": command})
            if command == "qmp_capabilities":
                reply(conn, {"event": "NOISE", "data": {}})
                reply(conn, {"return": {}})
            elif command == "query-status":
                reply(conn, {"return": {"status": state["status"],
                                        "running": state["status"] == "running"}})
            elif command == "stop":
                state["status"] = "paused"
                reply(conn, {"event": "STOP"})
                reply(conn, {"return": {}})
            elif command == "cont":
                state["status"] = "running"
                reply(conn, {"return": {}})
            elif command == "system_reset":
                reply(conn, {"event": "RESET"})
                reply(conn, {"return": {}})
            elif command == "system_powerdown":
                reply(conn, {"return": {}})
                if acpi == "exit":
                    threading.Timer(0.1, cleanup).start()
            elif command == "quit":
                reply(conn, {"return": {}})
                cleanup()
            else:
                reply(conn, {"error": {"class": "CommandNotFound",
                                       "desc": f"no {command}"}})

def listen(path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(path)
    except OSError:
        pass
    server.bind(path)
    server.listen(4)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=session, args=(conn,), daemon=True).start()

if serve:
    for path in sockets:
        threading.Thread(target=listen, args=(path,), daemon=True).start()
while True:
    time.sleep(1)
'''

FAKE_QEMU_IMG = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_QEMU_IMG_LOG"], "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
print(json.dumps({"filename": sys.argv[-1], "format": "qcow2",
                  "virtual-size": 1073741824, "actual-size": 200704,
                  "snapshots": [{"name": "clean-install", "date-sec": 1,
                                 "vm-state-size": 0}]}))
'''


def _executable(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class FakeRc:
    """kitty_rc's surface, recording tabs instead of opening them.

    Panes mirror the real ``kitten @ ls`` shape (captured read-only from a
    live Kilix): os-window → tabs → windows → foreground_processes, each
    process carrying ``cmdline``, ``cwd`` and ``pid``.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env or {}
        self.launched: list[dict] = []
        self.focused: list[int] = []
        self.windows: list[dict] = []
        self.processes: list[subprocess.Popen] = []
        self.next_id = 300
        self.up = True

    def available(self) -> bool:
        return self.up

    def add_window(self, title: str, processes: list[list[str]]) -> int:
        self.next_id += 1
        self.windows.append({
            "id": self.next_id, "title": title, "is_focused": False,
            "is_active": False, "cmdline": ["/bin/sh"], "cwd": "/",
            "env": {}, "pid": 1000 + self.next_id,
            "foreground_processes": [
                {"cmdline": argv, "cwd": "/", "pid": 2000 + index}
                for index, argv in enumerate(processes)
            ],
        })
        return self.next_id

    def payload(self) -> list[dict]:
        return [{
            "id": 1, "is_active": True, "is_focused": True,
            "tabs": [
                {"id": 10 + index, "title": window["title"],
                 "is_active": index == 0, "is_focused": index == 0,
                 "windows": [window]}
                for index, window in enumerate(self.windows)
            ],
        }]

    def tree(self):
        return kitty_rc.parse(self.payload())

    def focus_pane(self, pane_id: int) -> None:
        self.focused.append(pane_id)

    def launch_tab(self, argv, *, title, cwd=None, keep_focus=True):
        argv = list(argv)
        self.launched.append({"argv": argv, "title": title, "cwd": cwd,
                              "keep_focus": keep_focus})
        if argv[:1] == ["ssh"]:
            return self.add_window(title, [argv])
        assert argv[:2] == ["env", "KILIX_IN_OVERLAY=1"], argv
        run = argv.index("run")
        rest = argv[run + 1:]
        qemu_argv = rest[2:] if rest[:1] == ["--size"] else rest
        process = subprocess.Popen(
            qemu_argv, cwd=cwd or None, env={**os.environ, **self.env},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(process)
        return self.add_window(title, [
            ["/bin/bash", argv[2], "run", *rest],
            ["python3", "/opt/kilix/config/apprun.py", *rest],
            ["Xvfb", "-displayfd", "5", "-auth", "/tmp/x.auth"],
        ])

    def close(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)


class Sandbox(unittest.TestCase):
    """Every test gets private config, state, runtime and data roots."""

    def setUp(self) -> None:
        # Unix socket paths are short; keep the runtime root shallow.
        self.tmp = Path(tempfile.mkdtemp(prefix="qemu-mgr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        runtime = self.tmp / "run"
        runtime.mkdir(mode=0o700)
        self.env = {
            "KILIX_QEMU_HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_DATA_DIRS": str(self.tmp / "none"),
            "XDG_RUNTIME_DIR": str(runtime),
            "KILIX_TUI_STATE": str(self.tmp / "desk.json"),
            "PLEBIAN_OS_SESSION_HOME": str(self.tmp / "plebian-session"),
            "FAKE_QEMU_LOG": str(self.tmp / "qemu.log"),
            "FAKE_QEMU_IMG_LOG": str(self.tmp / "qemu-img.log"),
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        # A wrapper so /proc/<pid>/cmdline starts with the qemu-system name,
        # as a real QEMU's does, rather than with the Python interpreter.
        script = _executable(self.tmp / "fake_qemu.py", FAKE_QEMU)
        self.qemu = _executable(
            bin_dir / "qemu-system-x86_64",
            f'#!/bin/bash\nexec -a "$0" {shlex.quote(sys.executable)} '
            f'{shlex.quote(script)} "$@"\n')
        self.qemu_img = _executable(bin_dir / "qemu-img", FAKE_QEMU_IMG)
        self.rc = FakeRc()
        self.addCleanup(self.rc.close)
        self.started: list[subprocess.Popen] = []
        self.addCleanup(self._stop_started)

    def _stop_started(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)

    def manager(self, **kwargs) -> backend.QemuManager:
        options = dict(rc=self.rc, kilix=KILIX, qemu_img=self.qemu_img,
                       launch_wait_seconds=5, qmp_timeout=0.5)
        options.update(kwargs)
        return backend.QemuManager(**options)

    def define(self, name: str, *args: str, ssh=None, save=True):
        vm, _notes = discovery.definition_from(
            name, self.qemu, ["-m", "256", "-nodefaults", *args],
            cwd=str(self.tmp), ssh=ssh)
        if save:
            store.save(vm)
        return vm

    def start_outside(self, vm, **env) -> subprocess.Popen:
        os.makedirs(model.runtime_dir(), mode=0o700, exist_ok=True)
        process = subprocess.Popen(
            vm.command, env={**os.environ, **env}, cwd=str(self.tmp),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.started.append(process)
        return process

    def wait_for(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition not reached")

    def qmp_up(self, vm) -> bool:
        try:
            qmp.status(vm.qmp, timeout=0.3)
            return True
        except qmp.QmpError:
            return False

    def qmp_log(self) -> list[str]:
        path = Path(self.env["FAKE_QEMU_LOG"])
        if not path.exists():
            return []
        return [json.loads(line)["execute"]
                for line in path.read_text().splitlines()]

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()


# ── import normalisation ─────────────────────────────────────────────────────


class ImportTests(Sandbox):
    def test_import_strips_daemonize_and_foreign_display_and_says_so(self):
        normal = model.normalise(
            "scratch", "qemu-system-x86_64",
            ["-m", "256", "-daemonize", "-display", "none", "-vga", "std"],
            cwd=str(self.tmp))
        self.assertNotIn("-daemonize", normal.argv)
        self.assertNotIn("-display", normal.argv)
        self.assertIn("-vga", normal.argv)
        joined = "\n".join(normal.notes)
        self.assertIn("removed -daemonize", joined)
        self.assertIn("removed -display none", joined)

    def test_import_keeps_gtk_and_sdl_displays(self):
        for display in ("gtk", "gtk,gl=on", "sdl"):
            with self.subTest(display=display):
                normal = model.normalise(
                    "scratch", "qemu-system-x86_64",
                    ["-display", display], cwd=str(self.tmp))
                self.assertEqual(
                    normal.argv[normal.argv.index("-display") + 1], display)

    def test_import_keeps_or_assigns_the_pidfile(self):
        kept = model.normalise(
            "scratch", "qemu-system-x86_64", ["-pidfile", "vm.pid"],
            cwd=str(self.tmp))
        self.assertEqual(kept.pidfile, str(self.tmp / "vm.pid"))
        self.assertEqual(kept.argv.count("-pidfile"), 1)
        assigned = model.normalise(
            "scratch", "qemu-system-x86_64", [], cwd=str(self.tmp))
        self.assertEqual(assigned.pidfile, model.pid_path("scratch"))
        self.assertIn(assigned.pidfile, assigned.argv)

    def test_import_adds_one_qmp_socket_under_the_runtime_dir(self):
        normal = model.normalise(
            "scratch", "qemu-system-x86_64",
            ["-qmp", "unix:/elsewhere/build.sock,server=on,wait=off"],
            cwd=str(self.tmp))
        own = os.path.join(self.env["XDG_RUNTIME_DIR"], "kilix-qemu",
                           "scratch.qmp")
        self.assertEqual(normal.qmp, own)
        self.assertIn(f"unix:{own},server=on,wait=off", normal.argv)
        self.assertEqual(normal.argv.count("-qmp"), 2)   # the build's is kept
        again = model.normalise("scratch", "qemu-system-x86_64",
                                normal.argv, cwd=str(self.tmp))
        self.assertEqual(again.argv, normal.argv)        # idempotent

    def test_import_refuses_what_it_cannot_represent(self):
        refused = {
            "nographic": ["-nographic"],
            "stdio serial": ["-serial", "stdio"],
            "stdio monitor": ["-monitor", "mon:stdio"],
            "stdio chardev": ["-chardev", "stdio,id=c0"],
            "readconfig": ["-readconfig", "vm.cfg"],
            "runas": ["-runas", "nobody"],
            "run-with user": ["-run-with", "user=nobody"],
            "option as value": ["-m", "-daemonize"],
            "missing value": ["-m"],
            "equals syntax": ["-m=256"],
            "two pidfiles": ["-pidfile", "a", "-pidfile", "b"],
            "newline": ["-name", "a\nb"],
        }
        for label, args in refused.items():
            with self.subTest(label):
                with self.assertRaises(model.DefinitionError):
                    model.normalise("scratch", "qemu-system-x86_64", args,
                                    cwd=str(self.tmp))
        for binary in ("/bin/sh", "qemu-system-x86_64 -m 256", ""):
            with self.subTest(binary=binary):
                with self.assertRaises(model.DefinitionError):
                    model.normalise("scratch", binary, [], cwd=str(self.tmp))

    def test_disk_files_are_the_writable_local_images(self):
        files = model.disk_files([
            "-drive", "file=a.qcow2,format=qcow2,if=virtio",
            "-drive", "file=/iso/x.iso,media=cdrom,readonly=on",
            "-drive", "if=pflash,unit=0,readonly=on,file=/fw/CODE.fd",
            "-drive", "if=pflash,unit=1,file=/fw/VARS,,v2.fd",
            "-drive", "file=nbd:localhost:10809",
            "-hdb", "/d/b.img", "positional.img",
            "-blockdev", '{"driver":"file","filename":"/d/c.img"}',
        ], "/vm")
        self.assertEqual(files, ["/vm/a.qcow2", "/fw/VARS,v2.fd", "/d/b.img",
                                 "/vm/positional.img", "/d/c.img"])

    def test_a_tampered_definition_does_not_load(self):
        vm = self.define("Alpha", save=False)
        data = vm.as_json()
        data["argv"] = [word for word in data["argv"] if "qmp" not in word]
        with self.assertRaises(model.DefinitionError):
            model.VmDefinition.from_json(data)
        data = vm.as_json()
        data["argv"].append("-daemonize")
        with self.assertRaises(model.DefinitionError):
            model.VmDefinition.from_json(data)


# ── catalog, favorites, recents, CLI ─────────────────────────────────────────


class CatalogTests(Sandbox):
    def test_cli_import_list_favorite_and_remove_never_touch_disks(self):
        disk = self.tmp / "disk.qcow2"
        disk.write_bytes(b"QFI\xfb")
        code, out, err = self.run_cli(
            "import", "Alpha VM", "--ssh", "tester@2222", "--",
            self.qemu, "-m", "256", "-drive", f"file={disk},format=qcow2",
            "-daemonize")
        self.assertEqual(code, 0, err)
        self.assertIn("note: removed -daemonize", out)
        path = Path(self.env["KILIX_QEMU_HOME"]) / "vms" / "alpha-vm.json"
        stored = json.loads(path.read_text())
        self.assertEqual(stored["ssh"], {"port": 2222, "user": "tester"})
        self.assertIsInstance(stored["argv"], list)

        code, out, _ = self.run_cli("favorite", "Alpha VM")
        self.assertEqual((code, "now a favorite" in out), (0, True))
        code, out, _ = self.run_cli("--status")
        self.assertIn("★ Alpha VM", out)
        self.assertIn("stopped", out)
        code, out, _ = self.run_cli("unfavorite", "alpha-vm")
        self.assertIn("now not a favorite", out)

        code, out, _ = self.run_cli("remove", "alpha-vm")
        self.assertEqual(code, 0)
        self.assertFalse(path.exists())
        self.assertTrue(disk.exists())
        self.assertEqual(disk.read_bytes(), b"QFI\xfb")

    def test_import_dry_run_and_refusal_write_nothing(self):
        code, out, _ = self.run_cli("import", "Dry", "--dry-run", "--",
                                    self.qemu, "-m", "256")
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing was written", out)
        code, _out, err = self.run_cli("import", "Bad", "--", self.qemu,
                                       "-nographic")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertFalse((Path(self.env["KILIX_QEMU_HOME"]) / "vms").exists())

    def test_favorite_writes_only_when_something_changed(self):
        vm = self.define("Alpha")
        writes = []
        real = store._write_json

        def counting(path, data):
            writes.append(path)
            real(path, data)

        with mock.patch.object(store, "_write_json", counting):
            self.assertTrue(store.set_favorite(vm.id, True))
            self.assertFalse(store.set_favorite(vm.id, True))
            code, out, _ = self.run_cli("favorite", "Alpha")
            self.assertIn("already a favorite", out)
            self.assertFalse(store.set_favorite("beta", False))
            store.save_state(store.load_state())
        self.assertEqual(writes, [model.state_path()])

    def test_a_damaged_state_file_degrades_and_never_crashes(self):
        vm = self.define("Alpha")
        path = Path(model.state_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        for damage in ("{not json", "[]", '{"favorites": "alpha"}',
                       '{"favorites": [1, "../x", "alpha"], "recents": [7]}'):
            with self.subTest(damage=damage):
                path.write_text(damage)
                record = store.load_state()
                self.assertIsInstance(record["favorites"], list)
                self.assertEqual(record["recents"], [])
                frame = app.render_to_text(
                    tui.render, tui.State(self.manager(), refresh_seconds=0))
                self.assertIn("Alpha", frame)
        self.assertEqual(store.load_state()["favorites"], [vm.id])

    def test_recents_are_capped_and_favorites_sort_first(self):
        machines = [self.define(name) for name in
                    ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot",
                     "Golf")]
        for vm in machines:
            store.remember_launch(vm.id)
        self.assertEqual(len(store.recents()), store.MAX_RECENTS)
        self.assertEqual(store.recents()[0]["id"], "golf")
        store.set_favorite("bravo", True)
        order = [vm.id for vm in store.ordered(machines)]
        self.assertEqual(order[0], "bravo")
        self.assertEqual(order[1:3], ["golf", "foxtrot"])
        self.assertEqual(order[-1], "alpha")         # fell out of the recents

    def test_discovery_proposes_and_never_adopts_silently(self):
        vm_dir = Path(self.env["PLEBIAN_OS_SESSION_HOME"]) / "qemu-vms" / "lab"
        vm_dir.mkdir(parents=True)
        (vm_dir / "disk.qcow2").write_bytes(b"")
        (vm_dir / "OVMF_CODE.fd").write_bytes(b"")
        (vm_dir / "OVMF_VARS.fd").write_bytes(b"")
        proc = self.tmp / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cmdline").write_bytes(b"\0".join([
            b"qemu-system-x86_64", b"-name", b"guest=probe,debug-threads=on",
            b"-m", b"512", b"-display", b"none", b"-secretflag", b"hunter2",
            b""]))
        (proc / "4243").mkdir()
        (proc / "4243" / "cmdline").write_bytes(b"bash\0-l\0")
        manager = self.manager(proc_root=str(proc))
        with mock.patch.object(cli, "QemuManager", return_value=manager):
            code, out, _ = self.run_cli("discover")
            self.assertEqual(code, 0)
            self.assertIn("plebian:lab", out)
            self.assertIn("pid:4242", out)
            self.assertIn("probe", out)
            self.assertNotIn("hunter2", out)           # no full command line
            self.assertNotIn("4243", out)
            self.assertEqual(store.load_all(), ([], []))

            code, out, err = self.run_cli("discover", "--accept", "pid:4242",
                                          "--as", "Probe")
            self.assertEqual(code, 0, err)
            self.assertIn("removed -display none", out)
            code, out, err = self.run_cli("discover", "--accept", "plebian:lab")
            self.assertEqual(code, 0, err)
        machines, _ = store.load_all()
        self.assertEqual(sorted(vm.id for vm in machines), ["lab", "probe"])
        lab = next(vm for vm in machines if vm.id == "lab")
        self.assertIn(str(vm_dir / "qemu.pid"), lab.argv)
        self.assertIn(str(vm_dir / "disk.qcow2"), lab.disks)
        self.assertIn(str(vm_dir / "OVMF_VARS.fd"), lab.disks)


# ── QMP ──────────────────────────────────────────────────────────────────────


class FakeQmpServer:
    def __init__(self, path: str, replies: dict[str, dict]) -> None:
        self.path = path
        self.replies = replies
        self.seen: list[str] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(2)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b'{"QMP": {"version": {}, "capabilities": []}}\r\n')
                buffer = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        command = json.loads(line)["execute"]
                        self.seen.append(command)
                        reply = self.replies.get(command, {"return": {}})
                        if reply == "hangup":
                            conn.close()
                            break
                        conn.sendall(b'{"event": "X"}\n'
                                     + json.dumps(reply).encode() + b"\n")
                    else:
                        continue
                    break

    def close(self) -> None:
        self.server.close()


class QmpTests(Sandbox):
    def test_status_skips_events_and_reports_errors_and_hangups(self):
        path = str(self.tmp / "q.sock")
        server = FakeQmpServer(path, {
            "query-status": {"return": {"status": "paused"}},
            "bogus": {"error": {"desc": "nope"}},
            "quit": "hangup",
        })
        self.addCleanup(server.close)
        self.assertEqual(qmp.status(path), "paused")
        with self.assertRaisesRegex(qmp.QmpError, "nope"):
            qmp.execute(path, "bogus")
        self.assertEqual(qmp.execute(path, "quit", allow_hangup=True), {})
        self.assertEqual(server.seen.count("qmp_capabilities"), 3)
        with self.assertRaisesRegex(qmp.QmpError, "no QMP socket"):
            qmp.status(str(self.tmp / "absent.sock"))


# ── launch, focus, liveness, controls ────────────────────────────────────────


class BackendTests(Sandbox):
    def test_launch_uses_kitty_rc_with_keep_focus_title_and_kilix_run(self):
        vm = self.define("Alpha", "-display", "gtk")
        manager = self.manager()
        result = manager.launch(vm, size="1024x768")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(self.rc.launched), 1)
        tab = self.rc.launched[0]
        self.assertEqual(tab["title"], "QEMU: Alpha")
        self.assertTrue(tab["keep_focus"])
        self.assertEqual(tab["argv"][:6], [
            "env", "KILIX_IN_OVERLAY=1", KILIX, "run", "--size", "1024x768"])
        self.assertEqual(tab["argv"][6:], vm.command)
        self.assertEqual(qmp.status(vm.qmp), "running")
        self.assertEqual(store.recents()[0]["id"], vm.id)
        status = manager.status(vm, self.rc.tree())
        self.assertEqual((status.state, bool(status.pid), status.qmp_ok),
                         ("running", True, True))
        self.assertTrue(status.pane_id)
        self.assertEqual(status.label, "running")

    def test_launch_outside_kilix_is_refused_before_anything_starts(self):
        vm = self.define("Alpha")
        self.rc.up = False
        result = self.manager().launch(vm)
        self.assertFalse(result.ok)
        self.assertIn("inside Kilix", result.message)
        self.assertEqual(self.rc.launched, [])

    def test_a_vm_running_outside_kilix_is_never_launched_twice(self):
        vm = self.define("Alpha")
        self.start_outside(vm)
        self.wait_for(lambda: self.qmp_up(vm))
        manager = self.manager()
        status = manager.status(vm, self.rc.tree())
        self.assertEqual(status.label, "live, no Kilix tab")
        result = manager.launch(vm)
        self.assertFalse(result.ok)
        self.assertIn("not opening a second copy", result.message)
        self.assertEqual(self.rc.launched, [])

    def test_each_liveness_witness_alone_blocks_a_second_launch(self):
        # QMP silent: only the pidfile says the VM is up.
        vm = self.define("Pidonly")
        self.start_outside(vm, FAKE_QEMU_NO_QMP="1")
        self.wait_for(lambda: os.path.exists(vm.pidfile))
        result = self.manager().launch(vm)
        self.assertFalse(result.ok)
        self.assertIn("pid", result.message)
        # No pidfile and no QMP: only the disk lock says an image is in use.
        disk = self.tmp / "shared.img"
        disk.write_bytes(b"\0" * 512)
        locked = self.define("Lockonly", "-drive", f"file={disk},format=raw")
        fd = os.open(disk, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.fcntl(fd, fcntl.F_OFD_SETLK,
                    struct.pack("hhqqi4x", fcntl.F_RDLCK, 0, 101, 1, 0))
        self.assertTrue(backend.disk_locked(str(disk)))
        result = self.manager().launch(locked)
        self.assertFalse(result.ok)
        self.assertIn("disk in use", result.message)
        self.assertEqual(self.rc.launched, [])

    def test_a_recycled_pid_in_a_stale_pidfile_is_not_a_witness(self):
        vm = self.define("Alpha")
        Path(vm.pidfile).parent.mkdir(parents=True, exist_ok=True)
        Path(vm.pidfile).write_text(f"{os.getpid()}\n")     # not a QEMU
        self.assertEqual(self.manager().pid_of(vm), 0)
        # A live QEMU of this user, but another VM's: its argv opens none of
        # Alpha's files, so the stale pidfile must not make Alpha live.
        other = self.define("Other")
        process = self.start_outside(other)
        self.wait_for(lambda: self.qmp_up(other))
        Path(vm.pidfile).write_text(f"{process.pid}\n")
        self.assertEqual(self.manager().pid_of(other), process.pid)
        self.assertEqual(self.manager().pid_of(vm), 0)
        self.assertEqual(self.manager().status(vm).state, "stopped")
        self.assertTrue(self.manager().launch(vm).ok)

    def test_enter_on_a_running_vm_focuses_only_its_own_tab(self):
        alpha = self.define("Alpha")
        beta = self.define("Alphabet")
        socket_dir = os.path.dirname(alpha.qmp)
        # Decoys: a tab titled for Alpha that runs another VM, and a socket
        # whose path merely starts with Alpha's.
        self.rc.add_window("QEMU: Alpha", [
            ["qemu-system-x86_64", "-qmp", model.qmp_spec(beta.qmp)]])
        self.rc.add_window("QEMU: old", [
            ["qemu-system-x86_64", "-qmp",
             f"unix:{alpha.qmp}.old,server=on,wait=off"]])
        self.rc.add_window("QEMU: other", [
            ["qemu-system-x86_64", "-qmp",
             f"unix:{socket_dir}/x{os.path.basename(alpha.qmp)},server=on"]])
        right = self.rc.add_window("Alpha renamed", [
            ["/bin/bash", KILIX, "run", *alpha.command],
            ["python3", "apprun.py", *alpha.command],
            ["Xvfb", "-displayfd", "5"],
        ])
        self.start_outside(alpha)
        self.wait_for(lambda: self.qmp_up(alpha))
        state = tui.State(self.manager(), refresh_seconds=0)
        state.select_id(alpha.id)
        self.assertEqual(state.selected_vm.pane_id, right)
        tui.handle(ENTER, state)
        TuiTests.wait(state)
        self.assertEqual(self.rc.focused, [right])
        self.assertEqual(self.rc.launched, [])

    def test_qmp_controls_follow_query_status(self):
        vm = self.define("Alpha")
        self.start_outside(vm, FAKE_QEMU_ACPI="ignore")
        self.wait_for(lambda: self.qmp_up(vm))
        manager = self.manager()
        self.assertTrue(manager.control(vm, "pause").ok)
        self.assertEqual(manager.status(vm).state, "paused")
        self.assertTrue(manager.control(vm, "resume").ok)
        self.assertEqual(manager.status(vm).state, "running")
        self.assertTrue(manager.control(vm, "reset").ok)
        self.assertTrue(manager.control(vm, "shutdown").ok)   # guest ignores it
        self.assertTrue(manager.status(vm).live)
        result = manager.control(vm, "poweroff")
        self.assertTrue(result.ok, result.message)
        self.wait_for(lambda: manager.status(vm).state == "stopped")
        self.assertEqual(
            [c for c in self.qmp_log() if c not in ("qmp_capabilities",
                                                    "query-status")],
            ["stop", "cont", "system_reset", "system_powerdown", "quit"])

    def test_open_ssh_and_disk_info_are_fixed_argv(self):
        disk = self.tmp / "disk.qcow2"
        disk.write_bytes(b"\0" * 512)
        vm = self.define("Alpha", "-drive", f"file={disk},format=qcow2",
                         ssh=(2222, "tester"))
        result = self.manager().open_ssh(vm)
        self.assertTrue(result.ok)
        self.assertEqual(self.rc.launched[-1]["argv"],
                         ["ssh", "-p", "2222", "tester@127.0.0.1"])
        self.assertTrue(self.rc.launched[-1]["keep_focus"])
        rows = self.manager().disk_info(vm)
        self.assertEqual(rows[0]["snapshots"][0]["name"], "clean-install")
        logged = [json.loads(line) for line in
                  Path(self.env["FAKE_QEMU_IMG_LOG"]).read_text().splitlines()]
        self.assertEqual(logged, [["info", "-U", "--output=json", str(disk)]])


# ── the TUI, driven through its key handler ──────────────────────────────────


class TuiTests(Sandbox):
    @staticmethod
    def wait(state, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while state.pending and time.monotonic() < deadline:
            state.maintain()
            time.sleep(0.01)
        if state.pending:
            raise AssertionError("background operation did not complete")

    def choose(self, state, ident: str) -> None:
        """Move the action cursor to `ident` with Down, as a user would."""
        for _ in range(len(state.actions) + 1):
            if state.actions and state.actions[state.action_index].ident == ident:
                return
            tui.handle(258, state)
        self.fail(f"no {ident} action offered: "
                  f"{[action.ident for action in state.actions]}")

    def test_enter_launches_a_stopped_vm_then_focuses_it(self):
        vm = self.define("Alpha")
        state = tui.State(self.manager(), refresh_seconds=0)
        tui.handle(ENTER, state)
        self.wait(state)
        self.assertEqual(len(self.rc.launched), 1, state.error)
        self.assertEqual(state.selected_vm.state, "running")
        tui.handle(ENTER, state)
        self.wait(state)
        self.assertEqual(len(self.rc.launched), 1)        # no second copy
        self.assertEqual(self.rc.focused, [state.selected_vm.pane_id])
        self.assertTrue(self.qmp_up(vm))

    def test_enter_on_a_live_vm_without_a_tab_reports_and_does_nothing(self):
        vm = self.define("Alpha")
        self.start_outside(vm)
        self.wait_for(lambda: self.qmp_up(vm))
        state = tui.State(self.manager(), refresh_seconds=0)
        tui.handle(ENTER, state)
        self.assertIn("live, no Kilix tab", state.error)
        self.assertEqual((self.rc.launched, self.rc.focused), ([], []))
        frame = app.render_to_text(tui.render, state, height=24, width=100)
        self.assertIn("LIVE, NO KILIX TAB", frame)

    def test_f_toggles_a_favorite_and_it_sorts_first(self):
        for name in ("Alpha", "Bravo", "Charlie"):
            self.define(name)
        state = tui.State(self.manager(), refresh_seconds=0)
        self.assertEqual(state.selected_vm.vm.name, "Alpha")
        tui.handle(258, state)                       # Down
        tui.handle(258, state)
        self.assertEqual(state.selected_vm.vm.name, "Charlie")
        tui.handle(ord("f"), state)
        self.assertEqual([s.vm.name for s in state.machines],
                         ["Charlie", "Alpha", "Bravo"])
        self.assertEqual(state.selected_vm.vm.name, "Charlie")
        self.assertTrue(state.selected_vm.favorite)
        self.assertEqual(store.favorites(), ["charlie"])
        frame = app.render_to_text(tui.render, state, height=24, width=100)
        self.assertIn("★ Charlie", frame)
        tui.handle(ord("f"), state)
        self.assertEqual(store.favorites(), [])

    def test_destructive_actions_need_confirmation_showing_the_command(self):
        vm = self.define("Alpha")
        self.start_outside(vm)
        self.wait_for(lambda: self.qmp_up(vm))
        state = tui.State(self.manager(), refresh_seconds=0)
        for ident, command in (("reset", "system_reset"), ("poweroff", "quit")):
            with self.subTest(ident):
                tui.handle(ord("a"), state)
                self.assertEqual(state.modal, "actions")
                self.choose(state, ident)
                tui.handle(ENTER, state)
                self.assertEqual(state.modal, "confirm")
                self.assertTrue(state.confirm_action.danger)
                frame = app.render_to_text(tui.render, state,
                                           height=24, width=120)
                self.assertIn(f'{{"execute": "{command}"}}', frame)
                self.assertIn(vm.qmp, frame.replace("\n ", ""))
                self.assertNotIn(command, self.qmp_log())
                tui.handle(ord("n"), state)
                self.assertEqual(state.modal, "")
                self.assertIn("Cancelled", state.message)
                self.assertNotIn(command, self.qmp_log())
        tui.handle(ord("a"), state)
        self.choose(state, "poweroff")
        tui.handle(ENTER, state)
        tui.handle(ord("y"), state)
        self.wait(state)
        self.assertIn("quit", self.qmp_log())
        self.wait_for(lambda: not self.qmp_up(vm))

    def test_render_is_bounded_and_uses_the_shared_shell(self):
        for name in ("Alpha", "A rather long virtual machine name indeed"):
            self.define(name)
        state = tui.State(self.manager(), refresh_seconds=0)
        for tab in range(len(tui.DETAIL_TABS)):
            state.detail_tab = tab
            for height, width in ((30, 120), (24, 80), (10, 40), (3, 8)):
                with self.subTest(tab=tab, size=(height, width)):
                    frame = app.render_to_text(tui.render, state,
                                               height=height, width=width)
                    self.assertLessEqual(len(frame.splitlines()), height)
                    for line in frame.splitlines():
                        self.assertLessEqual(len(line), width)
        state.detail_tab = 0
        frame = app.render_to_text(tui.render, state, height=24, width=100)
        lines = frame.splitlines()
        self.assertIn("KILIX TUI", lines[0])
        self.assertIn("QEMU VMs", lines[0])
        self.assertIn("▶1", lines[1])
        self.assertIn(tui.TITLE, shell.TIPS)
        self.assertFalse(tui.handle(ord("q"), state))

    def test_screenshot_writes_one_frame(self):
        self.define("Alpha")
        target = self.tmp / "frame.txt"
        with mock.patch.object(cli, "QemuManager",
                               return_value=self.manager()):
            code, _out, err = self.run_cli("--screenshot", str(target))
        self.assertEqual(code, 0, err)
        self.assertIn("Alpha", target.read_text())


# ── installer, desktop entry, Start menu ─────────────────────────────────────


class IntegrationTests(Sandbox):
    def test_installer_publishes_the_manager_and_its_start_menu_entry(self):
        source = (ROOT / "install.sh").read_text()
        self.assertIn('"kilix-qemu-manager:kilix-qemu-manager"', source)
        stage = self.tmp / "staged ' tree"
        stage.mkdir()
        shutil.copy2(ROOT / "install.sh", stage / "install.sh")
        shutil.copytree(ROOT / "src", stage / "src")
        shutil.copytree(MANAGER, stage / MANAGER.name)
        prefix = self.tmp / "prefix"
        environment = dict(os.environ, HOME=str(self.tmp / "home-dir"),
                           KILIX_TUI_UTILS_PREFIX=str(prefix),
                           KILIX_TUI_UTILS_SYNC_MENU="1",
                           PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run(["bash", str(stage / "install.sh")],
                              env=environment, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        launcher = prefix / "bin" / "kilix-qemu-manager"
        shown = subprocess.run([str(launcher), "--help"], env=environment,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("usage: kilix-qemu-manager", shown.stdout)
        entry = Path(self.env["XDG_DATA_HOME"]) / "applications" / \
            desktop_entry.FILENAME
        text = entry.read_text()
        self.assertIn(f"Exec={launcher}\n", text)
        self.assertIn("Terminal=true\n", text)
        # The shared scanner Kilix 95's Start menu uses files it under System.
        with mock.patch.dict(os.environ, {
                "PATH": f"{prefix / 'bin'}:{os.environ.get('PATH', '')}"}):
            groups = xdgapps.grouped(force=True)
        rows = [row for row in groups.get("System", [])
                if row["name"] == "QEMU VMs"]
        self.assertEqual(len(rows), 1, groups)
        self.assertTrue(rows[0]["terminal"])

    def test_desktop_entry_never_replaces_a_foreign_file(self):
        directory = self.tmp / "apps"
        directory.mkdir()
        foreign = directory / desktop_entry.FILENAME
        foreign.write_text("[Desktop Entry]\nName=Mine\n")
        with self.assertRaises(FileExistsError):
            desktop_entry.install("/usr/bin/kilix-qemu-manager", str(directory))
        self.assertEqual(foreign.read_text(), "[Desktop Entry]\nName=Mine\n")
        foreign.unlink()
        self.assertTrue(desktop_entry.install("/x/kilix-qemu-manager",
                                              str(directory))[1])
        self.assertFalse(desktop_entry.install("/x/kilix-qemu-manager",
                                               str(directory))[1])
        self.assertIsNone(desktop_entry.exec_word('/a"b'))

    def test_manager_never_uses_a_shell_command_string(self):
        for path in (MANAGER / "kilix_qemu_manager").glob("*.py"):
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotIn("shell=True", text)
                self.assertNotIn("os.system", text)



# ── fix round r2: findings of review QM-R1 ───────────────────────────────────


def _proc_entry(root: Path, pid: int, argv: list[str]) -> str:
    """A synthetic /proc/<pid>/cmdline under `root`; returns the proc root."""
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "cmdline").write_bytes(
        b"\0".join(word.encode() for word in argv) + b"\0")
    return str(root)


class ConfirmTargetTests(Sandbox):
    """B1: a confirmation acts on the VM it was shown for, or on nothing."""

    def two_running(self):
        alpha, bravo = self.define("Alpha"), self.define("Bravo")
        self.start_outside(alpha)
        self.start_outside(bravo)
        self.wait_for(lambda: self.qmp_up(alpha) and self.qmp_up(bravo))
        state = tui.State(self.manager(), refresh_seconds=0)
        state.select_id(alpha.id)
        return alpha, bravo, state

    def confirm_poweroff_for_alpha(self, state, alpha):
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "poweroff")
        tui.handle(ENTER, state)
        self.assertEqual(state.modal, "confirm")
        frame = app.render_to_text(tui.render, state, height=24, width=120)
        self.assertIn("Confirm: Power off — Alpha", frame)

    def test_y_after_the_confirmed_vm_vanished_refuses_and_spares_the_next(self):
        alpha, bravo, state = self.two_running()
        self.confirm_poweroff_for_alpha(state, alpha)
        os.unlink(store._definition_path(alpha.id))
        state.refresh()                       # what the 3 s timer does
        self.assertEqual(state.selected_vm.vm.id, bravo.id)
        frame = app.render_to_text(tui.render, state, height=24, width=120)
        self.assertIn("Confirm: Power off — Alpha", frame)    # never Bravo
        self.assertIn(alpha.qmp, frame.replace("\n ", ""))
        self.assertIn("Alpha is gone from the catalog", frame)
        tui.handle(ord("y"), state)
        TuiTests.wait(state)
        self.assertEqual(state.modal, "")
        self.assertIn("Not done: Power off — Alpha is no longer in the catalog",
                      state.error)
        self.assertNotIn("quit", self.qmp_log())
        self.assertTrue(self.qmp_up(alpha) and self.qmp_up(bravo))

    def test_y_after_the_definition_changed_refuses(self):
        alpha, bravo, state = self.two_running()
        self.confirm_poweroff_for_alpha(state, alpha)
        alpha.notes = "edited in another tab"
        store.save(alpha, replace=True)        # same id, other definition
        tui.handle(ord("y"), state)
        TuiTests.wait(state)
        self.assertIn("definition changed since it was shown", state.error)
        self.assertNotIn("quit", self.qmp_log())
        self.assertTrue(self.qmp_up(alpha) and self.qmp_up(bravo))

    def test_y_on_an_unchanged_vm_acts_on_it_even_if_selection_moved(self):
        alpha, bravo, state = self.two_running()
        self.confirm_poweroff_for_alpha(state, alpha)
        state.selected = [s.vm.id for s in state.machines].index(bravo.id)
        tui.handle(ord("y"), state)
        TuiTests.wait(state)
        self.assertEqual(state.message, "Powered off Alpha.")
        self.wait_for(lambda: not self.qmp_up(alpha))
        self.assertTrue(self.qmp_up(bravo))

    def test_the_actions_menu_is_bound_to_its_vm_too(self):
        alpha, bravo, state = self.two_running()
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "pause")
        os.unlink(store._definition_path(alpha.id))
        state.refresh()
        self.assertEqual(state.actions, [])    # Alpha's actions, now none
        tui.handle(ENTER, state)
        TuiTests.wait(state)
        self.assertNotIn("stop", self.qmp_log())
        self.assertEqual(manager_state(self, bravo), "running")


def manager_state(case, vm) -> str:
    return case.manager().status(vm).state


class ForeignControlFileTests(Sandbox):
    """B2: import never keeps another VM's pidfile or QMP socket."""

    def test_a_cloned_argv_gets_its_own_pidfile_and_qmp_and_says_so(self):
        alpha = self.define("Alpha")
        code, out, err = self.run_cli("import", "Bravo", "--", self.qemu,
                                      *alpha.argv)
        self.assertEqual(code, 0, err)
        bravo = store.get("bravo")
        self.assertNotIn(alpha.pidfile, bravo.argv)
        self.assertFalse(any(alpha.qmp in word for word in bravo.argv))
        self.assertEqual(bravo.argv.count("-qmp"), 1)
        self.assertEqual(bravo.argv.count("-pidfile"), 1)
        self.assertEqual((bravo.pidfile, bravo.qmp),
                         (model.pid_path("bravo"), model.qmp_path("bravo")))
        self.assertIn(f"replaced -pidfile {alpha.pidfile}: it is VM alpha's",
                      out)
        self.assertIn("it is VM alpha's socket", out)

    def test_the_clone_running_never_makes_the_original_read_live(self):
        alpha = self.define("Alpha")
        bravo, _notes = discovery.definition_from(
            "Bravo", self.qemu, list(alpha.argv), cwd=str(self.tmp))
        store.save(bravo)
        self.start_outside(bravo)
        self.wait_for(lambda: self.qmp_up(bravo))
        manager = self.manager()
        self.assertEqual(manager.status(alpha).state, "stopped")
        self.assertFalse(manager.control(alpha, "poweroff").ok)
        self.assertTrue(self.qmp_up(bravo))

    def test_files_in_the_runtime_dir_named_for_another_vm_are_replaced(self):
        ghost_pid = model.pid_path("ghost")      # no VM "ghost" is catalogued
        ghost_qmp = model.qmp_path("ghost")
        normal = model.normalise(
            "scratch", "qemu-system-x86_64",
            ["-pidfile", ghost_pid, "-qmp", f"unix:{ghost_qmp},server,nowait",
             "-monitor", f"unix:{ghost_qmp}.hmp,server,nowait"],
            cwd=str(self.tmp))
        self.assertEqual(normal.pidfile, model.pid_path("scratch"))
        self.assertFalse(any("ghost" in word for word in normal.argv))
        self.assertEqual(normal.argv.count("-qmp"), 1)
        self.assertNotIn("-monitor", normal.argv)

    def test_a_catalogued_vms_files_outside_the_runtime_dir_are_replaced(self):
        build = self.tmp / "build"
        build.mkdir()
        first = self.define("First", "-pidfile", str(build / "qemu.pid"))
        self.assertEqual(first.pidfile, str(build / "qemu.pid"))   # kept
        second, notes = discovery.definition_from(
            "Second", self.qemu, ["-pidfile", str(build / "qemu.pid")],
            cwd=str(self.tmp))
        self.assertEqual(second.pidfile, model.pid_path("second"))
        self.assertIn("it is VM first's pidfile", "\n".join(notes))

    def test_unclaimed_files_outside_the_runtime_dir_are_the_users_and_kept(self):
        mine = str(self.tmp / "mine.pid")
        normal = model.normalise(
            "scratch", "qemu-system-x86_64",
            ["-pidfile", mine, "-qmp", "unix:/elsewhere/build.sock,server=on"],
            cwd=str(self.tmp))
        self.assertEqual(normal.pidfile, mine)
        self.assertIn("unix:/elsewhere/build.sock,server=on", normal.argv)

    def test_a_chardev_on_another_vms_socket_is_refused(self):
        alpha = self.define("Alpha")
        for args in (["-chardev", f"socket,id=m0,path={alpha.qmp},server=on",
                      "-mon", "chardev=m0,mode=control"],
                     ["-serial", f"unix:{alpha.qmp},server=on"]):
            with self.subTest(args=args[0]):
                with self.assertRaisesRegex(model.DefinitionError,
                                            "VM alpha's socket"):
                    discovery.definition_from("Bravo", self.qemu, args,
                                              cwd=str(self.tmp))

    def test_a_definition_that_opens_another_vms_files_does_not_load(self):
        alpha = self.define("Alpha")
        clone = alpha.as_json()           # what an r1 import would have kept
        clone.update(id="bravo", name="Bravo", pidfile=model.pid_path("bravo"),
                     qmp=model.qmp_path("bravo"))
        clone["argv"] = [*alpha.argv[:-4], "-pidfile", model.pid_path("bravo"),
                         "-qmp", model.qmp_spec(alpha.qmp),
                         "-qmp", model.qmp_spec(model.qmp_path("bravo"))]
        model.VmDefinition.from_json(clone)          # valid on its own
        Path(store._definition_path("bravo")).write_text(json.dumps(clone))
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertEqual(len(problems), 1)
        self.assertIn(f"bravo.json: not loaded: it opens {alpha.qmp}, the "
                      "manager's QMP socket for VM alpha", problems[0])

    def test_own_qmp_in_the_old_spelling_is_recognised_not_doubled(self):
        own = model.qmp_path("scratch")
        normal = model.normalise(
            "scratch", "qemu-system-x86_64",
            ["-qmp", f"unix:{own},server,nowait"], cwd=str(self.tmp))
        self.assertEqual(normal.argv, [
            "-qmp", model.qmp_spec(own), "-pidfile", model.pid_path("scratch")])
        self.assertIn("rewrote -qmp", "\n".join(normal.notes))
        with self.assertRaisesRegex(model.DefinitionError, "this VM's QMP"):
            model.normalise("scratch", "qemu-system-x86_64",
                            ["-monitor", f"unix:{own},server,nowait"],
                            cwd=str(self.tmp))


class GrammarTests(Sandbox):
    """N3, N4 and the import mutants that survived r1 (R02-R04)."""

    # The no-value options of QEMU 10.0's invocation docs. (The docs also
    # list -iscsi without one; it takes a value, so it is not a flag.)
    QEMU_10_FLAGS = (
        "S", "daemonize", "enable-kvm", "enable-sync-profile", "full-screen",
        "h", "jitdump", "mem-prealloc", "no-fd-bootchk", "no-reboot",
        "no-shutdown", "no-user-config", "nodefaults", "nographic",
        "old-param", "only-migratable", "perfmap", "s", "semihosting",
        "snapshot", "usb", "version", "win2k-hack", "xen-attach")

    def normalise(self, *args):
        return model.normalise("t", "qemu-system-x86_64", list(args),
                               cwd=str(self.tmp))

    def test_every_documented_no_value_option_is_a_flag(self):
        self.assertEqual(set(self.QEMU_10_FLAGS) - model.FLAGS, set())

    def test_mem_prealloc_takes_no_value(self):
        normal = self.normalise("-mem-prealloc", "-drive", "file=d.qcow2")
        self.assertEqual(normal.argv[:3], ["-mem-prealloc", "-drive",
                                           "file=d.qcow2"])
        self.assertEqual(model.disk_files(
            ["-mem-path", "/dev/hugepages", "-mem-prealloc", "-m", "4096",
             "-drive", "file=d.qcow2"], "/vm"), ["/vm/d.qcow2"])
        self.assertNotIn("-daemonize",
                         self.normalise("-mem-prealloc", "-daemonize").argv)

    def test_gdb_on_stdio_is_refused(self):
        with self.assertRaises(model.DefinitionError):
            self.normalise("-gdb", "stdio")

    def test_double_dash_daemonize_and_curses_are_removed(self):
        normal = self.normalise("--daemonize", "-m", "256", "-curses")
        self.assertEqual(normal.argv[:2], ["-m", "256"])
        self.assertNotIn("--daemonize", normal.argv)
        self.assertNotIn("-curses", normal.argv)
        self.assertIn("removed -curses", "\n".join(normal.notes))

    def test_run_with_chroot_is_refused(self):
        with self.assertRaises(model.DefinitionError):
            self.normalise("-run-with", "chroot=/srv/x")

    def test_dotted_and_nested_blockdevs_and_pflash_are_lock_checked(self):
        self.assertEqual(model.disk_files([
            "-blockdev", "driver=qcow2,node-name=a,file.driver=file,"
                         "file.filename=/vm/a.qcow2",
            "-blockdev", "driver=qcow2,file.driver=file,file.read-only=on,"
                         "file.filename=/vm/ro.qcow2",
            "-blockdev", json.dumps({"driver": "qcow2", "file": {
                "driver": "file", "filename": "/vm/n.qcow2"}}),
            "-pflash", "/fw/VARS.fd",
        ], "/vm"), ["/vm/a.qcow2", "/vm/n.qcow2", "/fw/VARS.fd"])


class RuntimeDirTests(Sandbox):
    """N7: a runtime directory is used only when it is safely this user's."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("XDG_RUNTIME_DIR")          # restored by patch.dict
        self.shared = self.tmp / "shared-tmp"
        self.shared.mkdir()
        patcher = mock.patch.object(model.tempfile, "gettempdir",
                                    return_value=str(self.shared))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fallback = self.shared / f"kilix-qemu-{os.getuid()}"

    def refused(self, vm, reason: str) -> None:
        result = self.manager().launch(vm)
        self.assertFalse(result.ok)
        self.assertIn(reason, result.message)
        self.assertEqual(self.rc.launched, [])

    def test_the_fallback_is_used_only_when_it_is_ours_and_0700(self):
        vm = self.define("Alpha")
        self.assertEqual(model.runtime_dir(), str(self.fallback))
        self.fallback.mkdir()
        self.fallback.chmod(0o755)                    # pre-created, open
        self.refused(vm, "it must be 0700")
        self.fallback.chmod(0o700)
        real = os.lstat

        def foreign_owner(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if os.fspath(path) != str(self.fallback):
                return info
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)

        with mock.patch.object(model.os, "lstat", foreign_owner):
            self.refused(vm, "not you")
        self.fallback.rmdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.fallback.symlink_to(elsewhere, target_is_directory=True)
        self.refused(vm, "symlink")
        self.fallback.unlink()
        result = self.manager().launch(vm)            # created 0700 by us
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.fallback.stat().st_mode & 0o777, 0o700)


    def test_a_runtime_dir_that_cannot_be_created_refuses_the_launch(self):
        os.environ["XDG_RUNTIME_DIR"] = str(self.tmp / "missing" / "run")
        vm = self.define("Alpha")
        self.refused(vm, "cannot create runtime directory")


class StateStoreTests(Sandbox):
    """N1, N6, N8 and the store mutants that survived r1 (R18-R21)."""

    def test_deeply_nested_files_degrade_instead_of_crashing(self):
        path = Path(model.state_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[" * 100000)
        self.assertEqual(store.load_state(), {"favorites": [], "recents": []})
        directory = Path(model.vms_dir())
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "deep.json").write_text("{\"a\":" * 100000)
        self.assertIsNone(store.get("deep"))
        machines, problems = store.load_all()
        self.assertEqual((machines, len(problems)), ([], 1))
        code, _out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)

    def test_a_favorite_that_could_not_be_saved_is_reported(self):
        self.define("Alpha")
        with mock.patch.object(store, "_write_json",
                               side_effect=PermissionError(13, "denied")):
            with self.assertRaises(OSError):
                store.set_favorite("alpha", True)
            code, out, err = self.run_cli("favorite", "Alpha")
            self.assertEqual(code, 1)
            self.assertIn("could not save favorites", err)
            self.assertNotIn("now a favorite", out)
            state = tui.State(self.manager(), refresh_seconds=0)
            tui.handle(ord("f"), state)
            self.assertIn("Could not save favorites", state.error)
            self.assertFalse(state.selected_vm.favorite)
        self.assertEqual(store.favorites(), [])

    def test_concurrent_writers_never_lose_or_tear_an_update(self):
        real_dump = json.dump
        paused, release = threading.Event(), threading.Event()

        def slow_dump(data, handle, **kwargs):
            if threading.current_thread().name == "launcher":
                paused.set()
                release.wait(5)
            return real_dump(data, handle, **kwargs)

        with mock.patch.object(store.json, "dump", slow_dump):
            launcher = threading.Thread(
                target=store.remember_launch, args=("alpha",), name="launcher")
            launcher.start()
            self.assertTrue(paused.wait(5))
            favorite = threading.Thread(target=store.set_favorite,
                                        args=("bravo", True))
            favorite.start()
            favorite.join(0.3)              # the TUI's main thread, meanwhile
            release.set()
            launcher.join(5)
            favorite.join(5)
        record = json.loads(Path(model.state_path()).read_text())
        self.assertEqual(record["favorites"], ["bravo"])
        self.assertEqual([row["id"] for row in record["recents"]], ["alpha"])
        leftovers = [name for name in os.listdir(os.path.dirname(
            model.state_path())) if name not in ("state.json",
                                                 "state.json.lock")]
        self.assertEqual(leftovers, [])

    def test_state_is_written_through_a_swap_file_and_one_replace(self):
        replaced = []
        real = os.replace

        def spy(source, target):
            replaced.append((os.fspath(source), os.fspath(target)))
            real(source, target)

        with mock.patch.object(store.os, "replace", spy):
            store.set_favorite("alpha", True)
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0][1], model.state_path())
        self.assertNotEqual(replaced[0][0], model.state_path())

    def test_remove_forgets_the_favorite_and_the_recent(self):
        vm = self.define("Alpha")
        store.set_favorite(vm.id, True)
        store.remember_launch(vm.id)
        store.remove(vm.id)
        self.assertEqual(store.load_state(), {"favorites": [], "recents": []})

    def test_damage_in_one_field_keeps_the_other_and_caps_recents(self):
        path = Path(model.state_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"favorites": "alpha", "recents": [
            {"id": f"vm{index}", "at": "t"} for index in range(9)]}))
        self.assertEqual([row["id"] for row in store.recents()],
                         [f"vm{index}" for index in range(store.MAX_RECENTS)])


class LivenessGapTests(Sandbox):
    """N9, N10, N13 and the liveness and focus mutants of r1 (R08-R12, R22)."""

    def stale_pidfile(self, vm, pid: int = 4242) -> None:
        Path(vm.pidfile).parent.mkdir(parents=True, exist_ok=True)
        Path(vm.pidfile).write_text(f"{pid}\n")

    def test_a_pidfile_witness_must_be_this_users_qemu(self):
        vm = self.define("Alpha")
        self.stale_pidfile(vm)
        proc = _proc_entry(self.tmp / "proc", 4242, [self.qemu, *vm.argv])
        self.assertEqual(self.manager(proc_root=proc).pid_of(vm), 4242)
        with mock.patch.object(backend.os, "getuid",
                               return_value=os.getuid() + 1):
            self.assertEqual(self.manager(proc_root=proc).pid_of(vm), 0)
        proc = _proc_entry(self.tmp / "proc2", 4242,
                           ["/usr/bin/tail", "-f", vm.pidfile])
        self.assertEqual(self.manager(proc_root=proc).pid_of(vm), 0)

    def test_a_held_launch_claim_reads_starting_and_is_exclusive(self):
        vm = self.define("Alpha")
        manager = self.manager()
        self.assertTrue(manager._claim_launch(vm))
        self.assertFalse(manager._claim_launch(vm))
        self.assertEqual(manager.status(vm).state, "starting")
        result = manager.launch(vm)
        self.assertFalse(result.ok)
        self.assertIn("already being launched", result.message)
        self.assertEqual(self.rc.launched, [])

    def test_an_open_tab_with_silent_qmp_blocks_a_second_launch(self):
        vm = self.define("Alpha")
        pane = self.rc.add_window("QEMU: Alpha", [
            ["/bin/bash", KILIX, "run", *vm.command]])
        manager = self.manager()
        status = manager.status(vm, self.rc.tree())
        self.assertEqual((status.state, status.pane_id), ("starting", pane))
        result = manager.launch(vm)
        self.assertFalse(result.ok)
        self.assertIn(f"pane {pane}", result.message)
        state = tui.State(manager, refresh_seconds=0)
        tui.handle(ENTER, state)
        TuiTests.wait(state)
        self.assertEqual((self.rc.launched, self.rc.focused), ([], [pane]))

    def test_focus_needs_the_whole_path_as_a_qmp_value(self):
        alpha = self.define("Alpha")
        self.rc.add_window("debugging", [["socat", "-", f"unix:{alpha.qmp}"]])
        self.rc.add_window("same name", [[
            "qemu-system-x86_64", "-qmp",
            f"unix:/somewhere/else/{os.path.basename(alpha.qmp)},server=on"]])
        self.assertIsNone(backend.find_pane(self.rc.tree(), alpha.qmp))

    def test_a_lock_in_the_shared_permission_bytes_is_seen(self):
        disk = self.tmp / "d.img"
        disk.write_bytes(b"\0" * 512)
        fd = os.open(disk, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.fcntl(fd, fcntl.F_OFD_SETLK,
                    struct.pack("hhqqi4x", fcntl.F_RDLCK, 0, 250, 1, 0))
        self.assertTrue(backend.disk_locked(str(disk)))

    def test_a_fifo_named_as_a_disk_never_blocks_the_refresh(self):
        fifo = self.tmp / "pipe.img"
        os.mkfifo(fifo)
        vm = self.define("Alpha", "-drive", f"file={fifo},format=raw")
        self.assertEqual(vm.disks, [str(fifo)])
        answers = []
        probe = threading.Thread(
            target=lambda: answers.append(backend.disk_locked(str(fifo))),
            daemon=True)
        probe.start()
        probe.join(3)
        self.assertEqual(answers, [False], "the lock probe blocked on a FIFO")
        rows = self.manager().disk_info(vm)
        self.assertIn("not probed", rows[0]["error"])
        self.assertFalse(Path(self.env["FAKE_QEMU_IMG_LOG"]).exists())


class GateGapTests(Sandbox):
    """The confirmation and desktop-entry mutants of r1 (R14, R15, R26)."""

    def running(self):
        vm = self.define("Alpha")
        self.start_outside(vm, FAKE_QEMU_ACPI="ignore")
        self.wait_for(lambda: self.qmp_up(vm))
        return vm, tui.State(self.manager(), refresh_seconds=0)

    def test_acpi_shutdown_asks_first(self):
        _vm, state = self.running()
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "shutdown")
        tui.handle(ENTER, state)
        self.assertEqual(state.modal, "confirm")
        self.assertNotIn("system_powerdown", self.qmp_log())

    def test_only_y_confirms(self):
        vm, state = self.running()
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "poweroff")
        tui.handle(ENTER, state)
        for key in (ENTER, 13, 343, ord(" "), ord("a"), ord("f")):
            tui.handle(key, state)
        self.assertEqual((state.modal, state.pending), ("confirm", ""))
        self.assertNotIn("quit", self.qmp_log())
        self.assertTrue(self.qmp_up(vm))

    def test_a_symlinked_entry_is_never_replaced_even_with_the_marker(self):
        apps = self.tmp / "apps"
        apps.mkdir()
        theirs = self.tmp / "theirs.desktop"
        body = f"[Desktop Entry]\n{desktop_entry.MARKER}\n"
        theirs.write_text(body)
        (apps / desktop_entry.FILENAME).symlink_to(theirs)
        with self.assertRaises(FileExistsError):
            desktop_entry.install("/x/kilix-qemu-manager", str(apps))
        self.assertTrue((apps / desktop_entry.FILENAME).is_symlink())
        self.assertEqual(theirs.read_text(), body)


# ── fix round r3 ─────────────────────────────────────────────────────────────


def hand_clone(original, ident: str, name: str, share=("pidfile", "qmp")):
    """Copy a definition file under another id, as a user would by hand.

    ``share`` names the control files the copy keeps from the original;
    the others become the copy's own, so the copy still loads on its own.
    """
    data = json.loads(Path(store._definition_path(original.id)).read_text())
    data.update(id=ident, name=name)
    argv = data["argv"]
    if "pidfile" not in share:
        argv[argv.index(original.pidfile)] = data["pidfile"] = (
            model.pid_path(ident))
    if "qmp" not in share:
        data["qmp"] = model.qmp_path(ident)
        argv[argv.index(model.qmp_spec(original.qmp))] = model.qmp_spec(
            data["qmp"])
    model.VmDefinition.from_json(data)              # valid on its own
    path = Path(store._definition_path(ident))
    path.write_text(json.dumps(data))
    return path


class LoaderOwnershipTests(Sandbox):
    """R2-B1: who keeps a pidfile or QMP socket never depends on load order."""

    SHAPES = (("pidfile", "qmp"), ("qmp",), ("pidfile",))
    # "bravo" loads after "alpha"; "aaclone" loads before it.
    CLONES = (("bravo", "Bravo"), ("aaclone", "AA clone"))

    def test_the_vm_whose_id_names_the_files_keeps_them_in_either_order(self):
        alpha = self.define("Alpha")
        for (ident, name), share in itertools.product(self.CLONES,
                                                      self.SHAPES):
            with self.subTest(clone=ident, shares=share):
                path = hand_clone(alpha, ident, name, share)
                try:
                    machines, problems = store.load_all()
                finally:
                    path.unlink()
                self.assertEqual([vm.id for vm in machines], ["alpha"])
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(
                    f"{ident}.json: not loaded: it opens "), problems)
                for kind in share:
                    shared, what = ((alpha.qmp, "QMP socket") if kind == "qmp"
                                   else (alpha.pidfile, "pidfile"))
                    self.assertIn(f"{shared}, the manager's {what} for VM "
                                  "alpha", problems[0])
                self.assertIn("not its own.", problems[0])
                self.assertIn(f"Edit or delete {path}", problems[0])

    def test_power_off_through_the_picker_never_reaches_the_original(self):
        alpha = self.define("Alpha")
        self.start_outside(alpha)
        self.wait_for(lambda: self.qmp_up(alpha))
        for ident, name in self.CLONES:
            with self.subTest(clone=ident):
                path = hand_clone(alpha, ident, name)
                try:
                    self.power_off_everything_but(alpha, ident, name)
                finally:
                    path.unlink()
        # The original itself is still controlled, and named, as itself.
        state = tui.State(self.manager(), refresh_seconds=0)
        state.select_id("alpha")
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "poweroff")
        tui.handle(ENTER, state)
        frame = app.render_to_text(tui.render, state, height=24, width=120)
        self.assertIn("Confirm: Power off — Alpha", frame)
        tui.handle(ord("y"), state)
        TuiTests.wait(state)
        self.assertEqual(state.message, "Powered off Alpha.")
        self.wait_for(lambda: not self.qmp_up(alpha))

    def power_off_everything_but(self, alpha, ident: str, name: str) -> None:
        if not self.qmp_up(alpha):              # each order on its own
            self.start_outside(alpha)
            self.wait_for(lambda: self.qmp_up(alpha))
            Path(self.env["FAKE_QEMU_LOG"]).unlink(missing_ok=True)
        state = tui.State(self.manager(), refresh_seconds=0)
        listed = [status.vm.id for status in state.machines]
        # Every VM the picker offers besides Alpha: Power off, confirmed.
        for other in (vm_id for vm_id in listed if vm_id != "alpha"):
            state.select_id(other)
            tui.handle(ord("a"), state)
            TuiTests.choose(self, state, "poweroff")
            tui.handle(ENTER, state)
            tui.handle(ord("y"), state)
            TuiTests.wait(state)
        time.sleep(0.2)
        self.assertTrue(self.qmp_up(alpha), f"Alpha's QEMU was quit; "
                        f"listed={listed} message={state.message!r}")
        self.assertNotIn("quit", self.qmp_log())
        self.assertEqual(listed, ["alpha"])
        code, out, err = self.run_cli("--status")
        self.assertEqual(code, 0, err)
        self.assertNotIn(name, out)
        self.assertEqual(self.run_cli("--vm", name)[0], 1)
        self.assertIn(f"skipped {ident}.json: not loaded", err)
        self.assertNotIn("alpha.json", err)

    def test_a_shared_path_no_id_derives_refuses_every_claimant(self):
        build = self.tmp / "build"
        build.mkdir()
        first = self.define("First", "-pidfile", str(build / "qemu.pid"))
        self.assertEqual(first.pidfile, str(build / "qemu.pid"))
        for ident, name in (("second", "Second"), ("aaa", "Aaa")):
            with self.subTest(clone=ident):
                path = hand_clone(first, ident, name, share=("pidfile",))
                try:
                    machines, problems = store.load_all()
                finally:
                    path.unlink()
                self.assertEqual(machines, [])
                self.assertEqual(sorted(line.split(":")[0] for line in problems),
                                 sorted([f"{ident}.json", "first.json"]))
                for line in problems:
                    self.assertIn(f"it opens {first.pidfile}, which ", line)
                    self.assertIn("none of them is loaded", line)
        self.assertEqual([vm.id for vm in store.load_all()[0]], ["first"])

    def test_a_socket_two_vms_link_through_is_not_a_control_file(self):
        link = str(self.tmp / "link.sock")
        self.define("Left", "-chardev", f"socket,id=l,path={link},server=on")
        self.define("Right", "-chardev", f"socket,id=r,path={link}")
        machines, problems = store.load_all()
        self.assertEqual(([vm.id for vm in machines], problems),
                         (["left", "right"], []))

    def test_runtime_dir_names_belong_to_their_id_even_when_uncatalogued(self):
        alpha = self.define("Alpha")
        data = alpha.as_json()
        data["argv"] += ["-monitor",
                         f"unix:{model.qmp_path('ghost')},server=on,wait=off"]
        Path(store._definition_path("alpha")).write_text(json.dumps(data))
        machines, problems = store.load_all()
        self.assertEqual(machines, [])
        self.assertIn(f"it opens {model.qmp_path('ghost')}, the manager's QMP "
                      "socket for VM ghost", problems[0])

    def test_a_symlinked_spelling_of_the_runtime_dir_is_the_same_file(self):
        """NB6, in the loader and on import."""
        alpha = self.define("Alpha")
        alias = self.tmp / "alias"
        alias.symlink_to(os.environ["XDG_RUNTIME_DIR"],
                         target_is_directory=True)
        aliased = str(alias / "kilix-qemu" / "alpha.qmp")
        # Import: removed, as the plain spelling is.
        normal = model.normalise(
            "bravo", "qemu-system-x86_64",
            ["-qmp", f"unix:{aliased},server=on,wait=off"], cwd=str(self.tmp),
            claimed=store.claimed_paths([alpha]))
        self.assertFalse(any(aliased in word for word in normal.argv))
        self.assertIn("it is VM alpha's socket", "\n".join(normal.notes))
        # The VM's own socket through the alias is recognised, not doubled.
        own = model.normalise(
            "alpha", "qemu-system-x86_64",
            ["-qmp", f"unix:{alias}/kilix-qemu/alpha.qmp,server,nowait"],
            cwd=str(self.tmp))
        self.assertEqual(own.argv.count("-qmp"), 1)
        # Loader: a hand clone that spells Alpha's socket through the alias.
        path = hand_clone(alpha, "bravo", "Bravo", share=())
        data = json.loads(path.read_text())
        data["argv"] += ["-monitor", f"unix:{aliased},server=on,wait=off"]
        path.write_text(json.dumps(data))
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertIn("the manager's QMP socket for VM alpha", problems[0])

    def test_a_pidfile_on_the_vms_own_qmp_path_is_replaced(self):
        """NB7."""
        normal = model.normalise(
            "bravo", "qemu-system-x86_64",
            ["-pidfile", model.qmp_path("bravo")], cwd=str(self.tmp))
        self.assertEqual(normal.pidfile, model.pid_path("bravo"))
        self.assertIn("it is this VM's QMP socket path", "\n".join(normal.notes))
        self.assertEqual(normal.argv.count(model.qmp_path("bravo")), 0)


class TargetBindingR3Tests(ConfirmTargetTests):
    """NB1, NB2: the binding covers the argv, and every action in the menu."""

    def test_y_after_an_argv_edit_refuses(self):
        alpha, bravo, state = self.two_running()
        self.confirm_poweroff_for_alpha(state, alpha)
        alpha.argv[alpha.argv.index("256")] = "512"
        store.save(alpha, replace=True)        # only the argv changed
        tui.handle(ord("y"), state)
        TuiTests.wait(state)
        self.assertIn("definition changed since it was shown", state.error)
        self.assertNotIn("quit", self.qmp_log())
        self.assertTrue(self.qmp_up(alpha))

    def test_an_unconfirmed_action_from_the_menu_rechecks_its_vm(self):
        alpha, _bravo, state = self.two_running()
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "pause")
        alpha.argv[alpha.argv.index("256")] = "512"
        store.save(alpha, replace=True)
        tui.handle(ENTER, state)
        TuiTests.wait(state)
        self.assertIn("Not done: Pause — Alpha's definition changed", state.error)
        self.assertNotIn("stop", self.qmp_log())
        # Launch, on a stopped VM, likewise never runs a stale argv.
        idle = self.define("Idle")
        state.refresh()
        state.select_id(idle.id)
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "launch")
        idle.argv[idle.argv.index("256")] = "512"
        store.save(idle, replace=True)
        tui.handle(ENTER, state)
        TuiTests.wait(state)
        self.assertIn("Not done: Launch in a Kilix tab — Idle's definition "
                      "changed", state.error)
        self.assertEqual(self.rc.launched, [])


class FocusR3Tests(Sandbox):
    """NB4: --qmp is QEMU's other spelling of -qmp, for import and focus."""

    def test_a_double_dash_qmp_is_kept_and_its_tab_is_found(self):
        vm = self.define("Alpha", "--qmp",
                         f"unix:{model.qmp_path('alpha')},server,nowait")
        self.assertIn("--qmp", vm.argv)
        self.assertNotIn("-qmp", vm.argv)
        pane = self.rc.add_window("QEMU: Alpha", [
            ["/bin/bash", KILIX, "run", *vm.command]])
        self.assertEqual(backend.find_pane(self.rc.tree(), vm.qmp).id, pane)


class StatusRuntimeDirTests(RuntimeDirTests):
    """NB8: status reads trust the runtime directory only as launch does."""

    def plant(self, vm):
        self.fallback.mkdir(mode=0o700, exist_ok=True)
        server = FakeQmpServer(vm.qmp, {"query-status": {
            "return": {"status": "running", "running": True}}})
        self.addCleanup(server.close)
        Path(vm.pidfile).write_text(f"{os.getpid()}\n")
        Path(self.fallback / f"{vm.id}.launch").write_text("1\n")
        return server

    def assert_not_read(self, vm, server, reason: str) -> None:
        manager = self.manager()
        status = manager.status(vm)
        self.assertEqual((status.state, status.qmp_ok, status.pid),
                         ("stopped", False, 0))
        self.assertIn(reason, status.detail)
        result = manager.control(vm, "poweroff")
        self.assertFalse(result.ok)
        self.assertIn(f"Not sent to {vm.name}", result.message)
        self.assertEqual(server.seen, [])

    def test_status_never_reads_a_runtime_dir_that_is_not_safely_ours(self):
        vm = self.define("Alpha")
        server = self.plant(vm)
        self.assertEqual(self.manager().status(vm).state, "running")
        server.seen.clear()
        self.fallback.chmod(0o755)
        self.assert_not_read(vm, server, "it must be 0700")
        self.fallback.chmod(0o700)
        real = os.lstat

        def foreign_owner(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if os.fspath(path) != str(self.fallback):
                return info
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)

        with mock.patch.object(model.os, "lstat", foreign_owner):
            self.assert_not_read(vm, server, "not you")

    def test_a_missing_runtime_dir_reads_as_stopped(self):
        vm = self.define("Alpha")
        self.assertFalse(self.fallback.exists())
        status = self.manager().status(vm)
        self.assertEqual((status.state, status.detail), ("stopped", ""))
        self.assertFalse(self.fallback.exists())     # a read creates nothing


class SwapAndLockR3Tests(Sandbox):
    """NB12 (stale swap files) and N8's cross-process residual."""

    def test_stale_swap_files_of_ours_are_cleaned_and_nothing_else(self):
        state_dir = Path(model.state_path()).parent
        state_dir.mkdir(parents=True)
        vms = Path(model.vms_dir())
        vms.mkdir(parents=True)
        old = time.time() - store.STALE_SWAP_SECONDS - 60
        outside = self.tmp / "outside.txt"
        outside.write_text("keep")
        files = {
            "stale": state_dir / ".state.json.abcd1234.tmp",
            "fresh": state_dir / ".state.json.efgh5678.tmp",
            "link": state_dir / ".state.json.ijkl9012.tmp",
            "foreign": state_dir / ".state.json.mnop3456.tmp",
            "other": state_dir / ".notes.tmp",
            "stale-vm": vms / ".alpha.json.qrst7890.tmp",
        }
        for key, path in files.items():
            if key == "link":
                path.symlink_to(outside)
                os.utime(path, (old, old), follow_symlinks=False)
                continue
            path.write_text("{")
            if key != "fresh":
                os.utime(path, (old, old))
        real = os.lstat

        def foreign(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if os.fspath(path) != str(files["foreign"]):
                return info
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)

        with mock.patch.object(store.os, "lstat", foreign):
            store.set_favorite("alpha", True)
        self.define("Alpha")
        self.assertFalse(files["stale"].exists())
        self.assertFalse(files["stale-vm"].exists())
        for key in ("fresh", "link", "foreign", "other"):
            self.assertTrue(os.path.lexists(files[key]), key)
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual(store.favorites(), ["alpha"])

    def test_a_second_process_never_loses_an_update(self):
        child = subprocess.Popen(
            [sys.executable, "-c", (
                "import sys; sys.path.insert(0, sys.argv[1])\n"
                "from kilix_qemu_manager import store\n"
                "with store.state_lock():\n"
                "    record = store.load_state()\n"
                "    print('held', flush=True)\n"
                "    sys.stdin.readline()\n"
                "    record['recents'] = [{'id': 'alpha', 'at': 't'}]\n"
                "    store.save_state(record)\n"), str(MANAGER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 5)
        self.assertEqual(child.stdout.readline().strip(), "held")
        writer = threading.Thread(target=store.set_favorite,
                                  args=("bravo", True))
        writer.start()
        writer.join(0.4)
        self.assertTrue(writer.is_alive(), "wrote while another process "
                                           "held the state")
        child.stdin.write("\n")
        child.stdin.close()
        self.assertEqual(child.wait(5), 0)
        writer.join(5)
        self.assertEqual(store.load_state(), {
            "favorites": ["bravo"], "recents": [{"id": "alpha", "at": "t"}]})


if __name__ == "__main__":
    unittest.main()
