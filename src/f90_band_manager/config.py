from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigurationError

DEFAULT_BANDS = (1, 3, 7, 8, 20, 28, 38, 40, 41)

# ZX297520V3/ZLTEBAND byte layout.  The ninth value is accepted by the newer
# F90 builds; older builds accept the same values with the trailing zeroes
# omitted.  F90_BAND_MASKS can override this if a particular firmware differs.
DEFAULT_BAND_MASKS = {
    1: "1,0,0,0,0,0,0,0,0",
    3: "4,0,0,0,0,0,0,0,0",
    7: "64,0,0,0,0,0,0,0,0",
    8: "128,0,0,0,0,0,0,0,0",
    20: "0,0,8,0,0,0,0,0,0",
    28: "0,0,0,8,0,0,0,0,0",
    38: "0,0,0,0,32,0,0,0,0",
    40: "0,0,0,0,128,0,0,0,0",
    41: "0,0,0,0,0,1,0,0,0",
}

DEFAULT_STATUS_COMMANDS = (
    "network_type",
    "sub_network_type",
    "ppp_status",
    "modem_main_state",
    "signalbar",
    "rssi",
    "lte_band",
    "wan_active_band",
    "lte_rsrp",
    "lte_rsrq",
    "lte_snr",
    "lte_pci",
    "nv_pci",
    "nv_arfcn",
    "cell_id",
    "lte_cell_id",
    "lte_enodebid",
)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"{path}:{number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ConfigurationError(f"{path}:{number}: invalid environment key")
        raw_value = raw_value.strip()
        if raw_value[:1] in {"'", '"'} and raw_value[-1:] == raw_value[:1]:
            try:
                parsed = shlex.split(raw_value, comments=False, posix=True)
            except ValueError as exc:
                raise ConfigurationError(f"{path}:{number}: invalid quoted value") from exc
            value = " ".join(parsed) if parsed else ""
        else:
            value = raw_value.split(" #", 1)[0].rstrip() if " #" in raw_value else raw_value
        values[key] = value
    return values


def _env(name: str, file_values: dict[str, str], default: str = "") -> str:
    return os.environ.get(name, file_values.get(name, default))


def _parse_int(name: str, value: str, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return parsed


def _parse_float(name: str, value: str, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return parsed


def _parse_list(name: str, value: str) -> tuple[int, ...]:
    if not value.strip():
        raise ConfigurationError(f"{name} cannot be empty")
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        result.append(_parse_int(name, item, minimum=1))
    if not result:
        raise ConfigurationError(f"{name} cannot be empty")
    if len(set(result)) != len(result):
        raise ConfigurationError(f"{name} contains duplicates")
    return tuple(result)


def _parse_commands(value: str) -> tuple[str, ...]:
    commands = tuple(item.strip() for item in value.split(",") if item.strip())
    if not commands:
        raise ConfigurationError("F90_STATUS_COMMANDS cannot be empty")
    return commands


def _parse_masks(value: str) -> dict[int, str]:
    if not value.strip():
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("F90_BAND_MASKS must be a JSON object") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("F90_BAND_MASKS must be a JSON object")
    masks: dict[int, str] = {}
    for key, mask in raw.items():
        band = _parse_int("F90_BAND_MASKS band", str(key), minimum=1)
        if not isinstance(mask, str) or not mask.strip():
            raise ConfigurationError(f"F90_BAND_MASKS[{band}] must be a non-empty string")
        parts = mask.split(",")
        if not 1 <= len(parts) <= 9:
            raise ConfigurationError(f"F90_BAND_MASKS[{band}] must contain 1..9 bytes")
        for part in parts:
            number = _parse_int("F90_BAND_MASKS byte", part.strip(), minimum=0)
            if number > 255:
                raise ConfigurationError("F90_BAND_MASKS bytes must be <= 255")
        masks[band] = ",".join(part.strip() for part in parts)
    return masks


def _validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("F90_BASE_URL must be an http(s) URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    base_url: str = "http://192.168.100.1"
    username: str = "admin"
    password: str = ""
    password_file: Path | None = None
    auth_mode: str = "auto"
    login_include_username: bool = False
    legacy_password_mode: str = "base64"
    csrf_mode: str = "auto"
    write_mode: str = "body"
    bands: tuple[int, ...] = DEFAULT_BANDS
    fast_scan_bands: tuple[int, ...] = (7, 1, 20)
    band_masks: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_BAND_MASKS))
    status_commands: tuple[str, ...] = DEFAULT_STATUS_COMMANDS
    http_timeout: float = 10.0
    web_ready_timeout: float = 120.0
    lte_timeout: float = 120.0
    poll_interval: float = 5.0
    stabilize_seconds: float = 20.0
    speedtest_backend: str = "cloudflare"
    speedtest_binary: str = "speedtest"
    speedtest_server_id: str = ""
    speedtest_tests: int = 3
    speedtest_timeout: float = 180.0
    speedtest_request_timeout: int = 30
    olax_interface: str = ""
    route_probe_ip: str = "1.1.1.1"
    warmup_url: str = ""
    probe_url: str = ""
    probe_bytes: int = 262144
    probe_timeout: float = 20.0
    ping_host: str = "1.1.1.1"
    ping_count: int = 5
    ping_timeout: float = 3.0
    health_bad_checks_required: int = 2
    health_max_ping_ms: float = 200.0
    health_max_loss_pct: float = 10.0
    health_min_download_ratio: float = 0.30
    results_dir: Path = Path("results")
    state_dir: Path = Path("state")

    def band_mask(self, band: int) -> str:
        try:
            return self.band_masks[band]
        except KeyError as exc:
            raise ConfigurationError(f"no ZLTEBAND mask configured for B{band}") from exc

    def require_password(self) -> str:
        if self.password:
            return self.password
        if self.password_file:
            try:
                password = self.password_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigurationError(f"cannot read F90_PASSWORD_FILE: {exc}") from exc
            if password:
                return password
        raise ConfigurationError(
            "F90_PASSWORD is not configured; put it in a 600-mode .env or use F90_PASSWORD_FILE"
        )

    def validate(self) -> None:
        if self.speedtest_backend not in {"cloudflare", "speedtest"}:
            raise ConfigurationError("F90_SPEEDTEST_BACKEND must be cloudflare or speedtest")
        if self.auth_mode not in {"auto", "legacy", "nonce"}:
            raise ConfigurationError("F90_AUTH_MODE must be auto, legacy, or nonce")
        if self.legacy_password_mode not in {"base64", "sha256_base64_hex"}:
            raise ConfigurationError(
                "F90_LEGACY_PASSWORD_MODE must be base64 or sha256_base64_hex"
            )
        if self.csrf_mode not in {"auto", "always", "never"}:
            raise ConfigurationError("F90_CSRF_MODE must be auto, always, or never")
        if self.write_mode not in {"body", "query", "both"}:
            raise ConfigurationError("F90_WRITE_MODE must be body, query, or both")
        if not set(self.fast_scan_bands).issubset(set(self.bands)):
            raise ConfigurationError("F90_FAST_SCAN_BANDS must be a subset of F90_BANDS")
        for band in self.bands:
            self.band_mask(band)
        for name in ("olax_interface",):
            value = getattr(self, name)
            if value and (len(value) > 15 or not all(c.isalnum() or c in "_.-" for c in value)):
                raise ConfigurationError(f"F90_{name.upper()} is not a valid interface name")
        for name in ("route_probe_ip",):
            value = getattr(self, name)
            if value:
                try:
                    IPv4Address(value)
                except ValueError as exc:
                    raise ConfigurationError(f"F90_{name.upper()} must be an IPv4 address") from exc

    def require_direct_path(self) -> None:
        if not self.olax_interface:
            raise ConfigurationError("Direct OLAX benchmark requires F90_OLAX_INTERFACE")


def load_config(project_root: Path, env_file: Path | None = None) -> Config:
    project_root = project_root.resolve()
    selected_env = env_file
    if selected_env is None:
        configured = os.environ.get("F90_ENV_FILE")
        selected_env = Path(configured).expanduser() if configured else project_root / ".env"
    file_values = _read_env_file(selected_env)

    bands = _parse_list("F90_BANDS", _env("F90_BANDS", file_values, ",".join(map(str, DEFAULT_BANDS))))
    fast = _parse_list("F90_FAST_SCAN_BANDS", _env("F90_FAST_SCAN_BANDS", file_values, "7,1,20"))
    masks = dict(DEFAULT_BAND_MASKS)
    masks.update(_parse_masks(_env("F90_BAND_MASKS", file_values)))

    def path_value(name: str, default: Path) -> Path:
        raw = _env(name, file_values)
        if not raw:
            return default
        path = Path(raw).expanduser()
        return path if path.is_absolute() else project_root / path

    config = Config(
        base_url=_validate_base_url(_env("F90_BASE_URL", file_values, "http://192.168.100.1")),
        username=_env("F90_USERNAME", file_values, "admin"),
        password=_env("F90_PASSWORD", file_values),
        password_file=(
            Path(_env("F90_PASSWORD_FILE", file_values)).expanduser()
            if _env("F90_PASSWORD_FILE", file_values)
            else None
        ),
        auth_mode=_env("F90_AUTH_MODE", file_values, "auto").lower(),
        login_include_username=_env("F90_LOGIN_INCLUDE_USERNAME", file_values, "0").lower()
        in {"1", "true", "yes", "on"},
        legacy_password_mode=_env("F90_LEGACY_PASSWORD_MODE", file_values, "base64").lower(),
        csrf_mode=_env("F90_CSRF_MODE", file_values, "auto").lower(),
        write_mode=_env("F90_WRITE_MODE", file_values, "body").lower(),
        bands=bands,
        fast_scan_bands=fast,
        band_masks=masks,
        status_commands=_parse_commands(
            _env("F90_STATUS_COMMANDS", file_values, ",".join(DEFAULT_STATUS_COMMANDS))
        ),
        http_timeout=_parse_float("F90_HTTP_TIMEOUT", _env("F90_HTTP_TIMEOUT", file_values, "10"), minimum=0.1),
        web_ready_timeout=_parse_float(
            "F90_WEB_READY_TIMEOUT", _env("F90_WEB_READY_TIMEOUT", file_values, "120"), minimum=1
        ),
        lte_timeout=_parse_float(
            "F90_LTE_TIMEOUT", _env("F90_LTE_TIMEOUT", file_values, "120"), minimum=1
        ),
        poll_interval=_parse_float(
            "F90_POLL_INTERVAL", _env("F90_POLL_INTERVAL", file_values, "5"), minimum=0.1
        ),
        stabilize_seconds=_parse_float(
            "F90_STABILIZE_SECONDS", _env("F90_STABILIZE_SECONDS", file_values, "20"), minimum=0
        ),
        speedtest_backend=_env("F90_SPEEDTEST_BACKEND", file_values, "cloudflare").lower(),
        speedtest_binary=_env("F90_SPEEDTEST_BINARY", file_values, "speedtest"),
        speedtest_server_id=_env("F90_SPEEDTEST_SERVER_ID", file_values),
        speedtest_tests=_parse_int(
            "F90_SPEEDTEST_TESTS", _env("F90_SPEEDTEST_TESTS", file_values, "3"), minimum=1
        ),
        speedtest_timeout=_parse_float(
            "F90_SPEEDTEST_TIMEOUT", _env("F90_SPEEDTEST_TIMEOUT", file_values, "180"), minimum=1
        ),
        speedtest_request_timeout=_parse_int(
            "F90_SPEEDTEST_REQUEST_TIMEOUT",
            _env("F90_SPEEDTEST_REQUEST_TIMEOUT", file_values, "30"), minimum=1
        ),
        olax_interface=_env("F90_OLAX_INTERFACE", file_values),
        route_probe_ip=_env("F90_ROUTE_PROBE_IP", file_values, "1.1.1.1"),
        warmup_url=_env("F90_WARMUP_URL", file_values),
        probe_url=_env("F90_PROBE_URL", file_values),
        probe_bytes=_parse_int(
            "F90_PROBE_BYTES", _env("F90_PROBE_BYTES", file_values, "262144"), minimum=1
        ),
        probe_timeout=_parse_float(
            "F90_PROBE_TIMEOUT", _env("F90_PROBE_TIMEOUT", file_values, "20"), minimum=0.1
        ),
        ping_host=_env("F90_PING_HOST", file_values, "1.1.1.1"),
        ping_count=_parse_int("F90_PING_COUNT", _env("F90_PING_COUNT", file_values, "5"), minimum=1),
        ping_timeout=_parse_float(
            "F90_PING_TIMEOUT", _env("F90_PING_TIMEOUT", file_values, "3"), minimum=0.1
        ),
        health_bad_checks_required=_parse_int(
            "F90_HEALTH_BAD_CHECKS_REQUIRED",
            _env("F90_HEALTH_BAD_CHECKS_REQUIRED", file_values, "2"),
            minimum=1,
        ),
        health_max_ping_ms=_parse_float(
            "F90_HEALTH_MAX_PING_MS", _env("F90_HEALTH_MAX_PING_MS", file_values, "200"), minimum=0
        ),
        health_max_loss_pct=_parse_float(
            "F90_HEALTH_MAX_LOSS_PCT", _env("F90_HEALTH_MAX_LOSS_PCT", file_values, "10"), minimum=0
        ),
        health_min_download_ratio=_parse_float(
            "F90_HEALTH_MIN_DOWNLOAD_RATIO",
            _env("F90_HEALTH_MIN_DOWNLOAD_RATIO", file_values, "0.30"),
            minimum=0,
        ),
        results_dir=path_value("F90_RESULTS_DIR", project_root / "results"),
        state_dir=path_value("F90_STATE_DIR", project_root / "state"),
    )
    config.validate()
    return config
