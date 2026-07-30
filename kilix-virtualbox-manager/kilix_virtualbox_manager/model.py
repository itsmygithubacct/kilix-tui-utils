"""The display model shared by the VirtualBox backend and the TUI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


STARTABLE_STATES = frozenset({
    "poweroff", "saved", "aborted", "aborted-saved", "teleported",
})
ACTIVE_STATES = frozenset({
    "running", "paused", "stuck", "starting", "stopping", "saving",
    "restoring", "teleporting", "live-snapshotting",
})
TUNNEL_PREFIXES = (
    "tun", "tap", "wg", "ppp", "tailscale", "zt", "vpn", "ipsec",
)


def state_label(value: str) -> str:
    """Turn VirtualBox's machine token into a compact human label."""
    labels = {
        "poweroff": "powered off",
        "aborted-saved": "aborted (saved)",
        "gurumeditation": "guru meditation",
        "live-snapshotting": "snapshotting",
    }
    return labels.get(value, value.replace("-", " ") or "unknown")


def parse_timestamp(value: str) -> datetime | None:
    """Parse VirtualBox's nanosecond ISO timestamps as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


@dataclass(frozen=True)
class GuestInterface:
    index: int
    name: str = ""
    status: str = ""
    ipv4: str = ""
    mac: str = ""

    @property
    def up(self) -> bool:
        return self.status.casefold() == "up"

    @property
    def is_tunnel(self) -> bool:
        name = self.name.casefold()
        return any(name.startswith(prefix) for prefix in TUNNEL_PREFIXES)


@dataclass(frozen=True)
class HostAdapter:
    index: int
    mode: str
    attachment: str = ""
    cable_connected: bool = False
    mac: str = ""

    @property
    def label(self) -> str:
        suffix = f" · {self.attachment}" if self.attachment else ""
        cable = "cable on" if self.cable_connected else "cable off"
        return f"{self.mode.upper()}{suffix} · {cable}"


@dataclass(frozen=True)
class PortForward:
    name: str
    protocol: str
    host_ip: str
    host_port: str
    guest_ip: str
    guest_port: str

    @property
    def label(self) -> str:
        host = f"{self.host_ip or '*'}:{self.host_port or '*'}"
        guest = f"{self.guest_ip or 'guest'}:{self.guest_port or '*'}"
        return f"{self.protocol.upper()} {host} -> {guest}"


@dataclass
class VirtualMachine:
    uuid: str
    name: str
    state: str = "unknown"
    os_type: str = ""
    cpus: int = 0
    memory_mb: int = 0
    vram_mb: int = 0
    state_changed: datetime | None = None
    session_name: str = ""
    config_file: str = ""
    guest_additions: str = ""
    host_adapters: tuple[HostAdapter, ...] = ()
    guest_interfaces: tuple[GuestInterface, ...] = ()
    port_forwards: tuple[PortForward, ...] = ()
    snapshots: tuple[str, ...] = ()
    current_snapshot: str = ""
    error: str = ""
    metadata: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def label_state(self) -> str:
        return state_label(self.state)

    @property
    def startable(self) -> bool:
        return not self.error and self.state in STARTABLE_STATES

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @property
    def tunnels(self) -> tuple[GuestInterface, ...]:
        return tuple(item for item in self.guest_interfaces if item.is_tunnel)

    @property
    def active_tunnels(self) -> tuple[GuestInterface, ...]:
        return tuple(item for item in self.tunnels if item.up)

    @property
    def guest_addresses(self) -> tuple[str, ...]:
        return tuple(
            item.ipv4 for item in self.guest_interfaces
            if item.ipv4 and not item.is_tunnel
        )

    @property
    def tunnel_status(self) -> str:
        if not self.active:
            return "offline"
        if self.active_tunnels:
            address = next(
                (item.ipv4 for item in self.active_tunnels if item.ipv4), "")
            return f"up {address}".rstrip()
        if self.tunnels:
            return "down"
        if not self.guest_additions:
            return "unknown"
        return "no tunnel"

    def age(self, now: datetime | None = None) -> str:
        if self.state_changed is None:
            return ""
        current = now or datetime.now(timezone.utc)
        return human_duration((current - self.state_changed).total_seconds())

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state_changed"] = (
            self.state_changed.isoformat() if self.state_changed else None)
        payload["state_label"] = self.label_state
        payload["tunnel_status"] = self.tunnel_status
        payload["startable"] = self.startable
        payload["active"] = self.active
        payload.pop("metadata", None)
        return payload
