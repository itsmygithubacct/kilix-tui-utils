"""A minimal QMP client: one unix socket, stdlib socket and JSON only.

Each call opens the socket, reads QEMU's greeting, negotiates capabilities,
runs one command and closes. Asynchronous events that arrive in between are
skipped. Nothing here keeps a connection open, so a manager that quits leaves
no state behind in QEMU.
"""
from __future__ import annotations

import json
import socket
from typing import Any

TIMEOUT = 2.0


class QmpError(RuntimeError):
    """The socket is absent, refused, timed out, or QEMU returned an error."""


class _Lines:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""

    def read(self) -> dict[str, Any] | None:
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk
            if len(self.buffer) > 4 * 1024 * 1024:
                raise QmpError("QMP reply is too large")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise QmpError(f"QMP sent malformed JSON: {error}") from error
        if not isinstance(value, dict):
            raise QmpError("QMP sent a non-object reply")
        return value

    def reply(self) -> dict[str, Any] | None:
        while True:
            value = self.read()
            if value is None or "event" not in value:
                return value


def _send(sock: socket.socket, command: str,
          arguments: dict[str, Any] | None = None) -> None:
    message: dict[str, Any] = {"execute": command}
    if arguments:
        message["arguments"] = arguments
    sock.sendall(json.dumps(message).encode("utf-8") + b"\n")


def execute(
    path: str,
    command: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = TIMEOUT,
    allow_hangup: bool = False,
) -> Any:
    """Run one QMP command and return its ``return`` value.

    `allow_hangup` is for ``quit``: QEMU may close the socket before it has
    written the reply, which is success, not an error.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(path)
        except FileNotFoundError as error:
            raise QmpError("no QMP socket") from error
        except OSError as error:
            raise QmpError(f"QMP socket refused: {error.strerror or error}") \
                from error
        lines = _Lines(sock)
        greeting = lines.read()
        if not greeting or "QMP" not in greeting:
            raise QmpError("the socket did not greet as QMP")
        _send(sock, "qmp_capabilities")
        answer = lines.reply()
        if not answer or "return" not in answer:
            raise QmpError("QMP refused capabilities negotiation")
        _send(sock, command, arguments)
        try:
            answer = lines.reply()
        except (ConnectionResetError, BrokenPipeError):
            answer = None
        if answer is None:
            if allow_hangup:
                return {}
            raise QmpError(f"QEMU closed the socket during {command}")
        if "error" in answer:
            error = answer["error"]
            detail = error.get("desc") if isinstance(error, dict) else error
            raise QmpError(f"{command}: {detail}")
        return answer.get("return")
    except socket.timeout as error:
        raise QmpError(f"QMP did not answer within {timeout:g}s") from error
    except OSError as error:
        raise QmpError(str(error)) from error
    finally:
        sock.close()


def status(path: str, *, timeout: float = TIMEOUT) -> str:
    """``query-status``'s status word: running, paused, shutdown, …"""
    value = execute(path, "query-status", timeout=timeout)
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise QmpError("query-status returned no status")
    return value["status"]
