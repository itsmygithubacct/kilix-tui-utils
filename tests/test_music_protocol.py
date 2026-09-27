"""Real Unix peer, framing, version and source-state refusal controls."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kilix_tui.music_protocol import MusicControl, control_parent, validate_reply


PING = {"protocol": 2, "ok": True, "max_protocol": 2, "encodec": True, "live_sources": True}
LIVE = {
    "protocol": 2, "ok": True, "state": "playing", "title": "stream", "file": "/private/audio.sock",
    "pos": 65.12, "len": None, "volume": 50, "repeat": 0, "shuffle": False, "index": 0, "count": 1,
    "source_type": "encodec-unix", "codec": "encodec", "profile": 1, "sample_rate": 24000,
    "channels": 1, "bitrate": 6000, "threads": 2, "ready": True, "model_ready": True, "live": True,
    "buffering": False, "seekable": False, "ended": False, "degraded": False, "reconnect_required": False,
    "wire_valid": True, "wire_pts_ms": "18446744073709551615", "wire_epoch": "0",
    "source_error_code": 0, "source_error_message": "", "source_error_recoverable": False,
}


class Peer:
    def __init__(self, respond=None, path=None):
        self.tmp = tempfile.TemporaryDirectory() if path is None else None
        self.path = path or str(Path(self.tmp.name) / "control.sock")
        self.respond = respond or (lambda request, client: client.sendall(json.dumps(PING).encode() + b"\n"))
        self.requests = []
        self.errors = []
        self.stop = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(8)
        self.sock.settimeout(.05)
        self.thread = threading.Thread(target=self.run)

    def __enter__(self):
        self.thread.start()
        return self

    def run(self):
        while not self.stop.is_set():
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(1)
                try:
                    data = bytearray()
                    while b"\n" not in data and len(data) <= 8192:
                        chunk = client.recv(8192)
                        if not chunk:
                            break
                        data.extend(chunk)
                    if data:
                        request = json.loads(data)
                        self.requests.append(request)
                        self.respond(request, client)
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    pass
                except Exception as failure:
                    self.errors.append(repr(failure))

    def __exit__(self, *args):
        self.stop.set()
        self.sock.close()
        self.thread.join(3)
        if self.tmp:
            self.tmp.cleanup()
        if self.thread.is_alive() or self.errors:
            raise AssertionError((self.thread.is_alive(), self.errors))


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


class ReplyTests(unittest.TestCase):
    def test_live_exact_u64_and_unknown_duration(self):
        self.assertEqual(validate_reply(encoded(LIVE)), LIVE)
        pending = dict(LIVE, wire_valid=False, wire_pts_ms=None, wire_epoch=None, ready=False, model_ready=False)
        self.assertEqual(validate_reply(encoded(pending)), pending)
        local = dict(LIVE, source_type="file", live=False, seekable=True, len=82.1)
        self.assertEqual(validate_reply(encoded(local)), local)

    def test_typed_complete_v2_state(self):
        cases = [(key, value) for key in ("ready", "seekable", "live", "model_ready", "wire_valid")
                 for value in (0, 1, "true", None, [], {})]
        cases += [(key, value) for key in ("profile", "channels", "bitrate", "threads", "source_error_code")
                  for value in (True, 1.0, "1", -1, None, [], {})]
        cases += [("state", []), ("state", "unknown"), ("source_type", {}), ("codec", []),
                  ("pos", -1), ("pos", 10 ** 400), ("pos", True), ("len", 10), ("seekable", True),
                  ("wire_pts_ms", 3), ("wire_epoch", "01"), ("wire_epoch", "-1"),
                  ("wire_epoch", "18446744073709551616"), ("wire_pts_ms", "١"),
                  ("wire_epoch", ""), ("wire_valid", False)]
        for key, value in cases:
            with self.subTest(key=key, value=str(value)[:40]):
                with self.assertRaises(ValueError):
                    validate_reply(encoded(dict(LIVE, **{key: value})))
        for key in ("model_ready", "source_error_code", "source_type", "wire_pts_ms", "len"):
            with self.subTest(missing=key):
                row = dict(LIVE)
                del row[key]
                with self.assertRaises(ValueError):
                    validate_reply(encoded(row))

    def test_malformed_json_and_bounded_population(self):
        for raw in (b'[]', b'null', b'{"protocol":2,"protocol":1,"ok":true}',
                    b'{"protocol":true,"ok":true}', b'{"protocol":2,"ok":1}',
                    b'{"protocol":2,"ok":true,"x":NaN}', b'{"protocol":2,"ok":true,"x":1e999}',
                    b'{"protocol":2,"ok":true,"x":"\\ud800"}',
                    b'{"protocol":2,"ok":true,"x":"\\u0000"}',
                    b'{"protocol":2,"ok":true,"x":"\xff"}', encoded(dict(PING, title="x" * 8193)),
                    encoded(dict(PING, extra=[0] * 32768)), b" " * (1024 * 1024 + 1)):
            with self.subTest(prefix=raw[:90]):
                with self.assertRaises(ValueError):
                    validate_reply(raw)
        deep = dict(PING)
        value = []
        for _ in range(17):
            value = [value]
        deep["extra"] = value
        with self.assertRaises(ValueError):
            validate_reply(encoded(deep))

    def test_errors_require_typed_structured_v2_fields(self):
        failure = {"protocol": 2, "ok": False, "error": "not ready",
                   "error_code": "NOT_READY", "error_recoverable": True}
        self.assertEqual(validate_reply(encoded(failure)), failure)
        for key in ("error", "error_code", "error_recoverable"):
            row = dict(failure)
            del row[key]
            with self.assertRaises(ValueError):
                validate_reply(encoded(row))
        old = {"protocol": 1, "ok": False, "error": "unsupported version"}
        self.assertEqual(validate_reply(encoded(old)), old)


class ExchangeTests(unittest.TestCase):
    def test_bounded_utf8_source_uses_utf8_wire_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, Peer() as peer:
            directory = Path(tmp)
            for _ in range(23):
                directory /= "\U0001f3b5" * 40
                directory.mkdir()
            path = str(directory / "actual.wav")
            import wave
            with wave.open(path, "wb") as audio:
                audio.setparams((1, 2, 24000, 24, "NONE", "not compressed"))
                audio.writeframes(b"\0" * 48)
            self.assertLess(len(path.encode("utf-8")), 4096)
            self.assertGreater(len(json.dumps({"path": path}).encode()), 8192)
            control = MusicControl(peer.path)
            try:
                self.assertTrue(control.command("open", source_type="file", path=path).get("ok"), control.error)
                self.assertEqual([r["cmd"] for r in peer.requests], ["ping", "open"])
                self.assertEqual(peer.requests[-1]["path"], path)
            finally:
                control.close()

    def test_fragments_are_read_until_newline(self):
        def respond(request, client):
            body = encoded(PING if request["cmd"] == "ping" else LIVE) + b"\n"
            for start in range(0, len(body), 3):
                client.sendall(body[start:start + 3])
        with Peer(respond) as peer:
            control = MusicControl(peer.path)
            self.assertEqual(control.command("state"), LIVE)
            self.assertEqual(control.version, 2)
            self.assertEqual([r["cmd"] for r in peer.requests], ["ping", "state"])
            control.close()

    def test_old_server_fallback_is_read_only_before_mutation(self):
        def respond(request, client):
            result = {"protocol": 1, "ok": request["protocol"] == 1}
            if not result["ok"]:
                result["error"] = "unsupported version"
            client.sendall(encoded(result) + b"\n")
        with Peer(respond) as peer:
            control = MusicControl(peer.path)
            self.assertTrue(control.command("play", index=0)["ok"])
            self.assertEqual([(r["cmd"], r["protocol"]) for r in peer.requests],
                             [("ping", 2), ("ping", 1), ("play", 1)])
            count = len(peer.requests)
            self.assertEqual(control.command("open", source_type="file", path="/x.kenc"), {})
            self.assertEqual(len(peer.requests), count)
            control.close()

    def test_negotiation_never_mutates_on_wrong_or_changing_version(self):
        for reply in (dict(PING, protocol=99), {"protocol": 2, "ok": True}, dict(PING, encodec=1)):
            with self.subTest(reply=reply):
                with Peer(lambda request, client: client.sendall(encoded(reply) + b"\n")) as peer:
                    control = MusicControl(peer.path)
                    self.assertEqual(control.command("quit"), {})
                    self.assertEqual([r["cmd"] for r in peer.requests], ["ping"])
        def changing(request, client):
            reply = {"protocol": 1, "ok": True} if request["protocol"] == 2 else PING
            client.sendall(encoded(reply) + b"\n")
        with Peer(changing) as peer:
            self.assertEqual(MusicControl(peer.path).command("play"), {})
            self.assertEqual([r["cmd"] for r in peer.requests], ["ping", "ping"])

    def test_peer_replacement_is_checked_before_sending_private_path(self):
        with Peer() as old:
            control = MusicControl(old.path)
            self.assertTrue(control.negotiate())
            original = control.identity
            os.unlink(old.path)
            with Peer(path=old.path) as replacement:
                self.assertEqual(control.command("add", path="/private/secret.wav"), {})
                self.assertIn("changed", control.error)
                self.assertEqual(replacement.requests, [])
                self.assertIsNone(control.identity)
                self.assertEqual(len(old.requests), 1)
                self.assertNotEqual(os.stat(old.path).st_ino, original[1])
                self.assertTrue(control.negotiate())
                self.assertEqual([r["cmd"] for r in replacement.requests], ["ping"])
            control.close()

    def test_nonmatching_peer_uid_receives_no_request(self):
        with Peer() as peer:
            control = MusicControl(peer.path)
            with patch("kilix_tui.music_protocol.struct.unpack", return_value=(123, os.geteuid() + 1, 123)):
                self.assertFalse(control.negotiate())
            self.assertEqual(peer.requests, [])

    def test_absolute_deadline_defeats_trickled_reply(self):
        def trickle(request, client):
            for byte in encoded(PING) + b"\n":
                client.sendall(bytes([byte]))
                time.sleep(.02)
        with Peer(trickle) as peer:
            control = MusicControl(peer.path)
            started = time.monotonic()
            self.assertFalse(control.negotiate(timeout=.15))
            self.assertLess(time.monotonic() - started, .8)
            self.assertIsNone(control.identity)

    def test_close_interrupts_inflight_request_without_quitting_peer(self):
        entered = threading.Event()
        release = threading.Event()
        def stall(request, client):
            entered.set()
            release.wait(2)
        with Peer(stall) as peer:
            control = MusicControl(peer.path)
            thread = threading.Thread(target=lambda: control.command("play"))
            thread.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            control.close()
            thread.join(1)
            release.set()
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(control.command("quit"), {})
            self.assertEqual([r["cmd"] for r in peer.requests], ["ping"])

    def test_truncated_oversized_and_extra_reply_refuse(self):
        for body in (encoded(PING), b" " * (1024 * 1024 + 1), encoded(PING) + b"\n{}\n"):
            with self.subTest(size=len(body)):
                with Peer(lambda request, client: client.sendall(body)) as peer:
                    control = MusicControl(peer.path)
                    self.assertFalse(control.negotiate())
                    self.assertEqual([r["cmd"] for r in peer.requests], ["ping"])


class EndpointTests(unittest.TestCase):
    def test_unsafe_paths_never_modify_existing_files_or_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file"
            target.write_bytes(b"preserve")
            target.chmod(0o640)
            (root / "alias").symlink_to(target)
            directory = root / "dir"
            directory.mkdir(mode=0o700)
            (root / "linked-dir").symlink_to(directory, target_is_directory=True)
            for path in (str(target), str(root / "alias"), str(root / "linked-dir" / "s"),
                         str(root / "dir" / ".." / "s"), str(root) + "//s", "relative", "/" + "a" * 108):
                with self.subTest(path=path):
                    self.assertFalse(MusicControl(path).available())
                    self.assertFalse(MusicControl(path).negotiate())
            self.assertEqual(target.read_bytes(), b"preserve")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertTrue((root / "alias").is_symlink())

    def test_parent_and_endpoint_permissions_are_refused_not_repaired(self):
        with Peer() as peer:
            path = Path(peer.path)
            for mode in (0o666, 0o640, 0o400, 0o700):
                path.chmod(mode)
                self.assertFalse(MusicControl(peer.path).available())
                self.assertEqual(path.stat().st_mode & 0o777, mode)
            path.chmod(0o600)
            path.parent.chmod(0o750)
            self.assertFalse(MusicControl(peer.path).negotiate())
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o750)
            path.parent.chmod(0o700)
            self.assertTrue(MusicControl(peer.path).negotiate())

    def test_creation_only_below_safe_pinned_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with control_parent(str(root / "one" / "two" / "control"), create=True):
                pass
            self.assertEqual((root / "one").stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "one" / "two").stat().st_mode & 0o777, 0o700)
            (root / "one").chmod(0o777)
            with self.assertRaises(ValueError):
                with control_parent(str(root / "one" / "absent" / "control"), create=True):
                    pass
            self.assertFalse((root / "one" / "absent").exists())


if __name__ == "__main__":
    unittest.main()
