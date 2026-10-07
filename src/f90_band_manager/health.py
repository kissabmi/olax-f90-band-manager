from __future__ import annotations

import logging

from .benchmark import create_benchmark, http_probe, run_ping
from .config import Config
from .errors import ConfigurationError, F90Error
from .modem import F90Modem
from .network import NetworkPathError
from .storage import Storage, now_iso

LOG = logging.getLogger(__name__)


class HealthChecker:
    def __init__(self, config: Config, modem: F90Modem, storage: Storage):
        self.config = config
        self.modem = modem
        self.storage = storage

    def check(self) -> dict:
        benchmark = create_benchmark(self.config)
        path = benchmark.preflight()
        status = None
        status_error = ""
        try:
            self.modem.login()
        except ConfigurationError as exc:
            status_error = str(exc)
        except F90Error as exc:
            status_error = str(exc)

        try:
            status = self.modem.get_status()
        except F90Error as exc:
            status_error = status_error or str(exc)

        ping_routes = benchmark._verify_host(self.config.ping_host, source_ip=path["source_ip"])
        ping = run_ping(
            self.config.ping_host,
            count=self.config.ping_count,
            timeout=self.config.ping_timeout,
            source_ip=path["source_ip"],
        )
        if benchmark._verify_host(self.config.ping_host, source_ip=path["source_ip"]) != ping_routes:
            raise NetworkPathError("OLAX ping route changed during health check")
        probe = http_probe(self.config)
        path_after = benchmark.preflight()
        if path_after != path:
            raise NetworkPathError("OLAX route changed during health check")
        best_band, baseline = self.storage.load_direct_baseline(
            interface=self.config.olax_interface, backend=self.config.speedtest_backend)

        reasons: list[str] = []
        if status is None:
            reasons.append("modem status unavailable")
        elif not status.connected:
            reasons.append("LTE is not registered")
        if ping.get("loss") is not None and ping["loss"] > self.config.health_max_loss_pct:
            reasons.append(f"packet loss {ping['loss']:.1f}%")
        if ping.get("ping") is None or ping.get("loss") is None:
            reasons.append("OLAX ping probe failed")
        if ping.get("ping") is not None and ping["ping"] > self.config.health_max_ping_ms:
            reasons.append(f"ping {ping['ping']:.0f}ms")
        probe_download = probe.get("download")
        if self.config.probe_url and not probe.get("ok"):
            reasons.append("OLAX HTTP probe failed")
        if (
            baseline is not None
            and probe_download is not None
            and float(baseline) > 0
            and probe_download < float(baseline) * self.config.health_min_download_ratio
        ):
            reasons.append(f"probe {probe_download:.1f}Mbps below baseline")
        if status_error and status is None:
            reasons.append(status_error[:200])

        return {
            "checked_at": now_iso(),
            "transport": "direct-olax",
            "backend": self.config.speedtest_backend,
            "path_before": path,
            "path_after": path_after,
            "bad": bool(reasons),
            "reasons": reasons,
            "status": status.as_dict() if status else None,
            "status_error": status_error,
            "ping": ping,
            "probe": probe,
            "baseline_download": baseline,
            "baseline_band": best_band,
        }


def record_health(storage: Storage, result: dict, required_bad_checks: int) -> dict:
    previous = storage.load_health()
    try:
        bad_checks = int(previous.get("bad_checks", 0) or 0)
    except (TypeError, ValueError):
        bad_checks = 0
    bad_checks = bad_checks + 1 if result.get("bad") else 0
    state = {
        "bad_checks": bad_checks,
        "degraded": bad_checks >= required_bad_checks,
        "last": result,
    }
    storage.save_health(state)
    return state
