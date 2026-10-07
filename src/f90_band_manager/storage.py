from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .network import NetworkPathError, _interface_name, _ipv4

CSV_FIELDS = (
    "timestamp",
    "band",
    "download",
    "upload",
    "ping",
    "jitter",
    "loss",
    "rsrp",
    "rsrq",
    "sinr",
    "pci",
    "cell_id",
    "earfcn",
    "available",
    "reason",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_number(value: object) -> object:
    if value is None:
        return ""
    return value


def _direct_path_verified(path: object, *, interface: str = "", source: str = "") -> bool:
    if not isinstance(path, dict) or path.get("transport") != "direct-olax":
        return False
    if any(key.startswith("vpn_") for key in path):
        return False
    try:
        _interface_name(path.get("olax_interface"))
        _ipv4(path.get("source_ip"))
        _ipv4(path.get("probe_ip"))
    except NetworkPathError:
        return False
    if ((interface and path["olax_interface"] != interface)
            or (source and path["source_ip"] != source)):
        return False
    for name in ("probe_route", "bound_probe_route"):
        route = path.get(name)
        if not isinstance(route, dict) or any(
            route.get(key) != value for key, value in {
                "dev": path["olax_interface"], "source_ip": path["source_ip"],
                "destination": path["probe_ip"],
            }.items()
        ):
            return False
        if "gateway" in route:
            try:
                _ipv4(route["gateway"])
            except NetworkPathError:
                return False
    return True


def _direct_sample_verified(sample: object) -> bool:
    if not isinstance(sample, dict) or sample.get("transport") != "direct-olax":
        return False
    if any(key.startswith("vpn_") for key in sample):
        return False
    before, after = sample.get("path_before"), sample.get("path_after")
    if not _direct_path_verified(before) or after != before:
        return False
    routes = sample.get("server_routes")
    backend = sample.get("backend", "speedtest")
    server_id = sample.get("server_id", "")
    valid_server = ((backend == "speedtest" and str(server_id).isdigit())
                    or (backend == "cloudflare" and server_id == "cloudflare"))
    if (not sample.get("server_host") or not valid_server
            or not isinstance(routes, dict) or not routes):
        return False
    for address, path in routes.items():
        if (not _direct_path_verified(path, interface=before["olax_interface"], source=before["source_ip"])
                or path["probe_ip"] != address):
            return False
    deltas, transferred = sample.get("interface_byte_delta"), sample.get("transferred_bytes")
    if not isinstance(deltas, dict) or set(deltas) != {"olax"} or not isinstance(transferred, dict):
        return False
    counters = deltas["olax"]
    if not isinstance(counters, dict):
        return False
    for direction, counter in (("sent", "tx_bytes"), ("received", "rx_bytes")):
        amount, delta = transferred.get(direction), counters.get(counter)
        if (type(amount) is not int or type(delta) is not int or amount <= 0
                or delta < amount * 0.5):
            return False
    if backend == "cloudflare":
        return _cloudflare_evidence_verified(sample)
    return True


def _cloudflare_evidence_verified(sample: dict) -> bool:
    from .cloudflare import DOWNLOAD_BYTES, HOST, UPLOAD_BYTES

    source = sample["path_before"]["source_ip"]
    routes = sample["server_routes"]
    address = sample.get("server_ip")
    if (sample["server_host"] != HOST or not isinstance(address, str) or set(routes) != {address}
            or sample["transferred_bytes"] != {"sent": UPLOAD_BYTES, "received": DOWNLOAD_BYTES}):
        return False
    transfers = sample.get("transfers")
    if not isinstance(transfers, list) or len(transfers) != 2:
        return False
    for record, direction, size, counter in zip(
            transfers, ("download", "upload"), (DOWNLOAD_BYTES, UPLOAD_BYTES), ("rx_bytes", "tx_bytes")):
        if not isinstance(record, dict):
            return False
        if (record.get("direction") != direction or record.get("verified") is not True
                or record.get("requested_bytes") != size
                or type(record.get("route_checks_during")) is not int
                or record["route_checks_during"] < 1
                or record.get("path_before") != sample["path_before"]
                or record.get("path_after") != sample["path_after"]
                or record.get("server_path_before") != routes[address]
                or record.get("server_path_after") != routes[address]):
            return False
        raw = record.get("transfer")
        delta = record.get("interface_byte_delta")
        if (not isinstance(raw, dict) or raw.get("http_code") != 200
                or raw.get("local_ip") != source or raw.get("remote_ip") != address
                or type(raw.get(f"size_{direction}")) not in (int, float)
                or raw[f"size_{direction}"] != size
                or not isinstance(delta, dict) or not isinstance(delta.get("olax"), dict)):
            return False
        counters = delta["olax"]
        if any(type(counters.get(key)) is not int or counters[key] < 0 for key in ("rx_bytes", "tx_bytes")):
            return False
        if counters[counter] < size * 0.9:
            return False
        for key in (f"speed_{direction}", "time_total"):
            value = raw.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                return False
    return not any(sample["interface_byte_delta"]["olax"][key] != sum(
            record["interface_byte_delta"]["olax"][key] for record in transfers)
            for key in ("rx_bytes", "tx_bytes"))


def _direct_result_verified(result: object) -> bool:
    if not isinstance(result, dict) or not result.get("available"):
        return False
    samples = result.get("samples")
    return isinstance(samples, list) and bool(samples) and all(_direct_sample_verified(s) for s in samples)


class Storage:
    def __init__(self, results_dir: Path, state_dir: Path):
        self.results_dir = Path(results_dir).expanduser()
        self.state_dir = Path(state_dir).expanduser()
        self.results_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.results_dir, 0o700)
        os.chmod(self.state_dir, 0o700)

    @property
    def latest_path(self) -> Path:
        return self.results_dir / "latest.json"

    @property
    def current_path(self) -> Path:
        return self.results_dir / "current.json"

    @property
    def history_path(self) -> Path:
        return self.results_dir / "history.csv"

    @property
    def last_good_path(self) -> Path:
        return self.state_dir / "last-good.json"

    @property
    def health_path(self) -> Path:
        return self.state_dir / "health.json"

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load_latest(self) -> dict | None:
        try:
            with self.latest_path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def save_current(self, result: dict) -> dict:
        payload = {"schema": 2, "tested_at": now_iso(), "mode": "current-band-direct",
                   "transport": "direct-olax", "direct_verified": _direct_result_verified(result),
                   "result": result}
        self._atomic_json(self.current_path, payload)
        return payload

    def load_direct_baseline(self, *, interface: str, backend: str = "speedtest") -> tuple[int | None, float | None]:
        """Do not trust historical/VPN data or a verification flag alone."""
        latest = self.load_latest() or {}
        mode = latest.get("mode")
        if (latest.get("schema") != 2 or latest.get("transport") != "direct-olax"
                or latest.get("direct_verified") is not True or latest.get("vpn_verified")
                or not isinstance(mode, str) or mode not in {"scan", "fast-scan", "full-scan"}):
            return None, None
        results = latest.get("results")
        if not isinstance(results, dict):
            return None, None
        available = [r for r in results.values() if isinstance(r, dict) and r.get("available")]
        if not available or not all(_direct_result_verified(r) for r in available):
            return None, None
        if any(s.get("backend", "speedtest") != backend for r in available for s in r["samples"]):
            return None, None
        band = latest.get("best_band")
        result = results.get(str(band))
        if type(band) is not int or not _direct_result_verified(result) or result.get("band") != band:
            return None, None
        if any(s["path_before"]["olax_interface"] != interface for s in result["samples"]):
            return None, None
        download = result.get("download")
        if isinstance(download, bool) or not isinstance(download, (int, float)) or not math.isfinite(download) or download <= 0:
            return None, None
        return band, float(download)

    def load_last_good_band(self) -> int | None:
        try:
            with self.last_good_path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            band = value.get("band") if isinstance(value, dict) else None
            return int(band) if band is not None else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save_last_good_band(self, band: int, *, source: str = "scan") -> None:
        self._atomic_json(self.last_good_path, {"band": band, "saved_at": now_iso(), "source": source})

    def save_scan(
        self,
        *,
        results: list[dict],
        best_band: int | None,
        previous_band: int | None,
        final_band: int | None,
        mode: str,
    ) -> dict:
        tested_at = now_iso()
        available_results = [result for result in results if result.get("available")]
        direct_verified = bool(available_results) and all(_direct_result_verified(r) for r in available_results)
        payload = {
            "schema": 2,
            "tested_at": tested_at,
            "mode": mode,
            "best_band": best_band,
            "previous_band": previous_band,
            "final_band": final_band,
            "results": {str(result["band"]): result for result in results},
            "transport": "direct-olax",
            "direct_verified": direct_verified,
        }
        self._atomic_json(self.latest_path, payload)
        self._append_history(tested_at, results)
        if best_band is not None:
            self.save_last_good_band(best_band, source=mode)
        return payload

    def _append_history(self, timestamp: str, results: list[dict]) -> None:
        is_new = not self.history_path.exists() or self.history_path.stat().st_size == 0
        with self.history_path.open("a", encoding="utf-8", newline="") as handle:
            os.chmod(self.history_path, 0o600)
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            for result in results:
                writer.writerow(
                    {
                        "timestamp": timestamp,
                        "band": result.get("band", ""),
                        "download": _safe_number(result.get("download")),
                        "upload": _safe_number(result.get("upload")),
                        "ping": _safe_number(result.get("ping")),
                        "jitter": _safe_number(result.get("jitter")),
                        "loss": _safe_number(result.get("loss")),
                        "rsrp": _safe_number(result.get("rsrp")),
                        "rsrq": _safe_number(result.get("rsrq")),
                        "sinr": _safe_number(result.get("sinr")),
                        "pci": result.get("pci", ""),
                        "cell_id": result.get("cell_id", ""),
                        "earfcn": result.get("earfcn", ""),
                        "available": result.get("available", False),
                        "reason": result.get("reason", ""),
                    }
                )

    def load_health(self) -> dict:
        try:
            with self.health_path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_health(self, value: dict) -> None:
        self._atomic_json(self.health_path, value)
