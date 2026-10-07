from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from http import cookiejar
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import Config
from .errors import (
    ConfigurationError,
    LoginError,
    ModemError,
    ModemHTTPError,
    ModemTimeout,
)

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes
    headers: object | None = None


@dataclass(frozen=True)
class ModemStatus:
    band: int | None
    band_raw: str
    network_type: str
    connected: bool
    rsrp: float | None
    rsrq: float | None
    sinr: float | None
    pci: str
    cell_id: str
    earfcn: str
    raw: dict

    def as_dict(self) -> dict:
        return {
            "band": self.band,
            "band_raw": self.band_raw,
            "network_type": self.network_type,
            "connected": self.connected,
            "rsrp": self.rsrp,
            "rsrq": self.rsrq,
            "sinr": self.sinr,
            "pci": self.pci,
            "cell_id": self.cell_id,
            "earfcn": self.earfcn,
        }


_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_BAND_RE = re.compile(r"(?:LTE\s*)?B(?:AND)?\s*[-:=]?\s*(\d{1,3})", re.IGNORECASE)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_band(value: object) -> int | None:
    text = _as_text(value)
    match = _BAND_RE.search(text)
    if match:
        return int(match.group(1))
    if re.fullmatch(r"\d{1,3}", text):
        return int(text)
    return None


def _first_value(data: dict, *names: str) -> object:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


def _is_connected(data: dict, network_type: str) -> bool:
    all_text = " ".join(
        _as_text(_first_value(data, key))
        for key in ("network_type", "sub_network_type", "ppp_status", "modem_main_state")
    ).lower()
    disconnected_markers = (
        "no service",
        "no_service",
        "disconnected",
        "disconnect",
        "offline",
        "limited service",
        "limited_service",
        "searching",
        "not registered",
    )
    if any(marker in all_text for marker in disconnected_markers):
        return False
    if "lte" in network_type.lower() or "4g" in network_type.lower():
        return True
    ppp = _as_text(_first_value(data, "ppp_status")).lower()
    return ppp in {"1", "up", "connected", "ppp_connected", "online"}


class F90Modem:
    """Small client for the F90's ZTE-style ``reqproc`` API.

    The firmware family has at least two login variants.  ``auto`` probes the
    nonce endpoint and otherwise uses the legacy base64-password login.  All
    band writes remain explicit and are limited to SET_FREQ_BAND.
    """

    def __init__(self, config: Config, opener=None, *, sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self._sleep = sleep
        self._clock = clock
        self._cookies = cookiejar.CookieJar()
        self._opener = opener or build_opener(
            # CookieProcessor is deliberately the only handler needed here.
            # The modem serves plain HTTP and does not need browser auth.
            HTTPCookieProcessor(self._cookies)
        )
        self.authenticated = False
        self._auth_mode: str | None = None

    @property
    def base_url(self) -> str:
        return self.config.base_url

    def _url(self, path: str, params: dict[str, object] | None = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> HTTPResult:
        url = self._url(path, params)
        encoded = urlencode(data).encode("utf-8") if data is not None else None
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "User-Agent": "olax-f90-band-manager/0.1",
            "Referer": f"{self.base_url}/index.html",
            "X-Requested-With": "XMLHttpRequest",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        request = Request(url, data=encoded, headers=headers, method=method.upper())
        request_timeout = timeout if timeout is not None else self.config.http_timeout
        try:
            response = self._opener.open(request, timeout=request_timeout)
            try:
                body = response.read()
                response_status = getattr(response, "status", None)
                status = int(response_status if response_status is not None else response.getcode())
                response_headers = getattr(response, "headers", None)
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except HTTPError as exc:
            try:
                body = exc.read()
            except OSError:
                body = b""
            raise ModemHTTPError(f"HTTP {exc.code} from {path}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ModemHTTPError(f"cannot reach modem at {url}: {exc}") from exc

        if status < 200 or status >= 400:
            raise ModemHTTPError(f"HTTP {status} from {path}")
        return HTTPResult(status=status, body=body, headers=response_headers)

    def _json(self, result: HTTPResult, path: str) -> dict:
        text = result.body.decode("utf-8", errors="replace").lstrip("\ufeff\x00 \t\r\n")
        if not text:
            raise ModemError(f"empty JSON response from {path}")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModemError(f"invalid JSON response from {path}") from exc
        if not isinstance(decoded, dict):
            raise ModemError(f"unexpected JSON response from {path}")
        return decoded

    def get_json(self, commands: tuple[str, ...] | list[str]) -> dict:
        commands = tuple(commands)
        params: dict[str, object] = {"isTest": "false", "cmd": ",".join(commands)}
        if len(commands) > 1:
            params["multi_data"] = "1"
        result = self._request("GET", "/reqproc/proc_get", params=params)
        return self._json(result, "/reqproc/proc_get")

    def _post_json(self, values: dict[str, object]) -> dict:
        values = {"isTest": "false", **values}
        if self.config.write_mode == "body":
            result = self._request("POST", "/reqproc/proc_post", data=values)
        elif self.config.write_mode == "query":
            result = self._request("POST", "/reqproc/proc_post", params=values)
        else:
            result = self._request("POST", "/reqproc/proc_post", params=values, data=values)
        return self._json(result, "/reqproc/proc_post")

    def get_token(self) -> str:
        response = self.get_json(("get_token",))
        return _as_text(response.get("token") or response.get("get_token"))

    @staticmethod
    def _base64(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    def _nonce(self) -> str | None:
        try:
            response = self.get_json(("get_random_login",))
        except ModemError:
            return None
        nonce = response.get("random_login") or response.get("random")
        return _as_text(nonce) or None

    @staticmethod
    def _login_result(response: dict) -> str:
        value = response.get("result")
        if value is None:
            value = response.get("success")
        return _as_text(value).lower()

    def _check_login(self, response: dict) -> None:
        result = self._login_result(response)
        # Firmware variants use 0, 3, or 4 for successful/previously active
        # sessions; the F90 family also emits the literal success string.
        if result not in {"0", "4", "success", "sucess", "true"}:
            raise LoginError(f"F90 login rejected (result={result or 'missing'})")

    def login(self, *, force: bool = True) -> None:
        if self.authenticated and not force:
            return
        password = self.config.require_password()
        configured_mode = self.config.auth_mode
        nonce = self._nonce() if configured_mode in {"auto", "nonce"} else None

        if configured_mode == "nonce" or nonce is not None:
            if configured_mode == "nonce" and nonce is None:
                raise LoginError("nonce login was requested but get_random_login returned no nonce")
            mode = "nonce"
            token = ""
            try:
                token = self.get_token()
            except ModemError:
                # An empty CSRF token is valid for the login request on this
                # family; a later write fetches it if the build supports it.
                LOG.debug("F90 did not expose get_token before login")
            digest = hashlib.sha256(f"{nonce or ''}{password}".encode()).hexdigest()
            values: dict[str, object] = {
                "goformId": "LOGIN",
                "username": self._base64(self.config.username),
                "password": self._base64(digest),
                "CSRFToken": token,
            }
        else:
            mode = "legacy"
            if self.config.legacy_password_mode == "sha256_base64_hex":
                encoded = self._base64(password)
                encoded_password = hashlib.sha256(encoded.encode("ascii")).hexdigest().upper()
            else:
                encoded_password = self._base64(password)
            values = {"goformId": "LOGIN", "password": encoded_password}
            if self.config.login_include_username:
                values["username"] = self.config.username

        response = self._post_json(values)
        self._check_login(response)
        self._auth_mode = mode
        self.authenticated = True
        LOG.debug("F90 login succeeded using %s authentication", mode)

    def _post_command(self, action: str, **values: object) -> dict:
        payload: dict[str, object] = {"goformId": action, **values}
        use_csrf = self.config.csrf_mode == "always" or (
            self.config.csrf_mode == "auto" and self._auth_mode == "nonce"
        )
        if use_csrf:
            try:
                payload["CSRFToken"] = self.get_token()
            except ModemError:
                if self.config.csrf_mode == "always":
                    raise
        response = self._post_json(payload)
        result = _as_text(response.get("result")).lower()
        if result in {"1", "2", "3", "failure", "fail", "false"}:
            self.authenticated = False
            raise ModemError(f"F90 command {action} rejected (result={result})")
        return response

    def get_status(self) -> ModemStatus:
        response = self.get_json(self.config.status_commands)
        return self._parse_status(response)

    @staticmethod
    def _parse_status(response: dict) -> ModemStatus:
        # Some modified pages wrap the same values in one object.  Keep the
        # original response but use the nested object when it is unambiguous.
        data = dict(response)
        nested = [value for value in response.values() if isinstance(value, dict)]
        if len(nested) == 1 and len(response) == 1:
            data = dict(nested[0])

        raw_band = _as_text(
            _first_value(data, "lte_band", "wan_active_band", "current_band", "band")
        )
        network_type = _as_text(
            _first_value(data, "network_type", "sub_network_type", "network_category")
        )
        return ModemStatus(
            band=parse_band(raw_band),
            band_raw=raw_band,
            network_type=network_type,
            connected=_is_connected(data, network_type),
            rsrp=_number(_first_value(data, "lte_rsrp", "rsrp")),
            rsrq=_number(_first_value(data, "lte_rsrq", "rsrq")),
            sinr=_number(_first_value(data, "lte_snr", "sinr", "lte_sinr")),
            pci=_as_text(_first_value(data, "lte_pci", "nv_pci", "pci")),
            cell_id=_as_text(
                _first_value(data, "cell_id", "lte_cell_id", "lte_enodebid", "enodeb_id")
            ),
            earfcn=_as_text(_first_value(data, "nv_arfcn", "earfcn", "lte_earfcn")),
            raw=response,
        )

    @staticmethod
    def band_matches(status: ModemStatus, band: int) -> bool:
        return status.band == band

    def current_band(self) -> int | None:
        return self.get_status().band

    def set_band(self, band: int) -> dict:
        mask = self.config.band_mask(band)
        parts = mask.split(",")
        if len(parts) == 9 and parts[-1] == "0":
            mask = ",".join(parts[:8])
        if not self.authenticated:
            self.login()
        settings = self.get_json(("work_lte_band", "ping_google"))
        ping_google = settings.get("ping_google")
        if ping_google not in {"yes", "no", ""}:
            raise ModemError("cannot read ping_google; refusing to change unrelated settings")
        response = self._json(self._request(
            "GET", "/reqproc/proc_post",
            params={"goformId": "SET_FREQ_BAND", "work_lte_band": mask,
                    "ping_google": ping_google},
        ), "/reqproc/proc_post")
        if response.get("result") != "success":
            raise ModemError(f"SET_FREQ_BAND rejected: {response.get('result', 'missing')}")
        stored = self.get_json(("work_lte_band",)).get("work_lte_band", "")
        if str(stored).strip() != mask:
            raise ModemError(f"band mask was not saved: expected {mask}, got {stored}")
        LOG.info("Saved B%s mask %s; rebooting F90 to apply it", band, mask)
        try:
            reboot = self._post_command("REBOOT_DEVICE")
            if reboot.get("result") not in {"success", "0"}:
                raise ModemError("reboot was not acknowledged")
        except ModemHTTPError as exc:
            if not isinstance(exc.__cause__, (RemoteDisconnected, ConnectionResetError)):
                raise
            LOG.info("HTTP closed during reboot; waiting for verified LTE recovery")
        self.authenticated = False
        self._sleep(15)
        return response

    def wait_until_online(self, timeout: float | None = None) -> None:
        timeout = self.config.web_ready_timeout if timeout is None else timeout
        deadline = self._clock() + timeout
        last_error: Exception | None = None
        while self._clock() < deadline:
            try:
                self._request("GET", "/", timeout=min(self.config.http_timeout, max(0.5, deadline - self._clock())))
                return
            except ModemError as exc:
                last_error = exc
                self._sleep(min(self.config.poll_interval, max(0.05, deadline - self._clock())))
        raise ModemTimeout(f"F90 web interface did not return within {timeout:.0f}s: {last_error}")

    def wait_for_lte(self, expected_band: int | None = None, timeout: float | None = None) -> ModemStatus:
        timeout = self.config.lte_timeout if timeout is None else timeout
        deadline = self._clock() + timeout
        last_error: Exception | None = None
        next_login = 0.0
        while self._clock() < deadline:
            now = self._clock()
            if not self.authenticated and now >= next_login:
                try:
                    self.login()
                except ConfigurationError:
                    raise
                except ModemError as exc:
                    last_error = exc
                    next_login = now + max(10.0, self.config.poll_interval * 2)
            if self.authenticated:
                try:
                    status = self.get_status()
                    if status.connected and (
                        expected_band is None or self.band_matches(status, expected_band)
                    ):
                        return status
                except ModemError as exc:
                    last_error = exc
                    self.authenticated = False
            self._sleep(min(self.config.poll_interval, max(0.05, deadline - self._clock())))
        expected = f" B{expected_band}" if expected_band is not None else ""
        raise ModemTimeout(f"F90 did not register LTE{expected} within {timeout:.0f}s: {last_error}")
