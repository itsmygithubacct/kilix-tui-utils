from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable

from .sensors import Sample, TemperatureSensor


class Level(IntEnum):
    NORMAL = 0
    WARM = 1
    HOT = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class ThresholdConfig:
    warning: float = 80.0
    hot: float = 90.0
    critical: float = 100.0

    def validate(self) -> None:
        if not (1.0 < self.warning < self.hot < self.critical <= 200.0):
            raise ValueError("thresholds must satisfy 1 < warning < hot < critical <= 200")


@dataclass(frozen=True, slots=True)
class SensorPolicy:
    warning: float
    hot: float
    critical: float
    hardware_critical: float | None


def policy_for(sensor: TemperatureSensor, config: ThresholdConfig) -> SensorPolicy:
    """Combine conservative defaults with limits exported by the driver."""

    hardware_critical = sensor.critical_hint
    critical = config.critical
    if hardware_critical is not None and 30.0 <= hardware_critical <= 200.0:
        critical = min(critical, hardware_critical)

    hot = min(config.hot, critical - 3.0)
    warning_candidates = [config.warning, hot - 3.0]
    if sensor.warning_hint is not None and 20.0 <= sensor.warning_hint < critical:
        warning_candidates.append(sensor.warning_hint)
    warning = min(warning_candidates)

    # Defensive fallbacks for unusual firmware limits.
    warning = max(1.0, warning)
    hot = max(warning + 1.0, hot)
    critical = max(hot + 1.0, critical)
    return SensorPolicy(
        warning=warning,
        hot=hot,
        critical=critical,
        hardware_critical=hardware_critical,
    )


def level_for(value: float, policy: SensorPolicy) -> Level:
    if value >= policy.critical:
        return Level.CRITICAL
    if value >= policy.hot:
        return Level.HOT
    if value >= policy.warning:
        return Level.WARM
    return Level.NORMAL


@dataclass(frozen=True, slots=True)
class Alert:
    monotonic: float
    sensor_key: str
    sensor_name: str
    level: Level
    value: float


@dataclass(slots=True)
class SensorState:
    sensor: TemperatureSensor
    policy: SensorPolicy
    history: deque[tuple[float, float]]
    current: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    level: Level = Level.NORMAL
    last_seen: float = 0.0

    @property
    def headroom(self) -> float | None:
        if self.current is None:
            return None
        return self.policy.critical - self.current

    @property
    def trend_per_minute(self) -> float | None:
        if len(self.history) < 2:
            return None
        last_time, last_value = self.history[-1]
        recent = [point for point in self.history if point[0] >= last_time - 30.0]
        first_time, first_value = recent[0] if len(recent) >= 2 else self.history[0]
        elapsed = last_time - first_time
        if elapsed < 2.0:
            return None
        return (last_value - first_value) * 60.0 / elapsed

    @property
    def values(self) -> list[float]:
        return [value for _, value in self.history]

    def update(self, monotonic: float, value: float) -> Level:
        previous = self.level
        self.current = value
        self.last_seen = monotonic
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        target = level_for(value, self.policy)
        if target < previous:
            boundary = {
                Level.CRITICAL: self.policy.critical,
                Level.HOT: self.policy.hot,
                Level.WARM: self.policy.warning,
            }.get(previous)
            if boundary is not None and value >= boundary - 1.5:
                target = previous
        self.level = target
        self.history.append((monotonic, value))
        return previous


class ThermalModel:
    def __init__(self, thresholds: ThresholdConfig, history_size: int = 180) -> None:
        thresholds.validate()
        if history_size < 2:
            raise ValueError("history_size must be at least 2")
        self.thresholds = thresholds
        self.history_size = history_size
        self.states: dict[str, SensorState] = {}
        self.alerts: deque[Alert] = deque(maxlen=8)

    def update(
        self,
        sample: Sample,
        sensors: Iterable[TemperatureSensor],
    ) -> list[Alert]:
        specs = {sensor.key: sensor for sensor in sensors}
        for state in self.states.values():
            state.current = None

        raised: list[Alert] = []
        for key, value in sample.temperatures.items():
            sensor = specs.get(key)
            if sensor is None:
                continue
            state = self.states.get(key)
            policy = policy_for(sensor, self.thresholds)
            if state is None:
                state = SensorState(
                    sensor=sensor,
                    policy=policy,
                    history=deque(maxlen=self.history_size),
                )
                self.states[key] = state
            else:
                state.sensor = sensor
                state.policy = policy
            previous = state.update(sample.monotonic, value)
            if state.level >= Level.HOT and state.level > previous:
                alert = Alert(
                    monotonic=sample.monotonic,
                    sensor_key=key,
                    sensor_name=sensor.display_name,
                    level=state.level,
                    value=value,
                )
                self.alerts.appendleft(alert)
                raised.append(alert)
        return raised

    @property
    def active_states(self) -> list[SensorState]:
        return [state for state in self.states.values() if state.current is not None]

    @property
    def overall_level(self) -> Level:
        return max((state.level for state in self.active_states), default=Level.NORMAL)

    @property
    def most_at_risk(self) -> SensorState | None:
        states = self.active_states
        if not states:
            return None
        return min(
            states,
            key=lambda state: (
                -(int(state.level)),
                state.headroom if state.headroom is not None else 999.0,
                -(state.current or 0.0),
            ),
        )

    def ordered_states(self, sort_mode: str = "risk") -> list[SensorState]:
        states = self.active_states
        if sort_mode == "source":
            source_order = {"thermal-zone": 0}
            return sorted(
                states,
                key=lambda state: (
                    source_order.get(state.sensor.source, 1),
                    state.sensor.chip.lower(),
                    state.sensor.label.lower(),
                ),
            )
        return sorted(
            states,
            key=lambda state: (
                -int(state.level),
                state.headroom if state.headroom is not None else 999.0,
                -(state.current or 0.0),
                state.sensor.display_name.lower(),
            ),
        )

    def reset_peaks(self) -> None:
        for state in self.active_states:
            value = state.current
            if value is None:
                continue
            state.minimum = value
            state.maximum = value
            if state.history:
                timestamp = state.history[-1][0]
                state.history.clear()
                state.history.append((timestamp, value))
