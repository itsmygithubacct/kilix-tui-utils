"""Bounded local Amp client: explicit negotiation and verified Unix peers."""
from contextlib import contextmanager
import json
import math
import os
import socket
import stat
import struct
import threading
import time

PROTOCOL_VERSION = 2
MAX_REPLY = 1024 * 1024
CONTROL_TIMEOUT = 5.0


@contextmanager
def control_parent(path: str, *, create: bool = False):
    """Pin a private parent without following links or modifying old modes."""
    if not isinstance(path, str) or not path.startswith("/") or len(os.fsencode(path)) >= 108:
        raise ValueError("control socket needs a bounded absolute path")
    parts = path[1:].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("control socket path has an unsafe component")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in parts[:-1]:
            parent = os.fstat(fd)
            if parent.st_uid not in {0, os.geteuid()} or (parent.st_mode & 0o022
                    and not (parent.st_uid == 0 and parent.st_mode & stat.S_ISVTX)):
                raise ValueError("control socket has an unsafe ancestor")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        parent = os.fstat(fd)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise ValueError("control socket needs a private owned parent directory")
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _endpoint(fd, leaf):
    info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("control endpoint must be an owned mode-0600 socket")
    return info.st_dev, info.st_ino


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result or not key.isascii() or not 1 <= len(key) <= 63:
            raise ValueError("duplicate or invalid reply key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite reply number")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_reply(data: bytes) -> dict:
    if len(data) > MAX_REPLY:
        raise ValueError("control reply is too large")
    reply = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                       parse_constant=_reject_constant)
    if type(reply) is not dict or type(reply.get("protocol")) is not int or type(reply.get("ok")) is not bool:
        raise ValueError("reply needs typed protocol and ok fields")
    stack = [(reply, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 16 or nodes > 32768:
            raise ValueError("reply structure is too large")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
        elif isinstance(value, str):
            if "\0" in value or len(value) > 8192 or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise ValueError("invalid reply string")
        elif type(value) in (int, float) and not _finite(value):
            raise ValueError("non-finite reply number")
    if not reply["ok"] and not isinstance(reply.get("error"), str):
        raise ValueError("error reply needs a message")
    if reply["protocol"] == 2 and not reply["ok"] and (
            not isinstance(reply.get("error_code"), str) or not reply["error_code"]
            or type(reply.get("error_recoverable")) is not bool):
        raise ValueError("version-2 error needs structured fields")
    for key in ("title", "file", "error", "error_code", "source_error_message"):
        if key in reply and not isinstance(reply[key], str):
            raise ValueError("wrongly typed reply text")
    for key in ("shuffle", "live", "ready", "seekable", "model_ready", "buffering", "ended", "degraded",
                "wire_valid", "reconnect_required", "source_error_recoverable", "error_recoverable", "encodec", "live_sources"):
        if key in reply and type(reply[key]) is not bool:
            raise ValueError("wrongly typed reply boolean")
    for key, low, high in (("index", -1, 2147483647), ("count", 0, 2147483647), ("volume", 0, 100),
                           ("repeat", 0, 2), ("profile", 0, 2), ("sample_rate", 0, 384000),
                           ("channels", 0, 64), ("bitrate", 0, 2147483647), ("threads", 0, 2),
                           ("source_error_code", 0, 9), ("max_protocol", 1, 2)):
        if key in reply and (type(reply[key]) is not int or not low <= reply[key] <= high):
            raise ValueError("invalid integer reply field")
    for key in ("pos", "len"):
        if key in reply and not (key == "len" and reply["protocol"] == 2 and reply[key] is None):
            if not _finite(reply[key]) or not 0 <= reply[key] <= 2147483.647:
                raise ValueError("invalid source time")
    if "state" in reply and (type(reply["state"]) is not str or reply["state"] not in {"playing", "paused", "stopped", "loading", "buffering"}):
        raise ValueError("invalid playback state")
    if "items" in reply and (type(reply["items"]) is not list or any(type(p) is not str for p in reply["items"])):
        raise ValueError("invalid playlist population")
    if reply["protocol"] == 2 and "state" in reply:
        required = {"source_type", "codec", "profile", "sample_rate", "channels", "bitrate", "threads",
                    "ready", "model_ready", "live", "buffering", "seekable", "ended", "degraded", "reconnect_required",
                    "wire_valid", "wire_pts_ms", "wire_epoch", "source_error_code", "source_error_message",
                    "source_error_recoverable", "pos", "len"}
        if not required <= reply.keys():
            raise ValueError("incomplete version-2 source state")
        if type(reply["source_type"]) is not str or type(reply["codec"]) is not str or reply["source_type"] not in {"none", "file", "encodec-stdin", "encodec-unix"} or reply["codec"] not in {"none", "pcm", "encodec"}:
            raise ValueError("unknown source type or codec")
        if reply["live"] and (reply["len"] is not None or reply["seekable"]):
            raise ValueError("live source claimed a finite duration or seek")
        for key in ("wire_pts_ms", "wire_epoch"):
            value = reply[key]
            if reply["wire_valid"]:
                if type(value) is not str or not value.isascii() or not value.isdecimal() or len(value) > 20:
                    raise ValueError("invalid exact wire integer")
                if str(int(value)) != value or int(value) > 18446744073709551615:
                    raise ValueError("noncanonical wire integer")
            elif value is not None:
                raise ValueError("unverified wire timestamp")
    return reply


class MusicControl:
    def __init__(self, path: str):
        self.path = path
        self.error = ""
        self.version = None
        self.identity = None
        self.capabilities = {}
        self._closed = threading.Event()
        self._connections = set()
        self._connection_lock = threading.Lock()

    def close(self):
        """Interrupt only this client's in-flight requests; never stop its peer."""
        self._closed.set()
        with self._connection_lock:
            for client in self._connections:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @contextmanager
    def _connection(self, closing):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            with self._connection_lock:
                if self._closed.is_set() and not closing:
                    raise ValueError("control client is closed")
                self._connections.add(client)
            try:
                yield client
            finally:
                with self._connection_lock:
                    self._connections.discard(client)

    def available(self) -> bool:
        try:
            with control_parent(self.path) as (fd, leaf):
                _endpoint(fd, leaf)
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as failure:
            self.error = str(failure)
            return False

    def _exchange(self, name, version, fields=None, *, expected=None, timeout=CONTROL_TIMEOUT, closing=False):
        if not _finite(timeout) or not 0 < timeout <= CONTROL_TIMEOUT:
            raise ValueError("invalid control deadline")
        payload = json.dumps({**(fields or {}), "cmd": name, "protocol": version},
                             allow_nan=False, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) > 8192:
            raise ValueError("control request is too large")
        deadline = time.monotonic() + timeout
        with control_parent(self.path) as (fd, leaf), self._connection(closing) as client:
            before = _endpoint(fd, leaf)
            client.settimeout(max(.001, deadline - time.monotonic()))
            client.connect(f"/proc/self/fd/{fd}/{leaf}")
            if self._closed.is_set() and not closing:
                raise ValueError("control client is closed")
            pid, uid, _gid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            identity = (*before, pid)
            if uid != os.geteuid() or _endpoint(fd, leaf) != before or (expected is not None and expected != identity):
                raise ValueError("control peer changed; reconnect before another command")
            client.settimeout(max(.001, deadline - time.monotonic()))
            client.sendall(payload)
            data = bytearray()
            while b"\n" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("control reply deadline expired")
                client.settimeout(remaining)
                chunk = client.recv(min(65536, MAX_REPLY + 1 - len(data)))
                if not chunk:
                    raise ValueError("truncated reply from kilix-amp")
                data.extend(chunk)
                if len(data) > MAX_REPLY:
                    raise ValueError("control reply is too large")
            line, suffix = bytes(data).split(b"\n", 1)
            if suffix.strip():
                raise ValueError("unexpected trailing reply data")
            try:
                reply = validate_reply(line)
            except (ValueError, TypeError, OverflowError, RecursionError) as failure:
                raise ValueError("malformed reply from kilix-amp") from failure
            return reply, identity

    def negotiate(self, *, timeout=CONTROL_TIMEOUT) -> bool:
        self.version = None
        self.identity = None
        self.capabilities = {}
        try:
            deadline = time.monotonic() + timeout
            reply, identity = self._exchange("ping", 2, timeout=timeout)
            self.version = reply["protocol"]
            if self.version == 1:
                reply, identity = self._exchange("ping", 1, expected=identity,
                                                 timeout=deadline - time.monotonic())
            if reply["protocol"] not in {1, 2} or not reply["ok"]:
                raise ValueError(f"unsupported kilix-amp protocol {reply['protocol']}")
            if self.version == 1 and reply["protocol"] != 1:
                raise ValueError("protocol changed during negotiation")
            if reply["protocol"] == 2:
                if reply.get("max_protocol") != 2 or type(reply.get("encodec")) is not bool or type(reply.get("live_sources")) is not bool:
                    raise ValueError("incomplete version-2 capability reply")
                self.capabilities = {key: reply[key] for key in ("encodec", "live_sources")}
            self.identity = identity
            self.version = reply["protocol"]
            self.error = ""
            return True
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as failure:
            self.error = f"control socket: {failure}"
            return False

    def command(self, name, **fields):
        if self.identity is None and not self.negotiate():
            return {}
        try:
            if name == "open" and self.version != 2:
                raise ValueError("this source needs a protocol-2 Amp backend")
            reply, _identity = self._exchange(name, self.version, fields, expected=self.identity)
            if reply["protocol"] != self.version:
                raise ValueError(f"kilix-amp changed protocol to {reply['protocol']}")
            self.error = "" if reply["ok"] else reply["error"]
            return reply
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as failure:
            self.error = f"control socket: {failure}"
            self.identity = None
            return {}
