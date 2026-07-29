from __future__ import annotations

import unittest

from kilix_temps.units import (
    TemperatureUnit,
    locale_temperature_unit,
    unit_for_locale,
)


class TemperatureUnitTests(unittest.TestCase):
    def test_absolute_and_delta_conversions_are_distinct(self) -> None:
        unit = TemperatureUnit.FAHRENHEIT
        self.assertAlmostEqual(unit.absolute(0.0), 32.0)
        self.assertAlmostEqual(unit.absolute(100.0), 212.0)
        self.assertAlmostEqual(unit.delta(10.0), 18.0)
        self.assertIs(unit.toggled(), TemperatureUnit.CELSIUS)

    def test_unknown_or_generic_locale_defaults_to_fahrenheit(self) -> None:
        for locale_name in (None, "", "C", "C.UTF-8", "POSIX", "en"):
            with self.subTest(locale_name=locale_name):
                self.assertIs(
                    unit_for_locale(locale_name),
                    TemperatureUnit.FAHRENHEIT,
                )

    def test_territory_qualified_locale_selects_expected_unit(self) -> None:
        expected = {
            "en_US.UTF-8": TemperatureUnit.FAHRENHEIT,
            "es_PR.UTF-8": TemperatureUnit.FAHRENHEIT,
            "en_GB.UTF-8": TemperatureUnit.CELSIUS,
            "de-DE.UTF-8": TemperatureUnit.CELSIUS,
        }
        for locale_name, unit in expected.items():
            with self.subTest(locale_name=locale_name):
                self.assertIs(unit_for_locale(locale_name), unit)

    def test_locale_category_precedence_and_empty_environment(self) -> None:
        self.assertIs(
            locale_temperature_unit({}),
            TemperatureUnit.FAHRENHEIT,
        )
        self.assertIs(
            locale_temperature_unit(
                {
                    "LC_ALL": "en_US.UTF-8",
                    "LC_MEASUREMENT": "en_GB.UTF-8",
                }
            ),
            TemperatureUnit.FAHRENHEIT,
        )
        self.assertIs(
            locale_temperature_unit(
                {
                    "LC_MEASUREMENT": "en_GB.UTF-8",
                    "LANG": "en_US.UTF-8",
                }
            ),
            TemperatureUnit.CELSIUS,
        )


if __name__ == "__main__":
    unittest.main()
