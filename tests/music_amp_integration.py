"""Optional real Amp contract check; explicit binaries and local fixtures only.

Run with --help. The ordinary suite needs no native runtime or model assets.
This check inherits the explicitly selected native runtime/model environment;
it is not an installed-asset admission or inference performance measurement.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import termios
import time
import wave

from test_music import music
from kilix_tui import app


@contextmanager
def environment(**values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run(options):
    checks = 0
    result = {"amp_sha256": hashlib.sha256(options.amp.read_bytes()).hexdigest(),
              "legacy_amp_sha256": hashlib.sha256(options.legacy_amp.read_bytes()).hexdigest(),
              "fixture_sha256": hashlib.sha256(options.mono.read_bytes()).hexdigest(),
              "stereo_fixture_sha256": hashlib.sha256(options.stereo.read_bytes()).hexdigest(),
              "replies": [], "frames": {}}
    options.output.parent.mkdir(parents=True, exist_ok=True)
    def check(condition, detail):
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(detail)
    def command(backend, name, **fields):
        reply = backend.command(name, **fields)
        result["replies"].append(reply)
        check(reply.get("ok") is True, (name, reply, backend.error))
        return reply
    def until(backend, predicate):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            reply = command(backend, "state")
            if predicate(reply):
                return reply
            if reply.get("source_error_code"):
                raise AssertionError(reply)
            time.sleep(.03)
        raise AssertionError("source did not reach the requested state")
    try:
        with tempfile.TemporaryDirectory(prefix="music-amp-") as tmp:
            root = Path(tmp)
            (root / "home").mkdir(mode=0o700)
            wav = root / "ordinary file.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                audio.writeframes(bytes(24000 * 2 * 4))
            with environment(HOME=str(root / "home"), XDG_RUNTIME_DIR=tmp,
                             XDG_CONFIG_HOME=str(root / "config"), SDL_AUDIODRIVER="dummy",
                             KILIX_AMP_SOCKET=str(root / "control"), KILIXAMP_EXIT_AFTER_MS="0"):
                with environment(KILIX_AMP=str(options.legacy_amp)):
                    old = music.Backend()
                    try:
                        check(old.start(), old.error)
                        check(old.version == 1, old.version)
                        command(old, "add", path=str(wav))
                        command(old, "play", index=0)
                        until(old, lambda reply: reply.get("len", 0) > 0)
                        command(old, "pause")
                        command(old, "seek", pos=1.25)
                        check(abs(command(old, "state")["pos"] - 1.25) < .05, "legacy seek")
                        check(not old.command("open", source_type="file", path=str(options.mono)), "legacy explicit source refused")
                    finally:
                        child = old._owned
                        old.close()
                        check(child is None or child.poll() is not None, "legacy child exited")
                with environment(KILIX_AMP=str(options.amp)):
                    backend = music.Backend()
                    try:
                        check(backend.start(), backend.error)
                        check(backend.version == 2 and backend.capabilities["encodec"], "native v2 negotiation")
                        command(backend, "open", source_type="file", path=str(options.mono))
                        ready = until(backend, lambda reply: reply.get("ready") and reply.get("len") is not None)
                        check(ready["profile"] == 1 and ready["model_ready"] and ready["seekable"], ready)
                        check(ready["threads"] == 2 and not ready["live"], ready)
                        command(backend, "pause")
                        command(backend, "seek", pos=1.12)
                        # EnCodec seeks to the preceding verified epoch anchor.
                        sought = until(backend, lambda reply: abs(reply["pos"] - 1.0) < .01)
                        check(sought["state"] == "paused", "asynchronous seek preserves pause")
                        # A legacy client still receives a legacy file shape.
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as legacy:
                            legacy.settimeout(5); legacy.connect(backend.path)
                            legacy.sendall(b'{"protocol":1,"cmd":"state"}\n')
                            with legacy.makefile("rb") as stream:
                                old_reply = json.loads(stream.readline(1048576))
                        check(old_reply["protocol"] == 1 and old_reply["ok"] and old_reply["len"] > 0, old_reply)
                        result["replies"].append(old_reply)
                        command(backend, "open", source_type="file", path=str(options.stereo))
                        stereo = until(backend, lambda reply: reply.get("ready") and reply.get("len") is not None)
                        check(stereo["profile"] == 2 and stereo["channels"] == 2 and stereo["sample_rate"] == 48000, stereo)
                        check(stereo["model_ready"] and stereo["seekable"] and not stereo["live"], stereo)
                        # A real producer sends one verified epoch and an explicit clean end.
                        data = options.mono.read_bytes()
                        header = bytearray(data[:64])
                        check(header[:8] == b"KENC\x01\r\n\x1a" and header[8] == 1, "mono fixture identity")
                        header[11] = 1; header[24:40] = bytes(16); header[44:48] = bytes(4)
                        offset = struct.unpack_from("<Q", data, 48)[0]
                        header[48:56] = struct.pack("<Q", 64)
                        packets = []
                        for _ in range(25):
                            size = struct.unpack_from("<I", data, offset)[0]
                            check(0 < size <= 160, "bounded fixture packet")
                            packets.append(data[offset:offset + size + 4]); offset += size + 4
                        source = root / "source"
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as producer:
                            producer.bind(str(source)); source.chmod(0o600); producer.listen(1); producer.settimeout(5)
                            errors = []
                            def send_audio():
                                try:
                                    peer, _ = producer.accept()
                                    with peer:
                                        peer.settimeout(5)
                                        peer.sendall(bytes(header) + b"".join(packets) + bytes(4))
                                except Exception as failure:
                                    errors.append(repr(failure))
                            thread = threading.Thread(target=send_audio)
                            thread.start()
                            command(backend, "open", source_type="encodec-unix", path=str(source))
                            ended = until(backend, lambda reply: reply.get("ended") and reply.get("state") == "stopped")
                            thread.join(6)
                            check(not thread.is_alive() and not errors, errors)
                        check(ended["len"] is None and not ended["seekable"] and ended["live"], ended)
                        check(ended["wire_valid"] and isinstance(ended["wire_pts_ms"], str), ended)
                        state = music.State()
                        state.backend = backend
                        state.status = ended
                        state.playlist = [str(source)]
                        # Render the actual captured state without initiating an extra poll.
                        state.tick = lambda: None
                        for height, width in ((30, 120), (24, 80), (14, 60), (8, 25), (3, 8)):
                            frame = app.render_to_text(music.render, state, height=height, width=width)
                            result["frames"][f"{height}x{width}"] = frame
                            check(all(len(line) <= width for line in frame.splitlines()), "resize clipping")
                        check("LIVE" in result["frames"]["30x120"] and "ended" in result["frames"]["30x120"], "real live frame")
                        attached = music.Backend()
                        check(attached.start(), attached.error)
                        attached.close()
                        check(backend._owned.poll() is None, "attached close preserves owner")
                        command(backend, "state")
                        # Exercise the actual curses loop, SIGWINCH resize and
                        # quit through a PTY while attached to the real backend.
                        master, slave = pty.openpty()
                        capture = bytearray()
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("4H", 24, 100, 0, 0))
                        ui = subprocess.Popen([sys.executable, music.__file__], stdin=slave, stdout=slave,
                                              stderr=slave, env=dict(os.environ, TERM="xterm-256color"),
                                              start_new_session=True)
                        os.close(slave)
                        def drain(timeout):
                            deadline = time.monotonic() + timeout
                            while time.monotonic() < deadline and len(capture) < 2 * 1024 * 1024:
                                if not select.select([master], [], [], min(.05, deadline - time.monotonic()))[0]:
                                    continue
                                try:
                                    chunk = os.read(master, 65536)
                                except OSError:
                                    break
                                if not chunk:
                                    break
                                capture.extend(chunk)
                        try:
                            drain(1.3)
                            check(ui.poll() is None and b"Music" in capture and b"LIVE" in capture, "PTY real state")
                            for height, width in ((14, 60), (3, 8), (24, 100)):
                                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("4H", height, width, 0, 0))
                                os.kill(ui.pid, signal.SIGWINCH)
                                drain(.15)
                            os.write(master, b"?"); drain(.15)
                            os.write(master, b"x"); drain(.15)
                            os.write(master, b"q"); drain(1)
                            check(ui.wait(timeout=5) == 0 and b"Traceback" not in capture, "PTY exit")
                            check(backend._owned.poll() is None, "PTY attached close preserves backend")
                            result["pty_capture_hex"] = capture.hex()
                        finally:
                            if ui.poll() is None:
                                ui.kill(); ui.wait(timeout=5)
                            os.close(master)
                    finally:
                        child = backend._owned
                        backend.close()
                        check(child is None or child.poll() is not None, "native owner exited")
                        check(not Path(backend.path).exists(), "native owned socket removed")
        result["passed"] = True
    except Exception as failure:
        result["passed"] = False
        result["failure"] = repr(failure)
        raise
    finally:
        result["reply_and_assertion_checks"] = checks
        options.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": True, "reply_and_assertion_checks": checks, "output": str(options.output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amp", type=Path, required=True)
    parser.add_argument("--legacy-amp", type=Path, required=True)
    parser.add_argument("--mono", type=Path, required=True)
    parser.add_argument("--stereo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    for key in ("amp", "legacy_amp", "mono", "stereo", "output"):
        setattr(options, key, getattr(options, key).resolve())
    run(options)
