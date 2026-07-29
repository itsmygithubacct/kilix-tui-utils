from __future__ import annotations

from enum import Enum
import os
import re
from typing import Mapping


class TemperatureUnit(Enum):
    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"

    @property
    def symbol(self) -> str:
        return "°C" if self is TemperatureUnit.CELSIUS else "°F"

    def absolute(self, celsius: float) -> float:
        if self is TemperatureUnit.FAHRENHEIT:
            return celsius * 9.0 / 5.0 + 32.0
        return celsius

    def delta(self, celsius: float) -> float:
        if self is TemperatureUnit.FAHRENHEIT:
            return celsius * 9.0 / 5.0
        return celsius

    def toggled(self) -> "TemperatureUnit":
        if self is TemperatureUnit.FAHRENHEIT:
            return TemperatureUnit.CELSIUS
        return TemperatureUnit.FAHRENHEIT


# These territories conventionally report everyday temperatures in Fahrenheit.
# A missing, generic, or malformed locale deliberately falls back to Fahrenheit.
_FAHRENHEIT_TERRITORIES = {
    "AS",
    "BS",
    "BZ",
    "FM",
    "GU",
    "KY",
    "LR",
    "MH",
    "MP",
    "PR",
    "PW",
    "US",
    "VI",
}
_TERRITORY_RE = re.compile(r"(?:_|-)([A-Za-z]{2}|\d{3})(?:[.@]|$)")


def unit_for_locale(locale_name: str | None) -> TemperatureUnit:
    if not locale_name:
        return TemperatureUnit.FAHRENHEIT
    name = locale_name.strip()
    if not name or name.upper() in {"C", "POSIX"} or name.upper().startswith("C."):
        return TemperatureUnit.FAHRENHEIT
    match = _TERRITORY_RE.search(name)
    if match is None:
        return TemperatureUnit.FAHRENHEIT
    territory = match.group(1).upper()
    if territory in _FAHRENHEIT_TERRITORIES:
        return TemperatureUnit.FAHRENHEIT
    return TemperatureUnit.CELSIUS


def locale_temperature_unit(
    environ: Mapping[str, str] | None = None,
) -> TemperatureUnit:
    values = os.environ if environ is None else environ
    for name in ("LC_ALL", "LC_MEASUREMENT", "LC_MESSAGES", "LANG"):
        value = values.get(name)
        if value:
            return unit_for_locale(value)
    return TemperatureUnit.FAHRENHEIT
