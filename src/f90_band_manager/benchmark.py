from __future__ import annotations

import json
import logging
import math
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from .config import Config
from .errors import BenchmarkError, F90Error
from .network import NetworkPathError, interface_counters, verify_path

LOG = logging.getLogger(__name__)


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _json_from_output(output: str) -> dict:
    output = output.strip()
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        start = output.find("{")
        end = output.rfind("}")
        if start < 0 or end <= start:
            raise BenchmarkError("speedtest did not return JSON")
        try:
            value = json.loads(output[start : end + 1])
        except json.JSONDecodeError as exc:
            raise BenchmarkError("speedtest returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise BenchmarkError("speedtest JSON root is not an object")
    return value


def parse_speedtest_json(data: dict, *, legacy: bool = False) -> dict:
    """Normalize Ookla and python-speedtest-cli JSON to Mbps/ms/percent."""
    download = data.get("download")
    upload = data.get("upload")
    ping = data.get("ping")
    jitter = None
    loss = data.get("packetLoss")

    if isinstance(download, dict):
        download_value = _float(download.get("bandwidth"))
        download_mbps = download_value * 8 / 1_000_000 if download_value is not None else None
    else:
        download_value = _float(download)
        download_mbps = download_value / 1_000_000 if download_value is not None else None

    if isinstance(upload, dict):
        upload_value = _float(upload.get("bandwidth"))
        upload_mbps = upload_value * 8 / 1_000_000 if upload_value is not None else None
    else:
        upload_value = _float(upload)
        upload_mbps = upload_value / 1_000_000 if upload_value is not None else None

    if isinstance(ping, dict):
        ping_ms = _float(ping.get("latency"))
        jitter = _float(ping.get("jitter"))
    else:
        ping_ms = _float(ping)

    if loss is not None:
        loss = _float(loss)
    server = data.get("server")
    server_id = ""
    if isinstance(server, dict):
        server_id = str(server.get("id") or "")

    return {
        "download": download_mbps,
        "upload": upload_mbps,
        "ping": ping_ms,
        "jitter": jitter,
        "loss": loss,
        "server_id": server_id,
        "source": "speedtest-cli" if legacy else "ookla",
    }


_LOSS_RE = re.compile(r"(?P<loss>\d+(?:\.\d+)?)%\s+packet loss", re.IGNORECASE)
_RTT_RE = re.compile(
    r"=\s*(?P<min>\d+(?:\.\d+)?)/(?P<avg>\d+(?:\.\d+)?)/"
    r"(?P<max>\d+(?:\.\d+)?)/(?P<mdev>\d+(?:\.\d+)?)\s*ms"
)


def run_ping(
    host: str,
    *,
    count: int = 5,
    timeout: float = 3.0,
    runner: Callable = subprocess.run,
    source_ip: str = "",
) -> dict:
    """Return an ICMP sample without making ping failure fatal to a scan."""
    command = [
        "ping",
        "-n",
        *(["-I", source_ip] if source_ip else []),
        "-c",
        str(count),
        "-W",
        str(max(1, math.ceil(timeout))),
        host,
    ]
    try:
        process = runner(
            command,
            capture_output=True,
            text=True,
            timeout=max(5.0, (timeout + 1) * count),
            env={"LC_ALL": "C", "LANG": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ping": None, "jitter": None, "loss": None, "error": str(exc)}

    output = f"{process.stdout}\n{process.stderr}"
    loss_match = _LOSS_RE.search(output)
    rtt_match = _RTT_RE.search(output)
    return {
        "ping": float(rtt_match.group("avg")) if rtt_match else None,
        # Linux ping's mdev is a useful, conservative jitter approximation.
        "jitter": float(rtt_match.group("mdev")) if rtt_match else None,
        "loss": float(loss_match.group("loss")) if loss_match else None,
        "error": "" if process.returncode == 0 else f"ping exit {process.returncode}",
    }


class SpeedtestRunner:
    def __init__(self, config: Config, *, runner: Callable = subprocess.run):
        self.config = config
        self._runner = runner
        self._legacy_cache: dict[str, bool] = {}
        self._selected_server: dict | None = None

    def _available_binary(self) -> str:
        configured = self.config.speedtest_binary
        if Path(configured).is_file() or shutil.which(configured):
            return configured
        fallback = shutil.which("speedtest-cli")
        if fallback:
            return fallback
        raise BenchmarkError(
            f"speedtest binary {configured!r} was not found (install speedtest-cli or configure F90_SPEEDTEST_BINARY)"
        )

    def _is_legacy(self, binary: str) -> bool:
        if binary in self._legacy_cache:
            return self._legacy_cache[binary]
        if "speedtest-cli" in Path(binary).name.lower():
            self._legacy_cache[binary] = True
            return True
        try:
            process = self._runner(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version = f"{process.stdout}\n{process.stderr}".lower()
        except (OSError, subprocess.TimeoutExpired):
            version = ""
        legacy = "speedtest-cli" in version and "ookla" not in version
        self._legacy_cache[binary] = legacy
        return legacy

    def _command(self, binary: str, source_ip: str, server_id: str) -> tuple[list[str], bool]:
        legacy = self._is_legacy(binary)
        if legacy:
            command = [binary, "--json", "--source", source_ip,
                       "--timeout", str(self.config.speedtest_request_timeout)]
            command[1:1] = ["--server", server_id]
        else:
            raise BenchmarkError("this speedtest client cannot be verified with an OLAX source; use speedtest-cli")
        return command, legacy

    def _path(self, probe_ip: str | None = None) -> dict:
        self.config.require_direct_path()
        return verify_path(
            self.config.olax_interface,
            probe_ip or self.config.route_probe_ip,
        )

    def preflight(self) -> dict:
        """Check direct OLAX routing without sending a speedtest request."""
        return self._path()

    def ready_for_scan(self) -> dict:
        """Require direct OLAX and a routed speedtest server before modem writes."""
        path = self._path()
        binary = self._available_binary()
        if not self._is_legacy(binary):
            raise BenchmarkError("Direct OLAX benchmark requires speedtest-cli with --source")
        self._select_server(path["source_ip"])
        if self._path() != path:
            raise NetworkPathError("OLAX route changed during speedtest preflight")
        return path

    def wait_for_path(self, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        last_error: BenchmarkError | None = None
        while time.monotonic() < deadline:
            try:
                return self._path()
            except BenchmarkError as exc:
                last_error = exc
                time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        raise NetworkPathError(f"Direct OLAX path did not recover within {timeout:.0f}s: {last_error}")

    def _verify_host(self, host: str, *, source_ip: str) -> dict[str, dict]:
        try:
            resolved = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            raise BenchmarkError(f"cannot resolve speedtest host {host}: {exc}") from exc
        addresses = sorted({entry[4][0] for entry in resolved})
        if not addresses:
            raise BenchmarkError(f"speedtest host {host} has no IPv4 address")
        routes = {}
        for address in addresses:
            route = self._path(address)
            if route["source_ip"] != source_ip:
                raise NetworkPathError(f"speedtest host {host} would use a different OLAX source")
            routes[address] = route
        return routes

    def _select_server(self, source_ip: str) -> dict:
        # The sivel CLI's --list omits hostnames. Its installed Python API
        # exposes the server URLs before any download/upload is attempted.
        if self._selected_server is not None:
            current_routes = self._verify_host(self._selected_server["host"], source_ip=source_ip)
            if current_routes != self._selected_server["routes"]:
                raise NetworkPathError("speedtest server routes changed during the scan")
            return self._selected_server
        catalog_routes = {
            host: self._verify_host(host, source_ip=source_ip)
            for host in ("www.speedtest.net", "c.speedtest.net")
        }
        catalog_code = (
            "import json,speedtest,sys\n"
            "client=speedtest.Speedtest(source_address=sys.argv[1],timeout=int(sys.argv[3]),secure=True)\n"
            "requested=[int(sys.argv[2])] if sys.argv[2] else []\n"
            "groups=client.get_servers(servers=requested)\n"
            "servers=[{'id':str(s['id']),'url':s['url']} for d in sorted(groups) for s in groups[d]]\n"
            "print(json.dumps({'servers':servers[:50]}))\n"
        )
        try:
            process = self._runner(
                [sys.executable, "-c", catalog_code, source_ip,
                 self.config.speedtest_server_id, str(self.config.speedtest_request_timeout)],
                capture_output=True, text=True, timeout=self.config.speedtest_timeout,
                env={"LC_ALL": "C", "LANG": "C"}, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BenchmarkError(f"speedtest server discovery failed: {exc}") from exc
        if process.returncode:
            detail = (process.stderr or process.stdout or "unknown error").strip().splitlines()[-1]
            raise BenchmarkError(f"speedtest server discovery failed: {detail[:240]}")
        catalog = _json_from_output(process.stdout)
        for host, routes in catalog_routes.items():
            if self._verify_host(host, source_ip=source_ip) != routes:
                raise NetworkPathError("speedtest catalog routes changed during discovery")
        servers = catalog.get("servers")
        if not isinstance(servers, list):
            raise BenchmarkError("speedtest server discovery returned an invalid catalog")
        for item in servers:
            if not isinstance(item, dict):
                continue
            try:
                host = urlsplit(item.get("url", "")).hostname
            except (TypeError, ValueError):
                continue
            if not host or not str(item.get("id", "")).isdigit():
                continue
            if self.config.speedtest_server_id and str(item["id"]) != self.config.speedtest_server_id:
                continue
            try:
                routes = self._verify_host(host, source_ip=source_ip)
            except BenchmarkError:
                continue
            self._selected_server = {"id": str(item["id"]), "host": host, "routes": routes}
            return self._selected_server
        raise BenchmarkError("no speedtest server has a verified direct OLAX route")

    def run(self) -> dict:
        path_before = self._path()
        binary = self._available_binary()
        selected = self._select_server(path_before["source_ip"])
        current_path = self._path()
        if current_path != path_before:
            raise NetworkPathError("OLAX route changed during server discovery")
        counters_before = {
            "olax": interface_counters(self.config.olax_interface),
        }
        command, legacy = self._command(binary, path_before["source_ip"], selected["id"])
        LOG.debug("running speedtest with %s", binary)
        stop_watch = Event()
        route_errors: list[F90Error] = []

        def watch_route() -> None:
            while not stop_watch.is_set():
                try:
                    if self._path() != path_before:
                        route_errors.append(NetworkPathError("OLAX route changed during speedtest"))
                        return
                    for address, expected_path in selected["routes"].items():
                        if self._path(address) != expected_path:
                            route_errors.append(NetworkPathError("speedtest server routes changed during measurement"))
                            return
                except F90Error as exc:
                    route_errors.append(exc)
                    return
                stop_watch.wait(1.0)

        watcher = Thread(target=watch_route, daemon=True)
        watcher.start()
        process = None
        run_error: BenchmarkError | None = None
        try:
            process = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.speedtest_timeout,
                env={"LC_ALL": "C", "LANG": "C"},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            run_error = BenchmarkError(f"speedtest failed to start or timed out: {exc}")
        finally:
            stop_watch.set()
            watcher.join(timeout=12)
        if watcher.is_alive():
            raise NetworkPathError("OLAX route monitor did not finish")
        if route_errors:
            raise NetworkPathError(f"OLAX route was not stable during speedtest: {route_errors[0]}")
        if self._path() != path_before:
            raise NetworkPathError("OLAX route changed during speedtest")
        counters_after = {
            "olax": interface_counters(self.config.olax_interface),
        }
        if run_error is not None:
            raise run_error
        assert process is not None
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "unknown error").strip().splitlines()[-1]
            raise BenchmarkError(f"speedtest exited {process.returncode}: {detail[:240]}")
        raw = _json_from_output(process.stdout)
        try:
            sent = int(raw["bytes_sent"])
            received = int(raw["bytes_received"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkError("speedtest did not report transferred bytes") from exc
        if sent <= 0 or received <= 0:
            raise BenchmarkError("speedtest transferred no data in one direction")
        deltas: dict[str, dict[str, int]] = {}
        for label in ("olax",):
            deltas[label] = {
                key: counters_after[label][key] - counters_before[label][key]
                for key in ("rx_bytes", "tx_bytes")
            }
            if (deltas[label]["rx_bytes"] < received * 0.5
                    or deltas[label]["tx_bytes"] < sent * 0.5):
                raise NetworkPathError(f"{label} counters do not support this speedtest transfer")
        sample = parse_speedtest_json(raw, legacy=legacy)
        server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
        server_url = server.get("url", "")
        try:
            server_host = urlsplit(server_url).hostname if server_url else None
        except (TypeError, ValueError) as exc:
            raise BenchmarkError("speedtest returned an invalid server URL") from exc
        if not server_host or str(server.get("id", "")) != selected["id"] or server_host != selected["host"]:
            raise BenchmarkError("speedtest returned a server different from the verified selection")
        server_routes = self._verify_host(server_host, source_ip=path_before["source_ip"])
        if server_routes != selected["routes"]:
            raise NetworkPathError("speedtest server routes changed during measurement")
        path_after = self._path()
        if path_after != path_before:
            raise NetworkPathError("OLAX route changed during speedtest")
        ping_routes = self._verify_host(self.config.ping_host, source_ip=path_before["source_ip"])
        probe = run_ping(
            self.config.ping_host,
            count=self.config.ping_count,
            timeout=self.config.ping_timeout,
            runner=self._runner,
            source_ip=path_before["source_ip"],
        )
        if self._verify_host(self.config.ping_host, source_ip=path_before["source_ip"]) != ping_routes:
            raise NetworkPathError("OLAX ping route changed during measurement")
        path_after = self._path()
        if path_after != path_before:
            raise NetworkPathError("OLAX route changed during ping")
        # Speedtest ping is the comparable server latency.  ICMP supplies
        # jitter/loss and becomes a fallback for ping if the CLI omitted it.
        if sample["ping"] is None:
            sample["ping"] = probe["ping"]
        if sample["jitter"] is None:
            sample["jitter"] = probe["jitter"]
        if sample["loss"] is None:
            sample["loss"] = probe["loss"]
        sample["probe_ping"] = probe["ping"]
        sample["probe_error"] = probe["error"]
        sample["transport"] = "direct-olax"
        sample["path_before"] = path_before
        sample["path_after"] = path_after
        sample["server_host"] = server_host
        sample["server_routes"] = server_routes
        sample["interface_byte_delta"] = deltas
        sample["transferred_bytes"] = {"sent": sent, "received": received}
        return sample

    def warmup(self) -> dict:
        if not self.config.warmup_url:
            return {"skipped": True}
        raise BenchmarkError("HTTP warm-up has no verified OLAX source/route binding; leave F90_WARMUP_URL empty")


def create_benchmark(config: Config, **kwargs) -> SpeedtestRunner:
    """Select an explicit backend; import locally to avoid a module cycle."""
    config.validate()
    if config.speedtest_backend == "cloudflare":
        from .cloudflare import CloudflareRunner

        return CloudflareRunner(config, **kwargs)
    return SpeedtestRunner(config, **kwargs)


def http_probe(config: Config) -> dict:
    """Keep unverified HTTP health downloads disabled."""
    if not config.probe_url:
        return {"ok": None, "download": None, "seconds": None, "error": "probe disabled"}
    raise BenchmarkError("HTTP health probe has no verified OLAX source/route binding; leave F90_PROBE_URL empty")
