class F90Error(Exception):
    """Base exception for recoverable F90 manager errors."""


class ConfigurationError(F90Error):
    """The local configuration is missing or invalid."""


class ModemError(F90Error):
    """The modem rejected a request or returned an unusable response."""


class ModemHTTPError(ModemError):
    """The modem HTTP server could not serve a request."""


class LoginError(ModemError):
    """Authentication failed."""


class ModemTimeout(ModemError):
    """The modem did not become reachable/registered in time."""


class BenchmarkError(F90Error):
    """A speed or network benchmark could not be completed."""


class ScanBusy(F90Error):
    """Another scan already holds the lock."""

