"""QEMU manager tests.

No real VM runs here. A fake ``qemu-system-x86_64`` serves real QMP over a
unix socket, writes its pidfile and locks its disk the way QEMU does; a fake
``kitty_rc`` records tabs instead of opening them; a fake ``qemu-img``
answers ``info``. Every path points into a temporary directory, and the live
Kilix credentials are removed from the environment before anything imports.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import grp
import io
import itertools
import json
import os
import pwd
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import typing
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
        # 0700, as the manager makes it, whatever the umask.
        Path(vm.pidfile).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        self.shared.chmod(0o1777)                   # as /tmp is, any umask
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

    def test_the_recorded_paths_count_even_where_the_argv_hides_them(self):
        alpha = self.define("Alpha")
        path = hand_clone(alpha, "bravo", "Bravo", share=())
        data = json.loads(path.read_text())
        data["pidfile"] = alpha.pidfile           # what status would read
        data["argv"] += ["-name", alpha.pidfile]  # only to pass from_json
        model.VmDefinition.from_json(data)
        path.write_text(json.dumps(data))
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertIn(f"{alpha.pidfile}, the manager's pidfile for VM alpha",
                      problems[0])

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


def pretend_foreign(directory: Path):
    """Make lstat and fstat report `directory` as another user's."""
    real_lstat, real_fstat = os.lstat, os.fstat

    def bump(info):
        fields = list(info)
        fields[4] = info.st_uid + 1
        return os.stat_result(fields)

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return bump(info) if os.fspath(path) == str(directory) else info

    def fstat(fd):
        info = real_fstat(fd)
        try:
            target = real_lstat(directory)
        except OSError:
            return info
        same = (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino)
        return bump(info) if same else info

    return mock.patch.multiple(model.os, lstat=lstat, fstat=fstat)


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
        with pretend_foreign(self.fallback):
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


# ── fix round r4 ─────────────────────────────────────────────────────────────


class RuntimeSymlinkR4Tests(RuntimeDirTests):
    """R3-B1: what decides is the directory that holds the name as written.

    Nothing named in an unsafe runtime directory is read or sent through,
    symlink or not; and the manager, which makes no symlinks there, never
    follows one it finds, even in a safe directory."""

    def setUp(self) -> None:
        super().setUp()
        self.other_dir = self.tmp / "other"
        self.other_dir.mkdir(mode=0o700)

    def start_other(self, *extra: str):
        """A live QEMU the catalog does not know (started by hand)."""
        qmp_path = str(self.other_dir / "vm.qmp")
        process = subprocess.Popen(
            [self.qemu, "-m", "256", "-nodefaults", *extra,
             "-pidfile", str(self.other_dir / "vm.pid"),
             "-qmp", model.qmp_spec(qmp_path)],
            env=dict(os.environ), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.started.append(process)
        answers = lambda: self.answers(qmp_path)        # noqa: E731
        self.wait_for(lambda: answers() and (self.other_dir / "vm.pid")
                      .exists())
        return process, qmp_path

    @staticmethod
    def answers(path: str) -> bool:
        try:
            qmp.status(path, timeout=0.3)
            return True
        except qmp.QmpError:
            return False

    def plant_links(self, vm, other_qmp: str) -> None:
        """alpha.qmp, alpha.pid and alpha.launch, all symlinks out."""
        self.fallback.mkdir(mode=0o700, exist_ok=True)
        marker = self.other_dir / "fresh-marker"
        marker.write_text("1\n")
        for name, target in ((vm.qmp, other_qmp),
                             (vm.pidfile, str(self.other_dir / "vm.pid")),
                             (str(self.fallback / f"{vm.id}.launch"),
                              str(marker))):
            if os.path.lexists(name):
                os.unlink(name)
            os.symlink(target, name)

    def test_status_and_control_never_go_through_a_planted_symlink(self):
        vm = self.define("Alpha")
        # The other QEMU names Alpha's pidfile path, so a reader that
        # followed alpha.pid would accept its pid as Alpha's.
        process, other_qmp = self.start_other("-name", vm.pidfile)
        self.plant_links(vm, other_qmp)
        # The links are live: anything that followed them would see a VM.
        self.assertTrue(self.answers(vm.qmp))
        self.assertEqual(int(Path(vm.pidfile).read_text()), process.pid)
        for mode, reason in ((0o755, "it must be 0700"),
                             (0o700, "is a symlink")):
            with self.subTest(mode=oct(mode)):
                self.fallback.chmod(mode)
                Path(self.env["FAKE_QEMU_LOG"]).unlink(missing_ok=True)
                manager = self.manager()
                status = manager.status(vm)
                self.assertEqual((status.state, status.qmp_ok, status.pid),
                                 ("stopped", False, 0), status.detail)
                self.assertIn(reason, status.detail)
                result = manager.control(vm, "poweroff")
                self.assertFalse(result.ok, result.message)
                self.assertIn("Not sent to Alpha", result.message)
                self.assertEqual(self.qmp_log(), [])
                self.assertIsNone(process.poll())
        self.fallback.chmod(0o700)
        result = self.manager().launch(vm)
        self.assertFalse(result.ok)
        self.assertIn("Not launching Alpha", result.message)
        self.assertIn("is a symlink", result.message)
        self.assertEqual(self.rc.launched, [])
        self.assertTrue(os.path.islink(vm.qmp))     # never removed for you

    def test_power_off_confirmed_in_the_picker_never_reaches_another_qemu(self):
        """The modal is shown for the real Alpha; then alpha.qmp becomes a
        symlink to another live QEMU (and, in one case, the directory loses
        its 0700) before y. Nothing is sent to either QEMU."""
        vm = self.define("Alpha")
        for mode in (0o755, 0o700):
            with self.subTest(mode=oct(mode)):
                alpha = self.start_outside(vm)
                self.wait_for(lambda: self.qmp_up(vm))
                process, other_qmp = self.start_other()
                state = tui.State(self.manager(), refresh_seconds=0)
                state.select_id(vm.id)
                tui.handle(ord("a"), state)
                TuiTests.choose(self, state, "poweroff")
                tui.handle(ENTER, state)
                frame = app.render_to_text(tui.render, state, height=24,
                                           width=120)
                self.assertIn("Confirm: Power off — Alpha", frame)
                os.unlink(vm.qmp)
                os.symlink(other_qmp, vm.qmp)
                self.fallback.chmod(mode)
                Path(self.env["FAKE_QEMU_LOG"]).unlink(missing_ok=True)
                tui.handle(ord("y"), state)
                TuiTests.wait(state)
                time.sleep(0.2)
                self.assertIn("Not sent to Alpha", state.message)
                self.assertNotIn("quit", self.qmp_log())
                self.assertIsNone(process.poll(), state.message)
                self.assertIsNone(alpha.poll(), state.message)
                for started in (process, alpha):
                    started.terminate()
                    started.wait(5)
                for leftover in (vm.qmp, vm.pidfile,
                                 str(self.other_dir / "vm.qmp"),
                                 str(self.other_dir / "vm.pid")):
                    Path(leftover).unlink(missing_ok=True)
                self.fallback.chmod(0o700)

    def test_a_symlink_from_outside_into_the_runtime_dir_is_not_followed(self):
        old = self.tmp / "old-run"
        old.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(old)
        vm = self.define("Alpha")                   # recorded under old/
        os.environ.pop("XDG_RUNTIME_DIR")
        self.fallback.mkdir(mode=0o700)
        inside = str(self.fallback / "planted.qmp")
        server = FakeQmpServer(inside, {"query-status": {
            "return": {"status": "running", "running": True}}})
        self.addCleanup(server.close)
        os.makedirs(os.path.dirname(vm.qmp), mode=0o700)
        os.symlink(inside, vm.qmp)
        status = self.manager().status(vm)
        self.assertEqual((status.state, status.qmp_ok), ("stopped", False))
        self.assertIn("symlink into the runtime directory", status.detail)
        self.assertFalse(self.manager().control(vm, "poweroff").ok)
        self.assertEqual(server.seen, [])


class RuntimeDirectoryFdR4Tests(RuntimeDirTests):
    """NB-R3-6: the runtime directory is checked and used through one fd."""

    RUNNING = {"query-status": {"return": {"status": "running",
                                           "running": True}}}

    def test_a_directory_swapped_in_after_the_check_is_never_used(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        mine = FakeQmpServer(vm.qmp, self.RUNNING)
        self.addCleanup(mine.close)
        with model.reach(vm.qmp) as where:
            # After the check: the checked directory is moved away, and an
            # open one with a planted socket takes its name.
            os.rename(self.fallback, self.shared / "moved")
            self.fallback.mkdir(mode=0o755)
            planted = FakeQmpServer(vm.qmp, {"query-status": {
                "return": {"status": "paused", "running": False}}})
            self.addCleanup(planted.close)
            self.assertEqual(qmp.status(where, timeout=0.5), "running")
        self.assertEqual(planted.seen, [])
        self.assertEqual(mine.seen, ["qmp_capabilities", "query-status"])

    def test_a_symlinked_runtime_dir_is_never_read(self):
        vm = self.define("Alpha")
        real = self.tmp / "real-run"
        real.mkdir(mode=0o700)                      # ours, 0700, but linked
        self.fallback.symlink_to(real, target_is_directory=True)
        server = FakeQmpServer(str(real / "alpha.qmp"), self.RUNNING)
        self.addCleanup(server.close)
        status = self.manager().status(vm)
        self.assertEqual((status.state, status.qmp_ok), ("stopped", False))
        self.assertIn("or is a symlink", status.detail)
        self.assertFalse(self.manager().control(vm, "poweroff").ok)
        self.assertEqual(server.seen, [])


class UnsafeDirR4Tests(RuntimeDirTests):
    """NB-R3-4: the guard is per path; X4 and X10 of QM-R3."""

    def test_a_build_vm_with_its_pidfile_outside_is_guarded_on_its_qmp(self):
        build = self.tmp / "build"
        build.mkdir()
        vm = self.define("Build", "-pidfile", str(build / "qemu.pid"))
        self.assertFalse(model.in_runtime_dir(vm.pidfile))
        self.assertTrue(model.in_runtime_dir(vm.qmp))
        self.fallback.mkdir(mode=0o700)
        server = FakeQmpServer(vm.qmp, {"query-status": {
            "return": {"status": "running", "running": True}}})
        self.addCleanup(server.close)
        self.assertTrue(self.manager().status(vm).qmp_ok)
        server.seen.clear()
        self.fallback.chmod(0o755)
        result = self.manager().control(vm, "poweroff")
        self.assertFalse(result.ok, result.message)
        self.assertIn("Not sent to Build", result.message)
        self.assertFalse(self.manager().status(vm).qmp_ok)
        self.assertEqual(server.seen, [])

    def test_a_live_pidfile_in_an_unsafe_dir_is_not_read(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        process = self.start_outside(vm, FAKE_QEMU_NO_QMP="1")
        self.wait_for(lambda: self.manager().status(vm).pid == process.pid)
        self.assertEqual(self.manager().status(vm).state, "live")
        self.fallback.chmod(0o755)
        status = self.manager().status(vm)
        self.assertEqual((status.state, status.pid), ("stopped", 0))
        self.assertIn("it must be 0700", status.detail)


class DamagedDefinitionR4Tests(Sandbox):
    """R3-B2: one damaged definition never takes down the others."""

    def damaged(self, ident: str, **change) -> Path:
        base = self.define(ident.title(), save=False)
        data = base.as_json()
        data.update(id=ident)
        for key, value in change.items():
            if key in ("pidfile", "qmp"):
                spelled = (data[key] if key == "pidfile"
                           else model.qmp_spec(data[key]))
                new = value if key == "pidfile" else model.qmp_spec(value)
                data["argv"][data["argv"].index(spelled)] = new
            data[key] = value
        path = Path(store._definition_path(ident))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_from_json_refuses_what_os_path_cannot_take(self):
        good = self.define("Alpha", save=False).as_json()
        bad_path = str(self.tmp / "bad\u0000.pid")
        for field, value, words in (
                ("pidfile", bad_path, "pidfile contains a NUL byte"),
                ("qmp", str(self.tmp / "b\u0000.qmp"), "qmp contains a NUL"),
                ("cwd", "/tmp/x\u0000", "cwd contains a NUL byte"),
                ("argv", ["-name", "x\u0000"], r"argv word \d+ contains a NUL"),
                ("argv", ["-name", "\ud800"], r"argv word \d+ is not valid UTF"),
                ("pidfile", str(self.tmp / "\ud800.pid"), "pidfile is not "),
                ("qemu", "/usr/bin/qemu-system-x86_64\u0000", "binary contains"),
                ("name", "Al\ud800pha", "invalid display name"),
                ("notes", "\ud800", "notes must be text")):
            with self.subTest(field=field, value=value):
                data = json.loads(json.dumps(good))
                if field == "argv":
                    data["argv"] = data["argv"] + value
                elif field == "pidfile":
                    data["argv"][data["argv"].index(data["pidfile"])] = value
                    data["pidfile"] = value
                elif field == "qmp":
                    data["argv"][data["argv"].index(
                        model.qmp_spec(data["qmp"]))] = model.qmp_spec(value)
                    data["qmp"] = value
                else:
                    data[field] = value
                with self.assertRaisesRegex(model.DefinitionError, words):
                    model.VmDefinition.from_json(data)

    def test_every_reader_of_the_catalog_survives_a_damaged_file(self):
        self.define("Alpha")
        self.damaged("nul", pidfile=str(self.tmp / "bad\u0000.pid"))
        self.damaged("odd", notes="\ud800")
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertEqual(sorted(line.split(":")[0] for line in problems),
                         ["nul.json", "odd.json"])
        self.assertIn("pidfile contains a NUL byte", "\n".join(problems))
        code, out, err = self.run_cli("--status")
        self.assertEqual(code, 0, err)
        self.assertIn("Alpha", out)
        self.assertIn("skipped nul.json", err)
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertIn("alpha", out)
        code, out, err = self.run_cli("list", "--json")
        self.assertEqual(len(json.loads(out)["problems"]), 2)
        self.assertEqual(self.run_cli("favorite", "Alpha")[0], 0)
        code, _out, err = self.run_cli(
            "import", "Bravo", "--", self.qemu, "-m", "256", "-nodefaults")
        self.assertEqual(code, 0, err)
        lab = Path(self.env["PLEBIAN_OS_SESSION_HOME"]) / "qemu-vms" / "lab"
        lab.mkdir(parents=True)
        (lab / "disk.qcow2").write_bytes(b"")
        code, out, err = self.run_cli("discover", "--no-processes")
        self.assertEqual(code, 0, err)
        self.assertIn("plebian:lab", out)
        code, _out, err = self.run_cli("discover", "--no-processes",
                                       "--accept", "plebian:lab")
        self.assertEqual(code, 0, err)
        state = tui.State(self.manager(), refresh_seconds=0)
        self.assertEqual(state.error, "")
        self.assertEqual(sorted(s.vm.id for s in state.machines),
                         ["alpha", "bravo", "lab"])
        tui.handle(ord("f"), state)
        self.assertEqual(state.error, "")

    def test_an_unexpected_error_in_one_file_is_that_files_problem(self):
        self.define("Alpha")
        self.define("Odd")
        real_from_json = model.VmDefinition.from_json
        real_paths = model.control_paths

        def from_json(data):
            if data.get("id") == "odd":
                raise LookupError("planted")
            return real_from_json(data)

        def control_paths(args, cwd):
            if "odd" in " ".join(args):
                raise TypeError("planted")
            return real_paths(args, cwd)

        for target, name, fake in ((model.VmDefinition, "from_json", from_json),
                                   (model, "control_paths", control_paths)):
            with self.subTest(patched=name), \
                    mock.patch.object(target, name, fake):
                machines, problems = store.load_all()
                self.assertEqual([vm.id for vm in machines], ["alpha"])
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith("odd.json: "))
                self.assertIn("planted", problems[0])


class LoaderOwnershipR4Tests(Sandbox):
    """NB-R3-1..3: QM-R3's surviving loader mutants X1, X2 and X3."""

    def test_the_recorded_qmp_counts_even_where_the_argv_hides_it(self):
        alpha = self.define("Alpha")
        path = hand_clone(alpha, "bravo", "Bravo", share=())
        data = json.loads(path.read_text())
        data["qmp"] = alpha.qmp                        # what status would use
        # A unix: value counts as a socket under any option, so only a
        # positional word hides the spec from the argv (from_json needs it).
        data["argv"].append(model.qmp_spec(alpha.qmp))
        self.assertNotIn(model.canonical(alpha.qmp), {
            model.canonical(path) for path in model.control_paths(
                data["argv"], data["cwd"] or "/")})
        model.VmDefinition.from_json(data)
        path.write_text(json.dumps(data))
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertIn(f"{alpha.qmp}, the manager's QMP socket for VM alpha",
                      problems[0])

    def test_a_qmp_shared_outside_the_runtime_dir_refuses_every_claimant(self):
        old = self.tmp / "old-run"
        old.mkdir(mode=0o700)
        for ident, name in (("second", "Second"), ("aaa", "Aaa")):
            with self.subTest(clone=ident):
                os.environ["XDG_RUNTIME_DIR"] = str(old)
                first = self.define("First")
                path = hand_clone(first, ident, name, share=("qmp",))
                os.environ["XDG_RUNTIME_DIR"] = self.env["XDG_RUNTIME_DIR"]
                self.assertFalse(model.in_runtime_dir(first.qmp))
                try:
                    machines, problems = store.load_all()
                finally:
                    path.unlink()
                    os.unlink(store._definition_path("first"))
                self.assertEqual(machines, [])
                self.assertEqual(sorted(line.split(":")[0] for line in problems),
                                 sorted([f"{ident}.json", "first.json"]))
                for line in problems:
                    self.assertIn(f"it opens {first.qmp}, which ", line)
                    self.assertIn("none of them is loaded", line)

    def test_a_clone_whose_id_is_a_prefix_of_the_original_is_refused(self):
        alpha = self.define("Alpha")
        for share in LoaderOwnershipTests.SHAPES:
            with self.subTest(shares=share):
                path = hand_clone(alpha, "alph", "Alph", share)
                try:
                    machines, problems = store.load_all()
                finally:
                    path.unlink()
                self.assertEqual([vm.id for vm in machines], ["alpha"])
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(
                    "alph.json: not loaded: it opens "), problems)


class StateFileR4Tests(Sandbox):
    """NB-R3-8 (swap cleanup scope) and NB-R3-7 (a bounded lock wait)."""

    def test_swap_cleanup_touches_only_the_managers_own_files(self):
        home = Path(self.env["KILIX_QEMU_HOME"])     # the state dir, too
        vms = Path(model.vms_dir())
        elsewhere = self.tmp / "elsewhere"
        for directory in (home, vms, elsewhere):
            directory.mkdir(parents=True, exist_ok=True)
        old = time.time() - store.STALE_SWAP_SECONDS - 60
        files = {
            "state": home / ".state.json.abcd1234.tmp",
            "foreign-name": home / ".notes.json.abcd1234.tmp",
            "vm": vms / ".alpha.json.efgh5678.tmp",
            "not-an-id": vms / ".Not_An_Id.json.efgh5678.tmp",
            "other-dir": elsewhere / ".state.json.ijkl9012.tmp",
        }
        for path in files.values():
            path.write_text("{")
            os.utime(path, (old, old))
        self.assertEqual(store.clean_stale_swaps(str(elsewhere)), [])
        store.set_favorite("alpha", True)
        self.define("Alpha")
        self.assertFalse(files["state"].exists())
        self.assertFalse(files["vm"].exists())
        for key in ("foreign-name", "not-an-id", "other-dir"):
            self.assertTrue(files[key].exists(), key)

    def test_a_peer_holding_the_lock_never_freezes_an_update(self):
        self.define("Alpha")
        child = subprocess.Popen(
            [sys.executable, "-c", (
                "import sys; sys.path.insert(0, sys.argv[1])\n"
                "from kilix_qemu_manager import store\n"
                "with store.state_lock():\n"
                "    print('held', flush=True)\n"
                "    sys.stdin.readline()\n"), str(MANAGER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "held")
        open_fds = lambda: len(os.listdir("/proc/self/fd"))  # noqa: E731
        before = open_fds()
        with mock.patch.object(store, "LOCK_WAIT_SECONDS", 0.3):
            started = time.monotonic()
            with self.assertRaisesRegex(OSError, "has held") as caught:
                store.set_favorite("alpha", True)
            self.assertIn(f"{model.state_path()}.lock", str(caught.exception))
            self.assertLess(time.monotonic() - started, 3)
            self.assertFalse(store.save_state({"favorites": ["alpha"]}))
            store.remember_launch("alpha")              # degrades, no raise
            code, _out, err = self.run_cli("favorite", "Alpha")
            self.assertEqual(code, 1)
            self.assertIn("could not save favorites", err)
        self.assertEqual(store._flock, {"depth": 0, "fd": -1})
        self.assertLessEqual(open_fds(), before, "a busy lock leaked its fd")
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertEqual(child.wait(5), 0)
        self.assertTrue(store.set_favorite("alpha", True))



# ── fix round r5 ─────────────────────────────────────────────────────────────


RUNNING_REPLY = {"query-status": {"return": {"status": "running",
                                             "running": True}}}


class ClassifyRaceR5Tests(RuntimeDirTests):
    """R4-B1: whether a path is in the runtime directory is decided by its
    name as written, never by resolving it, and the path is then reached
    only through the directory descriptor that was checked. The fallback
    runtime dir here is a symlink, flipped between two open directories of
    "another user" (stood in for by mode 0755), each holding alpha.qmp -> a
    live socket of the user's: the victim."""

    def setUp(self) -> None:
        super().setUp()
        victim_dir = self.tmp / "victim"
        victim_dir.mkdir(mode=0o700)
        self.victim = FakeQmpServer(str(victim_dir / "v.qmp"), RUNNING_REPLY)
        self.addCleanup(self.victim.close)
        self.a, self.b = self.shared / "attacker-a", self.shared / "attacker-b"
        for directory in (self.a, self.b):
            directory.mkdir()
            directory.chmod(0o755)              # whatever the umask
            os.symlink(self.victim.path, directory / "alpha.qmp")

    def plant(self) -> None:
        os.symlink(self.a, self.fallback)

    def flip(self) -> None:
        current = os.readlink(self.fallback)
        swap = self.shared / "flip.tmp"
        os.symlink(self.b if current == str(self.a) else self.a, swap)
        os.replace(swap, self.fallback)

    def flipping_realpath(self):
        """realpath, with the attacker's rename right after each lookup
        of the runtime directory's name."""
        real, fallback = os.path.realpath, str(self.fallback)

        def racing(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if os.fspath(path) == fallback:
                self.flip()
            return result
        return mock.patch.object(model.os.path, "realpath", racing)

    def test_the_place_of_a_name_is_never_resolved(self):
        vm = self.define("Alpha")
        self.plant()
        seen = []
        real = os.path.realpath

        def recording(path, *args, **kwargs):
            seen.append(os.fspath(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(model.os.path, "realpath", recording):
            self.assertTrue(model.in_runtime_dir(vm.qmp))
            for path in (vm.qmp, vm.pidfile, str(self.fallback / "x.launch")):
                with self.assertRaises(model.RuntimeDirError):
                    with model.reach(path):
                        pass
            self.manager().status(vm)
            self.manager().control(vm, "pause")
        self.assertEqual(seen, [])
        self.assertEqual(self.victim.seen, [])

    def test_a_flip_between_lookups_never_reaches_the_victim(self):
        vm = self.define("Alpha")
        self.plant()
        with self.flipping_realpath():
            status = self.manager().status(vm)
            result = self.manager().control(vm, "poweroff")
        self.assertEqual((status.state, status.qmp_ok), ("stopped", False))
        self.assertIn("or is a symlink", status.detail)
        self.assertFalse(result.ok, result.message)
        self.assertIn("Not sent to Alpha", result.message)
        self.assertEqual(self.victim.seen, [])

    def test_power_off_in_the_picker_never_reaches_the_victim(self):
        """End to end through the keys: the modal is shown for the real,
        running Alpha; then the attacker takes the fallback's name and
        races it while y is pressed."""
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        alpha = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(alpha.close)
        state = tui.State(self.manager(), refresh_seconds=0)
        state.select_id(vm.id)
        tui.handle(ord("a"), state)
        TuiTests.choose(self, state, "poweroff")
        tui.handle(ENTER, state)
        frame = app.render_to_text(tui.render, state, height=24, width=120)
        self.assertIn("Confirm: Power off — Alpha", frame)
        os.rename(self.fallback, self.shared / "alpha-moved")
        self.plant()
        with self.flipping_realpath():
            tui.handle(ord("y"), state)
            TuiTests.wait(state)
        self.assertIn("Not sent to Alpha", state.message)
        self.assertEqual(self.victim.seen, [])
        self.assertNotIn("quit", alpha.seen)

    ATTACKER = (
        "import os, sys, time\n"
        "mode, fallback, a, real, seconds = sys.argv[1:6]\n"
        "end = time.monotonic() + float(seconds)\n"
        "swap = fallback + '.flip'\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        if mode == 'links':\n"
        "            os.symlink(a, swap); os.replace(swap, fallback)\n"
        "            os.unlink(fallback)\n"
        "        else:                    # a real open dir, then a link\n"
        "            os.rename(real, fallback); os.rename(fallback, real)\n"
        "            os.symlink(a, fallback); os.unlink(fallback)\n"
        "    except OSError:\n"
        "        pass\n")

    def test_a_racing_process_never_reaches_the_victim(self):
        vm = self.define("Alpha")
        real = self.shared / "attacker-real"
        real.mkdir()
        real.chmod(0o755)
        os.symlink(self.victim.path, real / "alpha.qmp")
        manager = self.manager(qmp_timeout=0.3)
        for mode in ("links", "dir"):
            with self.subTest(mode=mode):
                attacker = subprocess.Popen(
                    [sys.executable, "-c", self.ATTACKER, mode,
                     str(self.fallback), str(self.a), str(real), "1.5"])
                self.started.append(attacker)
                rounds = 0
                deadline = time.monotonic() + 1.2
                while time.monotonic() < deadline:
                    rounds += 1
                    manager.status(vm)
                    manager.control(vm, "pause")
                attacker.wait(10)
                self.assertGreater(rounds, 10)
                self.assertEqual(self.victim.seen, [], f"{rounds} rounds")


class RuntimeDirFdR5Tests(RuntimeDirTests):
    """The runtime directory is checked through the descriptor that is
    then used (QM-R4's Y1): never through a second lookup of its name."""

    def test_a_dir_swapped_between_the_open_and_the_check_is_judged_as_opened(
            self):
        vm = self.define("Alpha")
        self.fallback.mkdir()
        self.fallback.chmod(0o755)                      # open: unsafe
        planted = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(planted.close)
        real_open = os.open
        name = self.fallback.name

        def swapping_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if os.fspath(path) == name and kwargs.get("dir_fd") is not None:
                # Right after the open: a safe 0700 dir takes the name.
                os.rename(self.fallback, self.shared / "opened")
                self.fallback.mkdir(mode=0o700)
            return fd

        with mock.patch.object(model.os, "open", swapping_open):
            result = self.manager().control(vm, "poweroff")
        self.assertFalse(result.ok, result.message)
        self.assertIn("it must be 0700", result.message)
        self.assertEqual(planted.seen, [])


class OutsideDirR5Tests(RuntimeDirTests):
    """A path outside the runtime directory is reached through a checked
    walk too (NB-R4-1, NB-R4-3), and only through its descriptor."""

    def test_a_path_under_an_old_fallback_another_user_made_is_not_used(self):
        vm = self.define("Alpha")                       # in the fallback
        self.fallback.mkdir()
        self.fallback.chmod(0o755)
        victim_dir = self.tmp / "victim"
        victim_dir.mkdir(mode=0o700)
        victim = FakeQmpServer(str(victim_dir / "v.qmp"), RUNNING_REPLY)
        self.addCleanup(victim.close)
        os.symlink(victim.path, vm.qmp)
        now = self.tmp / "run-now"
        now.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(now)        # a session that has one
        self.assertFalse(model.in_runtime_dir(vm.qmp))
        with pretend_foreign(self.fallback):
            status = self.manager().status(vm)
            result = self.manager().control(vm, "poweroff")
        self.assertEqual((status.state, status.qmp_ok), ("stopped", False))
        self.assertIn("which belongs to uid", status.detail)
        self.assertFalse(result.ok)
        self.assertEqual(victim.seen, [])
        # Ours again, it is followed as the user's own symlink.
        self.assertEqual(self.manager().status(vm).state, "running")

    def test_a_writable_directory_on_the_way_is_not_used(self):
        another = {"members": ("qemu-mgr-other",)}
        for mode, used, db in ((0o777, False, {}), (0o775, False, another),
                               (0o775, True, {}), (0o1777, True, {}),
                               (0o755, True, {})):
            with self.subTest(mode=oct(mode), **db), group_db(**db):
                open_dir = Path(tempfile.mkdtemp(dir=self.tmp))
                open_dir.chmod(mode)
                mine = open_dir / "mine"
                mine.mkdir(mode=0o700)
                server = FakeQmpServer(str(mine / "x.qmp"), RUNNING_REPLY)
                self.addCleanup(server.close)
                problem = model.reach_problem(server.path)
                if used:
                    self.assertEqual(problem, "")
                    with model.reach(server.path) as where:
                        self.assertEqual(qmp.status(where, timeout=0.5),
                                         "running")
                else:
                    self.assertIn(f"which is mode {mode:04o}", problem)
                    with self.assertRaises(model.RuntimeDirError):
                        with model.reach(server.path):
                            pass
                    self.assertEqual(server.seen, [])

    def test_a_name_another_user_made_in_a_sticky_dir_is_not_used(self):
        sticky = self.tmp / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)
        theirs = sticky / "theirs"
        theirs.mkdir(mode=0o700)
        server = FakeQmpServer(str(theirs / "x.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        real_stat = os.stat

        def foreign_entry(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if os.fspath(path) == "theirs" and kwargs.get("dir_fd") is not None:
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        with mock.patch.object(model.os, "stat", foreign_entry):
            self.assertIn("was made by uid", model.reach_problem(server.path))
        self.assertEqual(model.reach_problem(server.path), "")

    def test_an_outside_path_is_used_through_its_checked_directory(self):
        mine_dir = self.tmp / "mine"
        mine_dir.mkdir(mode=0o700)
        path = str(mine_dir / "x.qmp")
        mine = FakeQmpServer(path, RUNNING_REPLY)
        self.addCleanup(mine.close)
        with model.reach(path) as where:
            os.rename(mine_dir, self.tmp / "moved")
            mine_dir.mkdir(mode=0o700)
            planted = FakeQmpServer(path, {"query-status": {
                "return": {"status": "paused", "running": False}}})
            self.addCleanup(planted.close)
            self.assertEqual(qmp.status(where, timeout=0.5), "running")
        self.assertEqual(planted.seen, [])

    def test_a_path_below_a_subdir_of_the_runtime_dir_is_not_used(self):
        """NB-R4-3: <run>/sub/alpha.qmp, hand-made, in a safe runtime dir
        and in an open one; sub is a real dir or a symlink out."""
        vm = self.define("Alpha")
        victim_dir = self.tmp / "victim"
        victim_dir.mkdir(mode=0o700)
        victim = FakeQmpServer(str(victim_dir / "alpha.qmp"), RUNNING_REPLY)
        self.addCleanup(victim.close)
        self.fallback.mkdir(mode=0o700)
        real_sub = self.fallback / "real"
        real_sub.mkdir(mode=0o700)
        inner = FakeQmpServer(str(real_sub / "alpha.qmp"), RUNNING_REPLY)
        self.addCleanup(inner.close)
        os.symlink(victim_dir, self.fallback / "sub")
        for sub in ("sub", "real"):
            for mode in (0o700, 0o755):
                with self.subTest(sub=sub, mode=oct(mode)):
                    self.fallback.chmod(mode)
                    data = vm.as_json()
                    hand = str(self.fallback / sub / "alpha.qmp")
                    data["argv"] = [model.qmp_spec(hand)
                                    if word == model.qmp_spec(vm.qmp)
                                    else word for word in data["argv"]]
                    data["qmp"] = hand
                    hand_vm = model.VmDefinition.from_json(data)
                    status = self.manager().status(hand_vm)
                    result = self.manager().control(hand_vm, "poweroff")
                    self.assertEqual(status.state, "stopped")
                    self.assertIn("below the runtime directory", status.detail)
                    self.assertFalse(result.ok)
        self.assertEqual((victim.seen, inner.seen), ([], []))

    def test_a_users_own_symlink_outside_is_still_followed(self):
        mine = self.tmp / "mine"
        mine.mkdir(mode=0o700)
        server = FakeQmpServer(str(mine / "real.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        link = self.tmp / "link.qmp"
        os.symlink("mine/real.qmp", link)               # relative, in ours
        with model.reach(str(link)) as where:
            self.assertEqual(qmp.status(where, timeout=0.5), "running")
        chain = self.tmp / "chain.qmp"
        os.symlink(link, chain)
        with model.reach(str(chain)) as where:
            self.assertEqual(qmp.status(where, timeout=0.5), "running")

    def test_without_proc_nothing_is_reached_by_name(self):
        """NB-R4-6: with no /proc/self/fd, refuse; never fall back."""
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        server = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(server.close)
        real_isdir = os.path.isdir

        def no_proc(path):
            return (False if os.fspath(path).startswith("/proc/self/fd")
                    else real_isdir(path))

        with mock.patch.object(model.os.path, "isdir", no_proc):
            status = self.manager().status(vm)
            result = self.manager().control(vm, "poweroff")
            self.assertEqual(model.reach_problem(vm.qmp), "")
        self.assertEqual(status.state, "stopped")
        self.assertIn("/proc/self/fd is not available", status.detail)
        self.assertFalse(result.ok)
        self.assertEqual(server.seen, [])


class WalkR5Tests(RuntimeDirTests):
    """The walk itself: links, loops, other spellings and what is above."""

    def test_a_symlink_loop_is_refused_not_followed_forever(self):
        loop = self.tmp / "loop"
        os.symlink("loop2", loop)
        os.symlink("loop", self.tmp / "loop2")
        self.assertIn("too many symlinks",
                      model.reach_problem(str(loop / "x.qmp")))
        self.assertIn("too many symlinks", model.reach_problem(str(loop)))

    def test_a_name_swapped_for_a_link_after_its_check_is_not_followed(self):
        """Between the lstat that saw a directory and the open, the
        runtime dir's name becomes a symlink to a safe 0700 dir of ours
        with a live socket: the open must not follow it."""
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        linked = self.tmp / "linked"
        linked.mkdir(mode=0o700)
        server = FakeQmpServer(str(linked / "alpha.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        real_stat, name = os.stat, self.fallback.name

        def swapping_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if os.fspath(path) == name and kwargs.get("dir_fd") is not None \
                    and self.fallback.is_dir() and not self.fallback.is_symlink():
                self.fallback.rmdir()
                self.fallback.symlink_to(linked, target_is_directory=True)
            return info

        with mock.patch.object(model.os, "stat", swapping_stat):
            result = self.manager().control(vm, "poweroff")
        self.assertFalse(result.ok, result.message)
        self.assertIn("or is a symlink", result.message)
        self.assertEqual(server.seen, [])

    def alias(self) -> Path:
        """The runtime dir, spelled through a symlink to its parent."""
        alias = self.tmp / "alias"
        if not alias.is_symlink():
            alias.symlink_to(self.shared, target_is_directory=True)
        return alias / self.fallback.name

    def test_another_spelling_of_the_runtime_dir_gets_its_rules(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        server = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(server.close)
        spelled = str(self.alias() / "alpha.qmp")
        self.assertFalse(model.in_runtime_dir(spelled))
        with model.reach(spelled) as where:
            self.assertEqual(qmp.status(where, timeout=0.5), "running")
        self.fallback.chmod(0o755)
        self.assertIn("it must be 0700", model.reach_problem(spelled))
        self.fallback.chmod(0o700)
        victim_dir = self.tmp / "victim"
        victim_dir.mkdir(mode=0o700)
        victim = FakeQmpServer(str(victim_dir / "v.qmp"), RUNNING_REPLY)
        self.addCleanup(victim.close)
        os.symlink(victim.path, self.fallback / "linked.qmp")
        problem = model.reach_problem(str(self.alias() / "linked.qmp"))
        self.assertIn("is a symlink in the runtime directory", problem)
        self.assertEqual(victim.seen, [])

    def test_an_open_directory_above_the_runtime_dir_refuses_it(self):
        above = self.tmp / "above"
        above.mkdir()
        above.chmod(0o777)
        run = above / "run"
        run.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(run)
        vm = self.define("Alpha")
        os.mkdir(model.runtime_dir(), 0o700)
        server = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(server.close)
        status = self.manager().status(vm)
        self.assertEqual(status.state, "stopped")
        self.assertIn("which is mode 0777", status.detail)
        self.assertFalse(self.manager().control(vm, "poweroff").ok)
        self.assertEqual(server.seen, [])
        above.chmod(0o755)
        self.assertEqual(self.manager().status(vm).state, "running")


class LoaderAliasR5Tests(Sandbox):
    """The loader still compares the runtime dir canonically, so a hand
    clone that opens Alpha's socket by its real path is refused even when
    XDG_RUNTIME_DIR itself is spelled through a symlink."""

    def test_a_symlinked_xdg_runtime_dir_is_still_the_runtime_dir(self):
        real = os.environ["XDG_RUNTIME_DIR"]
        alias = self.tmp / "run-alias"
        alias.symlink_to(real, target_is_directory=True)
        os.environ["XDG_RUNTIME_DIR"] = str(alias)
        alpha = self.define("Alpha")
        self.assertTrue(alpha.qmp.startswith(str(alias)))
        path = hand_clone(alpha, "bravo", "Bravo", share=())
        data = json.loads(path.read_text())
        real_qmp = os.path.join(real, "kilix-qemu", "alpha.qmp")
        data["argv"] += ["-monitor", f"unix:{real_qmp},server=on,wait=off"]
        path.write_text(json.dumps(data))
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertIn("the manager's QMP socket for VM alpha", problems[0])


class LaunchLinksR5Tests(RuntimeDirTests):
    """NB-R4-2 and QM-R4's Y2: each runtime file a launch would use is
    checked for a symlink on its own, and a symlink is never removed."""

    def test_a_symlinked_launch_marker_is_kept_and_refuses_the_launch(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        keep = self.tmp / "users-file.txt"
        keep.write_text("mine\n")
        marker = self.fallback / "alpha.launch"
        os.symlink(keep, marker)
        result = self.manager(launch_wait_seconds=0.3).launch(vm)
        self.assertFalse(result.ok, result.message)
        self.assertIn("alpha.launch is a symlink", result.message)
        self.assertEqual(self.rc.launched, [])
        self.assertTrue(os.path.islink(marker))
        self.assertEqual(keep.read_text(), "mine\n")

    def test_a_launch_claim_never_removes_a_symlinked_marker(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        keep = self.tmp / "old-marker"
        keep.write_text("1\n")
        old = time.time() - 3600                    # stale, were it a file
        os.utime(keep, (old, old))
        marker = self.fallback / "alpha.launch"
        os.symlink(keep, marker)
        self.assertFalse(self.manager()._claim_launch(vm))
        self.assertTrue(os.path.islink(marker))

    def test_a_symlinked_pidfile_alone_refuses_the_launch(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        elsewhere = self.tmp / "elsewhere.pid"
        os.symlink(elsewhere, vm.pidfile)               # dangling: QEMU
        result = self.manager(launch_wait_seconds=0.3).launch(vm)  # would
        self.assertFalse(result.ok, result.message)     # write through it
        self.assertIn("alpha.pid is a symlink", result.message)
        self.assertEqual(self.rc.launched, [])
        self.assertTrue(os.path.islink(vm.pidfile))
        self.assertFalse(elsewhere.exists())


class StoreR5Tests(Sandbox):
    def test_an_interrupt_during_the_lock_wait_leaks_no_fd(self):
        """NB-R4-5."""
        child = subprocess.Popen(
            [sys.executable, "-c", (
                "import sys; sys.path.insert(0, sys.argv[1])\n"
                "from kilix_qemu_manager import store\n"
                "with store.state_lock():\n"
                "    print('held', flush=True)\n"
                "    sys.stdin.readline()\n"), str(MANAGER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "held")
        open_fds = lambda: len(os.listdir("/proc/self/fd"))  # noqa: E731
        before = open_fds()

        def interrupted(_seconds):
            raise KeyboardInterrupt

        with mock.patch.object(store.time, "sleep", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                store.set_favorite("alpha", True)
        self.assertEqual(store._flock, {"depth": 0, "fd": -1})
        self.assertLessEqual(open_fds(), before, "the lock fd leaked")
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertEqual(child.wait(5), 0)

    def test_a_fifo_in_the_catalog_never_hangs_a_reader(self):
        """NB-R4-7: in a child with a timeout, since a hang is the defect."""
        self.define("Alpha")
        os.mkfifo(os.path.join(model.vms_dir(), "fifo.json"))
        os.makedirs(os.path.dirname(model.state_path()), exist_ok=True)
        os.mkfifo(model.state_path())
        code = (
            "import json, sys; sys.path.insert(0, sys.argv[1])\n"
            "from kilix_qemu_manager import store\n"
            "machines, problems = store.load_all()\n"
            "print(json.dumps([[m.id for m in machines], problems,\n"
            "                  store.load_state(), store.get('fifo')]))\n")
        try:
            done = subprocess.run([sys.executable, "-c", code, str(MANAGER)],
                                  capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("a FIFO in the catalog hung load_all")
        self.assertEqual(done.returncode, 0, done.stderr)
        ids, problems, record, got = json.loads(done.stdout)
        self.assertEqual(ids, ["alpha"])
        self.assertEqual(problems, ["fifo.json: fifo.json is not a regular file"])
        self.assertEqual(record, {"favorites": [], "recents": []})
        self.assertIsNone(got)


# ── fix round r6 ─────────────────────────────────────────────────────────────


class Account(typing.NamedTuple):
    pw_name: str
    pw_gid: int
    pw_uid: int = -1


class Group(typing.NamedTuple):
    gr_name: str
    gr_gid: int
    gr_mem: list


@contextlib.contextmanager
def group_db(*, name: str | None = None, members=(), other_primary=False,
             missing=False, second_line=None, sources="files", listing=True,
             listed=None):
    """The account database as the manager sees it, for this process's
    group: named `name` (default: the user's own name, as a private group
    is), with `members`, and with `other_primary`, another account whose
    primary group it is; with `missing`, the group has no entry at all;
    with `second_line`, a (name, members) group line that shares its gid;
    `sources` is what nsswitch.conf names for passwd and group; `listed`
    is the (name, members) of the group's own line as the group listing
    shows it, when that differs from getgrgid's answer. With
    `listing` False the account and group lists come from the C library
    (model._libc), for tests that fake that instead. Other groups are looked
    up for real. (create=True and the pwd.getpwall stand-in let the same
    helper run against the r6 code, for the before-runs.)"""
    me = pwd.getpwuid(os.getuid())
    gid = os.getgid()
    real_getgrgid = grp.getgrgid
    real_getpwuid = pwd.getpwuid
    entry = (name or me.pw_name, "x", gid, list(members))

    def getgrgid(number):
        if number == gid and missing:
            raise KeyError(f"getgrgid(): gid not found: {number}")
        if number == gid:
            return grp.struct_group(entry)
        return real_getgrgid(number)

    def getpwuid(number):
        if number == os.getuid():
            return pwd.struct_passwd((me.pw_name, "x", me.pw_uid, gid, "",
                                      me.pw_dir, "/bin/sh"))
        return real_getpwuid(number)

    def accounts():
        entries = [Account(me.pw_name, gid, me.pw_uid)]
        if other_primary:
            entries.append(Account("qemu-mgr-other", gid))
        return entries

    def groups():
        own = listed or (entry[0], entry[3])
        entries = [] if missing else [Group(own[0], gid, list(own[1]))]
        if second_line:
            entries.append(Group(second_line[0], gid, list(second_line[1])))
        return entries

    with tempfile.TemporaryDirectory(prefix="qemu-mgr-nss-") as nss:
        conf = os.path.join(nss, "nsswitch.conf")
        with open(conf, "w") as handle:
            handle.write(f"passwd: {sources}\ngroup: {sources}\n")
        with contextlib.ExitStack() as stack:
            for target, attribute, value in (
                    (grp, "getgrgid", getgrgid), (pwd, "getpwuid", getpwuid),
                    (pwd, "getpwall", lambda: [
                        pwd.struct_passwd((entry.pw_name, "x", 0,
                                           entry.pw_gid, "", "/", "/bin/sh"))
                        for entry in accounts()]),
                    (model, "NSSWITCH", conf)):
                stack.enter_context(mock.patch.object(target, attribute,
                                                      value, create=True))
            if listing:
                stack.enter_context(mock.patch.object(
                    model, "_accounts", accounts, create=True))
                stack.enter_context(mock.patch.object(
                    model, "_groups", groups, create=True))
            yield


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


class PrivateGroupR6Tests(RuntimeDirTests):
    """R5-B1: group write by the user's own private group (Debian's
    pam_umask usergroups: umask 002, 0775 directories) is the user's write;
    group write by any real group is still refused."""

    def vm_dir(self, mode: int) -> Path:
        vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
        vmdir.chmod(mode)
        return vmdir

    def test_a_pidfile_in_a_0775_dir_of_the_users_private_group_is_used(self):
        vmdir = self.vm_dir(0o775)
        pidfile = str(vmdir / "qemu.pid")
        with group_db():
            vm = self.define("Alpha", "-pidfile", pidfile)
            self.assertEqual(vm.pidfile, pidfile)       # kept: the user's
            self.assertEqual(model.reach_problem(pidfile), "")
            result = self.manager().launch(vm)
            self.assertTrue(result.ok, result.message)
            self.wait_for(lambda: self.manager().status(vm).pid)
            sub = vmdir / "sub"                         # 0775 below 0775
            sub.mkdir()
            sub.chmod(0o775)
            server = FakeQmpServer(str(sub / "own.qmp"), RUNNING_REPLY)
            self.addCleanup(server.close)
            with model.reach(server.path) as where:
                self.assertEqual(qmp.status(where, timeout=0.5), "running")
        self.assertEqual(server.seen, ["qmp_capabilities", "query-status"])

    def test_a_0775_dir_whose_group_is_not_the_users_alone_is_refused(self):
        cases = (
            ("another member", {"members": ("qemu-mgr-other",)}, False),
            ("another name", {"name": "staff"}, False),
            ("another account's primary group", {"other_primary": True},
             False),
            ("not this process's group", {}, True),
            ("no group entry", {"missing": True}, False),
        )
        for index, (label, db, other_gid) in enumerate(cases):
            with self.subTest(label), group_db(**db):
                vmdir = self.vm_dir(0o775)
                vm = self.define(f"Alpha {index}", "-pidfile",
                                 str(vmdir / "qemu.pid"))
                server = FakeQmpServer(str(vmdir / "own.qmp"), RUNNING_REPLY)
                self.addCleanup(server.close)
                with contextlib.ExitStack() as stack:
                    if other_gid:
                        stack.enter_context(mock.patch.object(
                            model.os, "getgid", lambda: os.getegid() + 1))
                    problem = model.reach_problem(server.path)
                    result = self.manager().launch(vm)
                    status = self.manager().status(vm)
                self.assertIn("which is mode 0775 and writable by group",
                              problem)
                self.assertIn("which is not your private group", problem)
                self.assertFalse(result.ok)
                self.assertIn("which is mode 0775", result.message)
                self.assertFalse(status.pid)
                self.assertEqual(self.rc.launched, [])
                self.assertEqual(server.seen, [])

    def test_others_write_is_refused_even_with_a_private_group(self):
        with group_db():
            for mode in (0o777, 0o767, 0o757):
                with self.subTest(mode=oct(mode)):
                    vmdir = self.vm_dir(mode)
                    problem = model.reach_problem(str(vmdir / "x.qmp"))
                    self.assertIn(f"which is mode {mode:04o}", problem)
                    self.assertNotIn("writable by group", problem)

    def test_setgid_is_not_the_sticky_bit(self):
        """QM-R5's Z3: a 2777 or 2775 (shared project) dir is not trusted."""
        another = {"members": ("qemu-mgr-other",)}
        for mode, used, db in ((0o2777, False, {}), (0o2775, False, another),
                               (0o2775, True, {}), (0o3777, True, {})):
            with self.subTest(mode=oct(mode), **db), group_db(**db):
                vmdir = self.vm_dir(0o700)
                server = FakeQmpServer(str(vmdir / "x.qmp"), RUNNING_REPLY)
                self.addCleanup(server.close)
                vmdir.chmod(mode)
                problem = model.reach_problem(server.path)
                if used:
                    self.assertEqual(problem, "")
                else:
                    self.assertIn(f"which is mode {mode:04o}", problem)

    def test_a_sticky_dir_another_user_owns_is_not_trusted(self):
        """QM-R5's Z6: its owner may rename any entry in it."""
        sticky = self.vm_dir(0o1777)
        mine = sticky / "mine"
        mine.mkdir(mode=0o700)
        server = FakeQmpServer(str(mine / "x.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        with pretend_foreign(sticky):
            self.assertIn(f"{sticky}, which belongs to uid",
                          model.reach_problem(server.path))
            with self.assertRaises(model.RuntimeDirError):
                with model.reach(server.path):
                    pass
        self.assertEqual(server.seen, [])
        self.assertEqual(model.reach_problem(server.path), "")

    def test_the_sandbox_tmp_is_sticky_like_tmp(self):
        self.assertEqual(stat_mode(self.shared), 0o1777)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o7777


class DescriptorR6Tests(RuntimeDirTests):
    """NB-R5-1 (QM-R5's Z9): no descriptor outlives a status or control."""

    def test_status_and_controls_leak_no_descriptor(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        server = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(server.close)
        mine = self.tmp / "mine"
        mine.mkdir(mode=0o700)
        outside = FakeQmpServer(str(mine / "x.qmp"), RUNNING_REPLY)
        self.addCleanup(outside.close)
        open_dir = self.tmp / "open"
        open_dir.mkdir()
        open_dir.chmod(0o777)
        manager = self.manager()
        self.assertEqual(manager.status(vm).state, "running")   # warm up
        before = open_fds()
        for _ in range(10):
            self.assertEqual(manager.status(vm).state, "running")
            self.assertTrue(manager.control(vm, "pause").ok)
            self.assertEqual(model.reach_problem(vm.pidfile), "")
            with model.reach(outside.path) as where:
                self.assertEqual(qmp.status(where, timeout=0.5), "running")
            self.assertIn("which is mode 0777",
                          model.reach_problem(str(open_dir / "x.qmp")))
            with model.reach(str(self.tmp / "missing" / "x.qmp")) as where:
                self.assertIsNone(where)
        # The fake servers close their side of each connection on their
        # own threads, so wait for the count to settle. A leak is one or
        # more per path per round: dozens here.
        deadline = time.monotonic() + 5
        while open_fds() > before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertLessEqual(open_fds(), before)


class DotPathR6Tests(Sandbox):
    """NB-R5-3: a recorded absolute path may hold '..' (import keeps one
    as written); the walk goes up as the kernel does (QM-R5's Z2). A path
    that names a directory is refused, never an IndexError (Z7)."""

    def test_dotdot_goes_up_as_the_kernel_does(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir(mode=0o700)
        (a / "sub").mkdir(mode=0o700)
        (a / "b").mkdir(mode=0o700)
        b.mkdir(mode=0o700)
        server = FakeQmpServer(str(b / "x.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        decoy = FakeQmpServer(str(a / "b" / "x.qmp"), {"query-status": {
            "return": {"status": "paused", "running": False}}})
        self.addCleanup(decoy.close)
        os.symlink("a/sub", self.tmp / "link")
        for path in (f"{a}/../b/x.qmp", f"{a}/sub/../../b/x.qmp",
                     f"{self.tmp}/link/../../b/x.qmp"):
            with self.subTest(path=path.replace(str(self.tmp), "<t>")):
                self.assertEqual(qmp.status(path, timeout=0.5), "running")
                with model.reach(path) as where:
                    self.assertEqual(qmp.status(where, timeout=0.5),
                                     "running")
        self.assertEqual(decoy.seen, [])
        pidfile = b / "qemu.pid"
        pidfile.write_text("4242\n")
        vm = self.define("Alpha", "-pidfile", f"{a}/../b/qemu.pid")
        self.assertEqual(vm.pidfile, f"{a}/../b/qemu.pid")  # as written
        with model.reach(vm.pidfile) as where:
            self.assertEqual(Path(where).read_text(), "4242\n")

    def test_a_path_that_names_a_directory_is_refused(self):
        (self.tmp / "b").mkdir(mode=0o700)
        for tail in ("/b/.", "/b/./.", "/.", "/b/..", "/b/./.."):
            path = str(self.tmp) + tail
            with self.subTest(tail=tail):
                with self.assertRaises(model.RuntimeDirError) as caught:
                    with model.reach(path):
                        pass
                self.assertIn("names a directory", str(caught.exception))
                self.assertIn("names a directory", model.reach_problem(path))


class WalkErrorR6Tests(RuntimeDirTests):
    """NB-R5-5: an OSError inside the walk (EIO, EMFILE, a change of the
    user's own) is the documented refusal, not a crash of status."""

    def test_an_os_error_in_the_walk_is_a_refusal(self):
        vm = self.define("Alpha")
        self.fallback.mkdir(mode=0o700)
        server = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(server.close)
        mine = self.tmp / "mine"
        mine.mkdir(mode=0o700)
        os.symlink("mine", self.tmp / "rel")
        outside = str(self.tmp / "rel" / "x.qmp")
        real = {name: getattr(os, name) for name in ("open", "fstat",
                                                     "readlink")}

        def failing_open(path, flags, *args, **kwargs):
            if flags == model._DIR_FLAGS:
                raise OSError(errno.EIO, "injected I/O error")
            return real["open"](path, flags, *args, **kwargs)

        def failing_fstat(fd):
            info = real["fstat"](fd)
            if model.stat.S_ISDIR(info.st_mode):
                raise OSError(errno.EIO, "injected I/O error")
            return info

        def failing_readlink(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise OSError(errno.EIO, "injected I/O error")
            return real["readlink"](path, *args, **kwargs)

        for name, fake, whole in (("open", failing_open, True),
                                  ("fstat", failing_fstat, True),
                                  ("readlink", failing_readlink, False)):
            with self.subTest(name), mock.patch.object(model.os, name, fake):
                before = open_fds()
                with self.assertRaises(model.RuntimeDirError) as caught:
                    with model.reach(outside):
                        pass
                self.assertIn("injected I/O error", str(caught.exception))
                self.assertIn("injected I/O error",
                              model.reach_problem(outside))
                if whole:                       # every walk fails
                    status = self.manager().status(vm)
                    result = self.manager().control(vm, "poweroff")
                    self.assertEqual(status.state, "stopped")
                    self.assertIn("injected I/O error", status.detail)
                    self.assertFalse(result.ok)
                    self.assertIn("injected I/O error", result.message)
                self.assertEqual(open_fds(), before)
        self.assertEqual(server.seen, [])

    def test_what_the_with_body_raises_is_left_alone(self):
        mine = self.tmp / "mine"
        mine.mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            with model.reach(str(mine / "x.pid")) as where:
                self.assertIsNone(where)
                raise FileNotFoundError(errno.ENOENT, "the caller's own")


class SearchOnlyR6Tests(Sandbox):
    """NB-R5-6: a directory the user may search but not list is traversed,
    as the kernel would (a root-owned 0711 dir, say)."""

    def test_a_search_only_directory_on_the_way_is_traversed(self):
        xonly = self.tmp / "xonly"
        xonly.mkdir(mode=0o700)
        server = FakeQmpServer(str(xonly / "x.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        xonly.chmod(0o311)
        self.addCleanup(xonly.chmod, 0o700)
        self.assertEqual(model.reach_problem(server.path), "")
        with model.reach(server.path) as where:
            self.assertEqual(qmp.status(where, timeout=0.5), "running")
        xonly.chmod(0o000)                      # no search: still refused
        self.assertIn("Permission denied", model.reach_problem(server.path))


class OtherSpellingFdR6Tests(WalkR5Tests):
    """NB-R5-4 (QM-R5's Z11, Y1's twin): another spelling of the runtime
    dir is judged by the descriptor the walk ended in, not by its name."""

    def test_a_swap_after_the_walk_is_judged_as_opened(self):
        vm = self.define("Alpha")
        self.fallback.mkdir()
        self.fallback.chmod(0o755)                      # open: unsafe
        planted = FakeQmpServer(vm.qmp, RUNNING_REPLY)
        self.addCleanup(planted.close)
        spelled = str(self.alias() / "alpha.qmp")
        real_open, name, opens = os.open, self.fallback.name, [0]

        def swapping_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if os.fspath(path) == name and kwargs.get("dir_fd") is not None:
                opens[0] += 1
                if opens[0] == 2:               # the alias walk's open
                    os.rename(self.fallback, self.shared / "opened")
                    self.fallback.mkdir(mode=0o700)
            return fd

        with mock.patch.object(model.os, "open", swapping_open):
            with self.assertRaises(model.RuntimeDirError) as caught:
                with model.reach(spelled):
                    pass
        self.assertIn("it must be 0700", str(caught.exception))
        self.assertEqual(opens[0], 2)
        self.assertEqual(planted.seen, [])


class StoreR6Tests(Sandbox):
    """NB-R5-8 (QM-R5's Z14): only regular files load, not any non-FIFO."""

    def test_only_regular_files_load(self):
        self.define("Alpha")
        os.mkdir(os.path.join(model.vms_dir(), "dir.json"))
        os.symlink("/dev/null", os.path.join(model.vms_dir(), "null.json"))
        os.makedirs(os.path.dirname(model.state_path()), exist_ok=True)
        os.symlink("/dev/null", model.state_path())
        machines, problems = store.load_all()
        self.assertEqual([vm.id for vm in machines], ["alpha"])
        self.assertEqual(problems, [
            "dir.json: dir.json is not a regular file",
            "null.json: null.json is not a regular file"])
        self.assertEqual(store.load_state(), {"favorites": [], "recents": []})
        self.assertIsNone(store.get("null"))


# ── r7: ACLs, a FIFO pidfile, a real other gid, the account list ────────────

ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ = 0x01, 0x02, 0x04
ACL_GROUP, ACL_MASK, ACL_OTHER = 0x08, 0x10, 0x20
ACL_UNDEFINED_ID = 0xFFFFFFFF


def acl_blob(named=(), *, mask: int | None = 7, version: int = 2) -> bytes:
    """A POSIX ACL in the kernel's xattr form, entries in setfacl's order:
    owner rwx, `named` (tag, perm, id), group r-x, mask, others r-x."""
    entries = ([(ACL_USER_OBJ, 7, ACL_UNDEFINED_ID)]
               + sorted(e for e in named if e[0] == ACL_USER)
               + [(ACL_GROUP_OBJ, 5, ACL_UNDEFINED_ID)]
               + sorted(e for e in named if e[0] == ACL_GROUP)
               + ([] if mask is None else [(ACL_MASK, mask, ACL_UNDEFINED_ID)])
               + [(ACL_OTHER, 5, ACL_UNDEFINED_ID)])
    return struct.pack("<I", version) + b"".join(
        struct.pack("<HHI", *entry) for entry in entries)


ACCESS_ACL = "system.posix_acl_access"
DEFAULT_ACL = "system.posix_acl_default"


class AclR7Tests(RuntimeDirTests):
    """R6-B1: under a POSIX ACL the group bits of the mode are the ACL mask,
    so a 0775 directory of the user's private group may be writable by
    another user or group. The private-group exemption is given only when
    the directory has no ACL entry for a named user or group. The ACLs are
    real (set as setfacl does); the account database is group_db's, so the
    process's group is private here whatever the host's is."""

    def setUp(self) -> None:
        super().setUp()
        probe = Path(tempfile.mkdtemp(dir=self.tmp))
        try:
            os.setxattr(probe, ACCESS_ACL,
                        acl_blob([(ACL_USER, 5, 65534)], mask=5))
        except OSError as error:
            self.skipTest(f"no POSIX ACLs where the tests run: {error}")

    def vm_dir(self, mode: int) -> Path:
        vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
        vmdir.chmod(mode)
        return vmdir

    def shared_dirs(self):
        """(label, directory, expected words) for three ways another user
        or group gets write through an ACL: u:X:rwx, g:Y:rwx and a default
        ACL inherited from the parent."""
        by_user = self.vm_dir(0o755)
        os.setxattr(by_user, ACCESS_ACL, acl_blob([(ACL_USER, 7, 65534)]))
        by_group = self.vm_dir(0o755)
        os.setxattr(by_group, ACCESS_ACL, acl_blob([(ACL_GROUP, 7, 65534)]))
        parent = self.vm_dir(0o700)
        os.setxattr(parent, DEFAULT_ACL, acl_blob([(ACL_USER, 7, 65534)]))
        inherited = parent / "vm"
        inherited.mkdir()
        rows = [("u:65534:rwx", by_user, "an entry for user 65534"),
                ("g:65534:rwx", by_group, "an entry for group 65534"),
                ("inherited default", inherited, "an entry for user 65534")]
        for label, path, _words in rows:
            info = path.stat()
            # It looks exactly like a private 0775 directory.
            self.assertEqual(info.st_mode & 0o7777, 0o775, label)
            self.assertEqual(info.st_gid, os.getgid(), label)
            self.assertIn(ACCESS_ACL, os.listxattr(path), label)
        return rows

    def assert_refused(self, vmdir: Path, words: str, label: str) -> None:
        server = FakeQmpServer(str(vmdir / "own.qmp"), RUNNING_REPLY)
        self.addCleanup(server.close)
        vm = self.define(f"Alpha {label}", "-pidfile", str(vmdir / "qemu.pid"))
        launched = len(self.rc.launched)
        problem = model.reach_problem(server.path)
        result = self.manager().launch(vm)
        status = self.manager().status(vm)
        self.assertIn(f"{vmdir}, which is mode 0775 and ", problem)
        self.assertIn(words, problem)
        self.assertIn("another user can change that", problem)
        self.assertFalse(result.ok, result.message)
        self.assertIn(words, result.message)
        self.assertEqual(len(self.rc.launched), launched)
        self.assertFalse(status.pid)
        self.assertEqual(server.seen, [])
        with self.assertRaises(model.RuntimeDirError):
            with model.reach(server.path):
                pass

    def test_a_dir_others_may_write_through_an_acl_is_refused(self):
        with group_db():
            for label, vmdir, words in self.shared_dirs():
                with self.subTest(label):
                    self.assert_refused(vmdir, words, label)

    def test_a_planted_link_in_an_acl_shared_dir_reaches_nothing(self):
        """What the named user could do there: replace the recorded QMP
        socket with a link to another VM's, or the pidfile with a link to a
        file of the user's. Neither is followed, and no launch goes ahead."""
        with group_db():
            (_label, vmdir, _words), = [row for row in self.shared_dirs()
                                        if row[0] == "g:65534:rwx"]
            private = self.vm_dir(0o700)
            bravo = FakeQmpServer(str(private / "bravo.qmp"), RUNNING_REPLY)
            self.addCleanup(bravo.close)
            victim = private / "precious"
            victim.write_text("the user's own data\n")
            os.symlink(bravo.path, vmdir / "alpha.qmp")
            os.symlink(victim, vmdir / "qemu.pid")
            vm = self.define("Alpha", "-pidfile", str(vmdir / "qemu.pid"))
            self.assertFalse(self.manager().launch(vm).ok)
            with self.assertRaises(model.RuntimeDirError):
                with model.reach(str(vmdir / "alpha.qmp")) as where:
                    qmp.execute(where, "quit", timeout=0.5)
        self.assertEqual(bravo.seen, [])
        self.assertEqual(victim.read_text(), "the user's own data\n")

    def test_a_plain_0775_private_group_dir_is_still_used(self):
        with group_db():
            for label, extra in (("no ACL", None),
                                 ("an ACL with a mask and no named entry",
                                  acl_blob())):
                with self.subTest(label):
                    vmdir = self.vm_dir(0o775)
                    if extra is not None:
                        os.setxattr(vmdir, ACCESS_ACL, extra)
                        self.assertIn(ACCESS_ACL, os.listxattr(vmdir))
                    self.assertEqual(vmdir.stat().st_mode & 0o777, 0o775)
                    vm = self.define(f"Alpha {label}", "-pidfile",
                                     str(vmdir / "qemu.pid"))
                    self.assertEqual(vm.pidfile, str(vmdir / "qemu.pid"))
                    self.assertEqual(model.reach_problem(vm.pidfile), "")
                    result = self.manager().launch(vm)
                    self.assertTrue(result.ok, result.message)
                    self.wait_for(lambda: self.manager().status(vm).pid)
                    self.assertTrue(self.manager().control(vm,
                                                           "poweroff").ok)
                    self.wait_for(lambda: not self.manager().status(vm).pid)

    def test_a_named_entry_in_the_dirs_own_default_acl_is_refused(self):
        """It adds no writer to the directory itself, but QEMU's pidfile
        and socket would inherit it; any named entry ends the exemption."""
        with group_db():
            vmdir = self.vm_dir(0o775)
            os.setxattr(vmdir, DEFAULT_ACL, acl_blob([(ACL_USER, 7, 65534)]))
            self.assertNotIn(ACCESS_ACL, os.listxattr(vmdir))
            self.assert_refused(vmdir, f"({DEFAULT_ACL}) with an entry for "
                                "user 65534", "default")

    def test_a_read_only_acl_on_a_0755_dir_keeps_it_usable(self):
        """No group write bit (mask r-x): the exemption is not asked."""
        vmdir = self.vm_dir(0o755)
        os.setxattr(vmdir, ACCESS_ACL, acl_blob([(ACL_USER, 5, 65534)],
                                                mask=5))
        self.assertEqual(vmdir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(model.reach_problem(str(vmdir / "x.qmp")), "")

    def test_an_acl_that_cannot_be_read_or_parsed_is_refused(self):
        real_get, real_list = os.getxattr, os.listxattr

        def raising(code):
            def call(*_args, **_kwargs):
                raise OSError(code, os.strerror(code))
            return call

        def returning(value):
            return lambda *_args, **_kwargs: value

        bad_tag = acl_blob() + struct.pack("<HHI", 0x40, 7, 0)
        cases = (
            ("getxattr EIO", raising(errno.EIO), real_list,
             "its ACL cannot be read (Input/output error)"),
            ("getxattr EACCES", raising(errno.EACCES), real_list,
             "its ACL cannot be read (Permission denied)"),
            ("listxattr EIO", real_get, raising(errno.EIO),
             "its ACL cannot be read (Input/output error)"),
            ("version 3", returning(acl_blob(version=3)), real_list,
             "the manager cannot parse"),
            ("a torn entry", returning(acl_blob()[:-3]), real_list,
             "the manager cannot parse"),
            ("no header", returning(b"\x02"), real_list,
             "the manager cannot parse"),
            ("an unknown tag", returning(bad_tag), real_list,
             "the manager cannot parse"),
            ("an NFSv4 ACL", real_get, returning(["system.nfs4_acl"]),
             "(system.nfs4_acl) the manager does not read"),
            ("a rich ACL", real_get, returning(["system.richacl"]),
             "(system.richacl) the manager does not read"),
        )
        with group_db():
            for label, get, listing, words in cases:
                with self.subTest(label):
                    vmdir = self.vm_dir(0o775)
                    with mock.patch.object(model.os, "getxattr", get), \
                            mock.patch.object(model.os, "listxattr", listing):
                        self.assert_refused(vmdir, words, label)

    def test_no_acl_and_no_acl_support_are_fine(self):
        def raising(code):
            def call(*_args, **_kwargs):
                raise OSError(code, os.strerror(code))
            return call

        with group_db():
            for label, code in (("ENODATA", errno.ENODATA),
                                ("ENOTSUP", errno.ENOTSUP),
                                ("EOPNOTSUPP", errno.EOPNOTSUPP)):
                with self.subTest(label):
                    vmdir = self.vm_dir(0o775)
                    listing = (raising(code) if code != errno.ENODATA
                               else (lambda *_a, **_k: []))
                    with mock.patch.object(model.os, "getxattr",
                                           raising(code)), \
                            mock.patch.object(model.os, "listxattr",
                                              listing):
                        self.assertEqual(
                            model.reach_problem(str(vmdir / "x.qmp")), "")

    def test_without_proc_the_exemption_is_not_given(self):
        """The ACL is read through /proc/self/fd/N; without it, a 0775
        directory is refused, as before r6 (and the README says so)."""
        with group_db():
            vmdir = self.vm_dir(0o775)
            with mock.patch.object(model, "proc_fd_dir",
                                   lambda fd: f"{self.tmp}/no-proc/{fd}"):
                problem = model.reach_problem(str(vmdir / "x.qmp"))
            self.assertIn(f"{vmdir}, which is mode 0775 and its ACL cannot "
                          "be read (No such file or directory)", problem)
            self.assertEqual(model.reach_problem(str(vmdir / "x.qmp")), "")

    def test_the_acl_is_read_from_the_held_descriptor(self):
        """The xattr calls get /proc/self/fd/N of the walk's own fd, never
        the directory's name, so a rename after the walk opened it cannot
        change what is judged."""
        with group_db():
            vmdir = self.vm_dir(0o775)
            seen = []
            real_get = os.getxattr

            def recording(path, *args, **kwargs):
                seen.append(os.fspath(path))
                return real_get(path, *args, **kwargs)

            with mock.patch.object(model.os, "getxattr", recording):
                self.assertEqual(model.reach_problem(str(vmdir / "x.qmp")),
                                 "")
            self.assertTrue(seen)
            self.assertTrue(all(path.startswith("/proc/self/fd/")
                                for path in seen), seen)


class PidfileFifoR7Tests(Sandbox):
    """NB-R6-4: reading a pidfile never blocks on a FIFO or follows a
    symlink: it is opened O_NONBLOCK|O_NOFOLLOW and read only when fstat
    says it is a regular file."""

    def status_in_thread(self, call):
        done: list = []
        worker = threading.Thread(target=lambda: done.append(call()),
                                  daemon=True)
        worker.start()
        worker.join(5)
        return worker, done

    def test_a_fifo_pidfile_never_blocks_status(self):
        vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
        vmdir.chmod(0o700)
        fifo = vmdir / "qemu.pid"
        vm = self.define("Alpha", "-pidfile", str(fifo))
        os.mkfifo(fifo)
        for label, call in (("status", lambda: self.manager().status(vm)),
                            ("pid_of", lambda: self.manager().pid_of(vm))):
            with self.subTest(label):
                worker, done = self.status_in_thread(call)
                hung = worker.is_alive()
                if hung:                        # release the blocked reader
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                    worker.join(5)
                self.assertFalse(hung, f"{label} blocked on a FIFO pidfile")
                result = done[0]
                self.assertFalse(result if label == "pid_of" else result.pid)

    def test_only_a_regular_file_is_read(self):
        vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
        regular = vmdir / "regular.pid"
        regular.write_text("4242\n")
        self.assertEqual(backend.read_pidfile(str(regular)), "4242\n")
        os.mkfifo(vmdir / "fifo.pid")
        os.symlink(regular, vmdir / "link.pid")
        (vmdir / "dir.pid").mkdir()
        for name in ("fifo.pid", "link.pid", "dir.pid"):
            with self.subTest(name), self.assertRaises(OSError):
                backend.read_pidfile(str(vmdir / name))
        regular.write_text("1" * 4096)
        self.assertEqual(len(backend.read_pidfile(str(regular))),
                         backend.PIDFILE_MAX_BYTES)


class RealGroupR7Tests(RuntimeDirTests):
    """NB-R6-3 (QM-R6's W4): the exemption is asked about the directory's
    own gid, from the descriptor the walk holds. Here the kernel's gid is
    one of the user's real supplementary groups, set with chown, while
    group_db makes the process's own group private, so a rule that asked
    about the process's gid would trust this directory."""

    def test_a_dir_of_a_real_other_group_is_refused(self):
        others = [gid for gid in os.getgroups() if gid != os.getgid()]
        if not others:
            self.skipTest("this user has no supplementary group, so no "
                          "directory of theirs can carry another real gid "
                          "without root")
        gid = others[0]
        with group_db():
            vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
            os.chown(vmdir, -1, gid)
            vmdir.chmod(0o775)
            info = vmdir.stat()
            self.assertEqual((info.st_gid, info.st_mode & 0o777),
                             (gid, 0o775))
            self.assertTrue(model._private_group(os.getgid()))
            server = FakeQmpServer(str(vmdir / "own.qmp"), RUNNING_REPLY)
            self.addCleanup(server.close)
            vm = self.define("Alpha", "-pidfile", str(vmdir / "qemu.pid"))
            problem = model.reach_problem(server.path)
            result = self.manager().launch(vm)
            self.assertIn(f"{vmdir}, which is mode 0775 and writable by "
                          "group ", problem)
            self.assertIn("which is not your private group", problem)
            self.assertFalse(result.ok)
            self.assertEqual(self.rc.launched, [])
            self.assertEqual(server.seen, [])
            vmdir.chmod(0o755)                  # no group write: used
            self.assertEqual(model.reach_problem(server.path), "")


class FakeLibc:
    """setpwent/getpwent/endpwent and the gr* three, as model._libc gives
    them, from lists; `stop_after` entries in, getpwent (or getgrent)
    returns NULL with errno `code`, as a source that fails half-way does."""

    def __init__(self, accounts, groups, *, stop="", stop_after=0,
                 code=errno.EIO):
        self.lists = {"pw": list(accounts), "gr": list(groups)}
        self.stop, self.stop_after, self.code = stop, stop_after, code
        self.errno, self.index, self.started, self.ended = 0, {}, [], []

    def set_errno(self, value):
        self.errno = value

    def get_errno(self):
        return self.errno

    def __getattr__(self, name):
        verb, kind = name[:3], name[3:5]
        if verb == "set":
            return lambda: (self.started.append(kind),
                            self.index.__setitem__(kind, 0))
        if verb == "end":
            return lambda: self.ended.append(kind)
        return lambda: self.step(kind)

    def step(self, kind):
        index = self.index[kind]
        if kind == self.stop and index >= self.stop_after:
            self.errno = self.code
            return None
        if index >= len(self.lists[kind]):
            return None
        self.index[kind] = index + 1
        entry = self.lists[kind][index]
        if kind == "pw":
            return types.SimpleNamespace(contents=types.SimpleNamespace(
                pw_name=entry[0].encode(), pw_gid=entry[1],
                pw_uid=os.getuid() if entry[0] == pwd.getpwuid(os.getuid()).pw_name else entry[1] + 50000))
        return types.SimpleNamespace(contents=types.SimpleNamespace(
            gr_name=entry[0].encode(), gr_gid=entry[1],
            gr_mem=[member.encode() for member in entry[2]] + [None]))


class SupplementaryMembershipR8Tests(RuntimeDirTests):
    """An account's initgroups answer can grant a gid absent from getgrent."""

    def problem(self):
        vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
        vmdir.chmod(0o775)
        return model.reach_problem(str(vmdir / "x.qmp"))

    def test_supplementary_member_missing_from_the_group_listing_refuses(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        gid = os.getgid()
        peer_gid = gid + 51000
        with group_db(), mock.patch.object(model, "_accounts", return_value=[
                Account(me, gid, os.getuid()), Account("qemu-mgr-peer", peer_gid)]), \
                mock.patch.object(os, "getgrouplist", return_value=[
                    peer_gid, gid]) as memberships:
            problem = self.problem()
        self.assertIn("which is not your private group", problem)
        self.assertIn("supplementary group", problem)
        memberships.assert_called_with("qemu-mgr-peer", peer_gid)

    def test_a_private_group_with_no_other_members_is_still_used(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        gid = os.getgid()
        peer_gid = gid + 51000
        with group_db(sources="files systemd"), \
                mock.patch.object(model, "_accounts", return_value=[
                    Account(me, gid, os.getuid()), Account("qemu-mgr-peer", peer_gid)]), \
                mock.patch.object(os, "getgrouplist", return_value=[
                    peer_gid]) as memberships:
            self.assertEqual(self.problem(), "")
        memberships.assert_called_with("qemu-mgr-peer", peer_gid)

    def test_membership_lookup_failure_refuses(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        gid = os.getgid()
        for error in [OSError(errno.EIO, "membership lookup failed"),
                      KeyError("missing account")]:
            with self.subTest(error=type(error).__name__), group_db(), \
                    mock.patch.object(model, "_accounts", return_value=[
                        Account(me, gid, os.getuid()), Account("qemu-mgr-peer", gid + 1)]), \
                    mock.patch.object(os, "getgrouplist", side_effect=error):
                self.assertIn("which is not your private group", self.problem())

    def test_each_other_accounts_supplementary_groups_are_checked(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        gid = os.getgid()

        def memberships(user, primary):
            return [primary, gid] if user == "qemu-mgr-peer2" else [primary]

        with group_db(), mock.patch.object(model, "_accounts", return_value=[
                Account(me, gid, os.getuid()), Account("qemu-mgr-peer1", gid + 1),
                Account("qemu-mgr-peer2", gid + 2)]), \
                mock.patch.object(os, "getgrouplist", side_effect=memberships):
            self.assertIn("supplementary group", self.problem())

    def test_an_explicit_non_listing_initgroups_source_refuses(self):
        for source in ["sss", "ldap", "qemu-mgr-directory"]:
            with self.subTest(source=source), group_db():
                with open(model.NSSWITCH, "a") as handle:
                    handle.write(f"initgroups: files {source}\n")
                problem = self.problem()
                self.assertIn("which is not your private group", problem)
                self.assertIn("initgroups", problem)

    def test_supported_or_implicit_initgroups_sources_keep_private_groups(self):
        for line in ["", "initgroups: files\n",
                     "initgroups: files systemd\n"]:
            with self.subTest(line=line), group_db():
                with open(model.NSSWITCH, "a") as handle:
                    handle.write(line)
                self.assertEqual(self.problem(), "")

    def test_an_explicit_empty_initgroups_database_refuses(self):
        with group_db():
            with open(model.NSSWITCH, "a") as handle:
                handle.write("initgroups:\n")
            self.assertIn("names no source for initgroups", self.problem())


class LocalMembershipProofTests(RuntimeDirTests):
    """Whole-file authority and conservative systemd scope, using real files."""

    def test_config_limit_requires_eof_in_bytes(self):
        conf = self.tmp / "nsswitch"
        prefix = b"passwd: files\ngroup: files\n#"
        with mock.patch.object(model, "NSSWITCH", str(conf)):
            for size, accepted in [(65535, True), (65536, True), (65537, False)]:
                conf.write_bytes(prefix + b" " * (size - len(prefix)))
                self.assertEqual(model._sources_problem() == "", accepted)
            conf.write_bytes(prefix + b" " * 65536 + b"\npasswd: files ldap\n")
            self.assertTrue(model._sources_problem())
            conf.write_bytes(prefix + "é".encode() * 32768)
            self.assertTrue(model._sources_problem())
            conf.write_bytes(prefix + b"\0")
            self.assertTrue(model._sources_problem())

    def test_local_files_keep_uid_identity_and_all_group_lines(self):
        passwd = self.tmp / "passwd"
        group = self.tmp / "group"
        own = "owner:x:1000:1000::/:/bin/sh\n"
        with mock.patch.object(model, "PASSWD_FILE", str(passwd)), \
                mock.patch.object(model, "GROUP_FILE", str(group)):
            cases = [
                (own, "owner:x:1000:\n", True),
                (own + "owner:x:61002:1000::/:/bin/sh\n", "owner:x:1000:\n", False),
                (own + "peer:x:61002:1000::/:/bin/sh\n", "owner:x:1000:\n", False),
                (own + "alias:x:1000:1000::/:/bin/sh\n", "owner:x:1000:\n", True),
                (own, "owner:x:1000:\nteam:x:1000:peer\n", False),
                (own, "owner:x:1000:peer\n", False),
                (own + "broken\n", "owner:x:1000:\n", False),
                (own, "owner:x:1000:\nbroken\n", False),
                (own, "", False),
                ("", "owner:x:1000:\n", False),
            ]
            for pw, gr, accepted in cases:
                with self.subTest(pw=pw, gr=gr):
                    passwd.write_text(pw); group.write_text(gr)
                    self.assertEqual(model._files_group_problem("owner", 1000, 1000) == "", accepted)

    def test_systemd_providers_are_not_membership_authorities(self):
        services = self.tmp / "services"; services.mkdir()
        records = self.tmp / "records"; records.mkdir()
        with mock.patch.object(model, "USERDB_SERVICES", str(services)), \
                mock.patch.object(model, "USERDB_RECORDS", (str(records),)), \
                mock.patch.object(os, "getuid", return_value=1000), \
                mock.patch.object(os, "getgid", return_value=1000):
            self.assertEqual(model._systemd_authority_problem(), "")
            dynamic = services / "io.systemd.DynamicUser"; dynamic.touch()
            self.assertEqual(model._systemd_authority_problem(), "")
            for name in ["io.systemd.Home", "io.systemd.Multiplexer", "custom"]:
                provider = services / name; provider.touch()
                self.assertTrue(model._systemd_authority_problem())
                with group_db(sources="files systemd"):
                    self.assertTrue(model._sources_problem())
                provider.unlink()
            record = records / "peer:owner.membership"; record.touch()
            self.assertTrue(model._systemd_authority_problem())
            record.unlink(); records.rmdir(); records.write_text("not a directory")
            with self.assertRaises(OSError):
                model._systemd_authority_problem()

    def test_nss_accounts_preserve_uid_even_with_duplicate_names(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        gid = os.getgid()
        with group_db(), mock.patch.object(model, "_accounts", return_value=[
                Account(me, gid, os.getuid()), Account(me, gid, os.getuid() + 1)]):
            self.assertIn("another account", model._private_group_problem(gid))


class AccountListR7Tests(RuntimeDirTests):
    """NB-R6-1 and NB-R6-2: the private-group test fails closed when the
    account list cannot be complete, and sees every group line with the
    user's gid."""

    def vm_dir(self) -> Path:
        vmdir = Path(tempfile.mkdtemp(dir=self.tmp))
        vmdir.chmod(0o775)
        return vmdir

    def problem(self) -> str:
        return model.reach_problem(str(self.vm_dir() / "x.qmp"))

    def test_only_sources_that_list_every_account_are_trusted(self):
        for sources, used in (("files", True), ("files systemd", True),
                              ("files [NOTFOUND=return] systemd", True),
                              ("files sss", False), ("sss files", False),
                              ("ldap", False), ("compat", False),
                              ("files winbind", False)):
            with self.subTest(sources), group_db(sources=sources):
                problem = self.problem()
                if used:
                    self.assertEqual(problem, "")
                else:
                    self.assertIn("which is mode 0775 and writable by group",
                                  problem)
                    self.assertIn("need not list every account", problem)

    def test_an_nsswitch_conf_without_both_lines_or_unreadable_refuses(self):
        for label, text, words in (
                ("no group line", "passwd: files\n", "has no group line"),
                ("no passwd line", "group: files\n", "has no passwd line"),
                ("no source", "passwd:\ngroup: files\n",
                 "names no source for passwd"),
                ("missing", None, "cannot be read")):
            with self.subTest(label), group_db():
                conf = self.tmp / f"nsswitch-{label.replace(' ', '-')}"
                if text is not None:
                    conf.write_text(f"# a comment: passwd: ldap\n{text}")
                with mock.patch.object(model, "NSSWITCH", str(conf)):
                    problem = self.problem()
                self.assertIn("which is not your private group", problem)
                self.assertIn(words, problem)

    def test_a_listing_that_stops_on_an_error_refuses(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        gid = os.getgid()
        accounts = [("root", 0), (me, gid), ("qemu-mgr-twin", gid)]
        groups = [("root", 0, []), (me, gid, [])]
        for label, libc, used in (
                ("complete, with the twin", FakeLibc(accounts, groups), False),
                ("complete, without it", FakeLibc(accounts[:2], groups), True),
                ("passwd stops before the twin (EIO)",
                 FakeLibc(accounts, groups, stop="pw", stop_after=2), False),
                ("group stops (EIO)",
                 FakeLibc(accounts[:2], groups, stop="gr", stop_after=1),
                 False),
                ("passwd ends with ENOENT",
                 FakeLibc(accounts[:2], groups, stop="pw", stop_after=2,
                          code=errno.ENOENT), True)):
            with self.subTest(label), group_db(listing=False), \
                    mock.patch.object(model, "_libc", create=True,
                                      return_value=(libc, libc.set_errno,
                                                    libc.get_errno)):
                problem = self.problem()
                self.assertTrue(libc.started)    # every listing is ended
                self.assertEqual(sorted(libc.ended), sorted(libc.started))
                if used:
                    self.assertEqual(problem, "")
                else:
                    self.assertIn("which is not your private group", problem)

        libc = FakeLibc(accounts, groups, stop="pw", stop_after=2)
        with mock.patch.object(model, "_libc", create=True,
                               return_value=(libc, libc.set_errno,
                                             libc.get_errno)):
            with self.assertRaises(OSError) as caught:
                model._accounts()
        self.assertIn("the account list stopped", str(caught.exception))
        self.assertEqual(libc.ended, ["pw"])

    def test_the_real_listing_sees_this_user(self):
        me = pwd.getpwuid(os.getuid())
        self.assertIn((me.pw_name, me.pw_gid),
                      [(entry.pw_name, entry.pw_gid)
                       for entry in model._accounts()])
        self.assertEqual(sorted(entry.pw_name for entry in model._accounts()),
                         sorted(entry.pw_name for entry in pwd.getpwall()))
        self.assertEqual(sorted((g.gr_name, g.gr_gid, tuple(g.gr_mem))
                                for g in model._groups()),
                         sorted((g.gr_name, g.gr_gid, tuple(g.gr_mem))
                                for g in grp.getgrall()))

    def test_the_group_must_be_the_users_primary_group(self):
        """getgid() is the user's primary gid unless newgrp or sg changed
        it; a group the user only switched to is not their private group."""
        me = pwd.getpwuid(os.getuid())
        with group_db():
            self.assertEqual(self.problem(), "")
            moved = pwd.struct_passwd((me.pw_name, "x", me.pw_uid,
                                       os.getgid() + 1, "", me.pw_dir,
                                       "/bin/sh"))
            with mock.patch.object(pwd, "getpwuid", return_value=moved):
                self.assertIn("(it is not your primary group)",
                              self.problem())

    def test_an_account_list_that_cannot_be_read_at_all_refuses(self):
        """No ctypes, or a C library without getpwent: a refusal with the
        reason, never an exception out of status."""
        vm = self.define("Alpha", "-pidfile", str(self.vm_dir() / "q.pid"))
        for label, error in (("no ctypes", ImportError("No module named "
                                                       "'ctypes'")),
                             ("no getpwent", AttributeError("getpwent"))):
            with self.subTest(label), group_db(listing=False), \
                    mock.patch.object(model, "_libc", side_effect=error):
                problem = self.problem()
                self.assertIn("(the account list cannot be read (", problem)
                status = self.manager().status(vm)
                self.assertFalse(status.pid)
                self.assertIn("the account list cannot be read",
                              status.detail)

    def test_getgrgid_is_checked_as_well_as_the_listing(self):
        """nscd caches getgrgid but not getgrent, so the two can disagree:
        the group's own entry from getgrgid is checked too, not only the
        lines the listing shows."""
        me = pwd.getpwuid(os.getuid()).pw_name
        for label, db, words in (
                ("another name", {"name": "staff"},
                 "(it does not have your user name)"),
                ("another member", {"members": ("qemu-mgr-other",)},
                 "(it has another member)")):
            with self.subTest(label), group_db(listed=(me, []), **db):
                self.assertIn(words, self.problem())
        with group_db(listed=(me, [])):
            self.assertEqual(self.problem(), "")

    def test_a_second_group_line_with_the_gid_refuses(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        for label, line, used in (
                ("another group, with a member",
                 ("qemu-mgr-team", ["qemu-mgr-mallory"]), False),
                ("another group, no member", ("qemu-mgr-team", []), False),
                ("the user's name again, another member",
                 (me, ["qemu-mgr-mallory"]), False),
                ("the user's name again, no member", (me, []), True)):
            with self.subTest(label), group_db(second_line=line):
                problem = self.problem()
                if used:
                    self.assertEqual(problem, "")
                else:
                    self.assertIn("(another group line has its gid)", problem)


if __name__ == "__main__":
    unittest.main()
