from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
import re
import stat
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


def preferred_temperature_unit(environ: Mapping[str, str] | None = None) -> TemperatureUnit:
    """Read the shared display preference; a new installation defaults to °F.

    A shared file takes precedence over inherited settings. CLI unit switches
    and the dashboard's `u` key can still override this for one invocation.
    """
    values = os.environ if environ is None else environ
    key = 'KILIX_TEMPERATURE_UNIT'
    root = values.get('GPU_TERMINAL_HOME') or str(Path.home() / '.local/gpu_terminal')
    path = Path(values.get('GPU_TERMINAL_SETTINGS_FILE') or str(Path(root) / 'settings.conf')).expanduser()
    value = 'fahrenheit'
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return TemperatureUnit.FAHRENHEIT
            data = stream.read(1024 * 1024 + 1)
        if len(data) <= 1024 * 1024:
            text = data.decode('utf-8', errors='replace')
            for line in text.splitlines():
                name, separator, setting = line.partition('=')
                if separator and name.strip() == key:
                    value = setting.strip().lower()
    except FileNotFoundError:
        value = values.get(key, 'fahrenheit').strip().lower()
    except OSError:
        pass
    return TemperatureUnit.CELSIUS if value == 'celsius' else TemperatureUnit.FAHRENHEIT
