"""Fixed-size, source-bound HTTPS throughput, not an Ookla speedtest."""
from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from .benchmark import SpeedtestRunner, run_ping
from .config import Config
from .errors import BenchmarkError
from .network import NetworkPathError, _ipv4, interface_counters, verify_path

HOST = "speed.cloudflare.com"
DOWNLOAD_BYTES = 8 * 1024 * 1024
UPLOAD_BYTES = 2 * 1024 * 1024
PREFLIGHT_BYTES = 64 * 1024
TRANSFER_TIMEOUT = 90


class CloudflareRunner(SpeedtestRunner):
    def __init__(self, config: Config, *, runner: Callable = subprocess.run,
                 resolver: Callable = socket.getaddrinfo,
                 path_verifier: Callable = verify_path,
                 counters: Callable = interface_counters):
        super().__init__(config, runner=runner)
        self._resolver = resolver
        self._path_verifier = path_verifier
        self._counters = counters
        self._server_ip: str | None = None

    def _path(self, probe_ip: str | None = None) -> dict:
        self.config.require_direct_path()
        return self._path_verifier(self.config.olax_interface,
                                   probe_ip or self.config.route_probe_ip, runner=self._runner)

    def _verify_host(self, host: str, *, source_ip: str) -> dict[str, dict]:
        try:
            addresses = sorted({_ipv4(item[4][0]) for item in
                                self._resolver(host, None, socket.AF_INET, socket.SOCK_STREAM)})
        except OSError as exc:
            raise BenchmarkError(f"cannot resolve benchmark host {host}: {exc}") from exc
        if not addresses:
            raise BenchmarkError(f"benchmark host {host} has no IPv4 address")
        routes = {address: self._path(address) for address in addresses}
        if any(path["source_ip"] != source_ip for path in routes.values()):
            raise NetworkPathError("benchmark host would use a different OLAX source")
        return routes

    def _paths(self) -> dict:
        control = self._path()
        if self._server_ip is None:
            routes = self._verify_host(HOST, source_ip=control["source_ip"])
            self._server_ip = min(routes)
        server = self._path(self._server_ip)
        if server["source_ip"] != control["source_ip"]:
            raise NetworkPathError("Cloudflare server/control sources differ")
        return {"control": control, "server": server}

    def _check_paths(self, expected: dict) -> None:
        if self._paths() != expected:
            raise NetworkPathError("OLAX control/server route changed during Cloudflare measurement")

    def _guarded(self, operation: Callable, paths: dict):
        """Check ordinary + bound control/server paths before, during and after IO."""
        self._check_paths(paths)
        stop = Event()
        first_check = Event()
        errors: list[Exception] = []
        checks = []

        def watch():
            while not stop.is_set():
                try:
                    self._check_paths(paths)
                    checks.append(True)
                except Exception as exc:  # noqa: BLE001 - fail closed on daemon worker failure
                    errors.append(exc)
                    return
                finally:
                    first_check.set()
                stop.wait(0.5)

        watcher = Thread(target=watch, daemon=True)
        watcher.start()
        try:
            if not first_check.wait(25) or errors:
                raise NetworkPathError("OLAX route monitor could not verify initial paths")
            result = operation()
        finally:
            stop.set()
            watcher.join(timeout=25)
            if watcher.is_alive():
                raise NetworkPathError("OLAX route monitor did not finish")
            if errors:
                raise NetworkPathError(f"OLAX route was not stable: {errors[0]}")
            self._check_paths(paths)
        if not checks:
            raise NetworkPathError("OLAX route monitor supplied no evidence")
        return result, len(checks)

    def _transfer(self, direction: str, size: int, paths: dict,
                  upload_file: Path | None = None) -> dict:
        if not shutil.which("curl"):
            raise BenchmarkError("Cloudflare backend requires curl")
        source = paths["control"]["source_ip"]
        self._check_paths(paths)
        before = self._counters(self.config.olax_interface)
        # --disable must be first: user curlrc cannot add redirects/proxies/options.
        command = ["curl", "--disable", "--noproxy", "*", "--proxy", "", "--ipv4",
                   "--interface", source, "--resolve", f"{HOST}:443:{self._server_ip}",
                   "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "0",
                   "--connect-timeout", "12", "--max-time", str(TRANSFER_TIMEOUT),
                   "--silent", "--show-error", "--fail", "--header", "Accept-Encoding: identity",
                   "--output", "/dev/null", "--write-out", "%{json}"]
        if direction == "download":
            command += ["--request", "GET", f"https://{HOST}/__down?bytes={size}"]
        else:
            command += ["--request", "POST", "--header", "Content-Type: application/octet-stream",
                        "--data-binary", f"@{upload_file}", f"https://{HOST}/__up"]
        try:
            process, checks = self._guarded(lambda: self._runner(
                command, capture_output=True, text=True, timeout=TRANSFER_TIMEOUT + 10,
                env={"LC_ALL": "C", "LANG": "C"}, check=False), paths)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BenchmarkError(f"Cloudflare {direction} failed to start or timed out: {exc}") from exc
        after = self._counters(self.config.olax_interface)
        self._check_paths(paths)
        if process.returncode:
            detail = (process.stderr or "unknown error").strip()[-240:]
            raise BenchmarkError(f"Cloudflare {direction} exited {process.returncode}: {detail}")
        try:
            raw = json.loads(process.stdout)
        except (ValueError, TypeError) as exc:
            raise BenchmarkError("curl returned malformed transfer JSON") from exc
        if not isinstance(raw, dict) or raw.get("http_code") != 200:
            raise BenchmarkError("Cloudflare transfer did not return HTTP 200")
        if raw.get("local_ip") != source or raw.get("remote_ip") != self._server_ip:
            raise NetworkPathError("curl local/remote IPv4 differs from the verified OLAX/server")
        amount = raw.get(f"size_{direction}")
        speed = raw.get(f"speed_{direction}")
        seconds = raw.get("time_total")
        if type(amount) not in (int, float) or amount != size:
            raise BenchmarkError("Cloudflare transferred size differs from the exact requested payload")
        for name, value in (("speed", speed), ("duration", seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise BenchmarkError(f"curl returned invalid transfer {name}")
        delta = {key: after[key] - before[key] for key in ("rx_bytes", "tx_bytes")}
        counter = "rx_bytes" if direction == "download" else "tx_bytes"
        if any(value < 0 for value in delta.values()) or delta[counter] < size * 0.9:
            raise NetworkPathError("OLAX byte counters do not corroborate Cloudflare transfer")
        return {"direction": direction, "requested_bytes": size, "verified": True,
                "mbps": speed * 8 / 1_000_000, "path_before": paths["control"],
                "path_after": paths["control"], "server_path_before": paths["server"],
                "server_path_after": paths["server"],
                "route_checks_during": checks, "interface_byte_delta": {"olax": delta},
                "transfer": {key: raw.get(key) for key in (
                    "http_code", "local_ip", "remote_ip", "size_download", "size_upload",
                    "speed_download", "speed_upload", "time_total", "time_starttransfer", "time_appconnect")}}

    def ready_for_scan(self) -> dict:
        paths = self._paths()
        self._transfer("download", PREFLIGHT_BYTES, paths)
        self._check_paths(paths)
        return paths["control"]

    def run(self) -> dict:
        paths = self._paths()
        download = self._transfer("download", DOWNLOAD_BYTES, paths)
        try:
            with tempfile.TemporaryDirectory(prefix="olax-cloudflare-") as temporary:
                upload_path = Path(temporary) / "upload.bin"
                with upload_path.open("wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(os.urandom(UPLOAD_BYTES))
                upload = self._transfer("upload", UPLOAD_BYTES, paths, upload_path)
        except OSError as exc:
            raise BenchmarkError(f"cannot prepare Cloudflare upload: {exc}") from exc
        source = paths["control"]["source_ip"]
        ping_routes = self._verify_host(self.config.ping_host, source_ip=source)
        ping, _ = self._guarded(lambda: run_ping(
            self.config.ping_host, count=self.config.ping_count, timeout=self.config.ping_timeout,
            runner=self._runner, source_ip=source), paths)
        if self._verify_host(self.config.ping_host, source_ip=source) != ping_routes:
            raise NetworkPathError("OLAX ping route changed during Cloudflare measurement")
        self._check_paths(paths)
        delta = {key: sum(item["interface_byte_delta"]["olax"][key]
                          for item in (download, upload)) for key in ("rx_bytes", "tx_bytes")}
        return {"backend": "cloudflare", "source": "cloudflare-https",
                "method": "source-bound HTTPS fixed-size transfers; not Ookla",
                "download": download["mbps"], "upload": upload["mbps"],
                "ping": ping["ping"], "jitter": ping["jitter"], "loss": ping["loss"],
                "probe_ping": ping["ping"], "probe_error": ping["error"], "ping_evidence": ping,
                "ping_routes": ping_routes, "transport": "direct-olax", "server_id": "cloudflare",
                "server_host": HOST, "server_ip": self._server_ip,
                "server_routes": {self._server_ip: paths["server"]},
                "path_before": paths["control"], "path_after": paths["control"],
                "interface_byte_delta": {"olax": delta},
                "transferred_bytes": {"sent": UPLOAD_BYTES, "received": DOWNLOAD_BYTES},
                "transfers": [download, upload]}
