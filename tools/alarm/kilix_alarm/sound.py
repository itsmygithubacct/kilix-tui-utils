"""Build a wav for one alarm cycle and hand it to a player.

The player reads wav bytes on stdin. Tests pass their own player. Nothing
here waits for the sound to finish, and a missing player leaves the flash
working.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import wave
from io import BytesIO

from .model import Tone, pattern

RATE = 22050


def frames(tones: tuple[Tone, ...]) -> bytes:
    chunks = bytearray()
    for tone in tones:
        count = max(0, int(RATE * tone.seconds))
        if tone.wave == "silence" or tone.amplitude <= 0 or tone.hz <= 0:
            chunks.extend(b"\x00\x00" * count)
            continue
        scale = max(0.0, min(1.0, tone.amplitude))
        for index in range(count):
            angle = 2 * math.pi * tone.hz * (index / RATE)
            sample = math.sin(angle)
            if tone.wave == "square":
                sample = 1.0 if sample >= 0 else -1.0
            value = int(max(-1.0, min(1.0, sample * scale)) * 32767)
            chunks.extend(struct.pack("<h", value))
    return bytes(chunks)


def wav_bytes(style: str) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames(pattern(style)))
    return buffer.getvalue()


def player_argv() -> list[str] | None:
    """A program that reads a wav on stdin, or None when sound is off."""
    override = os.environ.get("KILIX_ALARM_PLAYER")
    if override is not None:
        if override in {"", "none"}:
            return None
        return [override]
    paplay = shutil.which("paplay")
    if paplay:
        return [paplay]
    aplay = shutil.which("aplay")
    if aplay:
        return [aplay, "-q", "-t", "wav", "-"]
    return None


class Player:
    """Play one cycle without stacking a second copy on top of it."""

    def __init__(self, argv: list[str] | None | object = ...) -> None:
        self.argv = player_argv() if argv is ... else argv
        self.proc: subprocess.Popen[bytes] | None = None
        self.played = 0

    def play(self, payload: bytes) -> None:
        self.reap()
        if not self.argv or not payload:
            return
        if self.proc is not None and self.proc.poll() is None:
            return
        try:
            self.proc = subprocess.Popen(
                list(self.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert self.proc.stdin is not None
            self.proc.stdin.write(payload)
            self.proc.stdin.close()
            self.played += 1
        except OSError:
            self.proc = None

    def reap(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
