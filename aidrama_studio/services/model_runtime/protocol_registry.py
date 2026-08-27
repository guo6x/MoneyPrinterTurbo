"""Small fail-closed registry for protocol drivers.

The registry deliberately indexes only protocol families.  Capability/model
selection remains the resolver's responsibility and provider wire fields stay
inside codecs.
"""

from __future__ import annotations

from collections.abc import Mapping
from .contracts import ProtocolFamily
from .drivers import Driver, DriverError, ProtocolDriver


class UnsupportedProtocolError(DriverError):
    """Raised when no driver is registered for a protocol family."""


class ProtocolDriverRegistry:
    def __init__(self, drivers: Mapping[ProtocolFamily | str, ProtocolDriver] | None = None) -> None:
        self._drivers: dict[ProtocolFamily, ProtocolDriver] = {}
        for family, driver in (drivers or {}).items():
            self.register(family, driver)

    @staticmethod
    def _family(value: ProtocolFamily | str) -> ProtocolFamily:
        try:
            return ProtocolFamily.coerce(value)
        except Exception as exc:
            raise UnsupportedProtocolError(f"unsupported protocol family: {value!r}") from exc

    def register(self, family: ProtocolFamily | str, driver: ProtocolDriver) -> ProtocolDriver:
        normalized = self._family(family)
        if driver is None or not hasattr(driver, "family"):
            raise DriverError("protocol driver must expose family")
        declared = self._family(getattr(driver, "family"))
        if declared is not normalized:
            raise DriverError(
                f"driver family {declared.value} does not match registration {normalized.value}"
            )
        self._drivers[normalized] = driver
        return driver

    add = register

    def get(self, family: ProtocolFamily | str) -> ProtocolDriver:
        normalized = self._family(family)
        try:
            return self._drivers[normalized]
        except KeyError as exc:
            raise UnsupportedProtocolError(
                f"no protocol driver registered for {normalized.value}"
            ) from exc

    resolve = get
    driver_for = get

    def list(self) -> tuple[ProtocolDriver, ...]:
        return tuple(self._drivers.values())

    def __contains__(self, family: object) -> bool:
        try:
            return self._family(family) in self._drivers  # type: ignore[arg-type]
        except UnsupportedProtocolError:
            return False


DriverRegistry = ProtocolDriverRegistry


__all__ = [
    "Driver",
    "DriverRegistry",
    "ProtocolDriver",
    "ProtocolDriverRegistry",
    "UnsupportedProtocolError",
]
