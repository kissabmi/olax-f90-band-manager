from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import median

METRICS = ("download", "upload", "ping", "jitter", "loss")


def _valid_numbers(values: Iterable[object]) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def median_or_none(values: Iterable[object]) -> float | None:
    numbers = _valid_numbers(values)
    return float(median(numbers)) if numbers else None


def aggregate_result(band: int, status: dict, samples: list[dict], mask: str) -> dict:
    result: dict = {
        "band": band,
        "mask": mask,
        "available": True,
        "reason": "",
        "samples": samples,
    }
    for metric in METRICS:
        result[metric] = median_or_none(sample.get(metric) for sample in samples)
    for metric in ("rsrp", "rsrq", "sinr"):
        result[metric] = status.get(metric)
    result["pci"] = status.get("pci", "")
    result["cell_id"] = status.get("cell_id", "")
    result["earfcn"] = status.get("earfcn", "")
    result["network_type"] = status.get("network_type", "")
    result["connected"] = bool(status.get("connected"))
    return result


def unavailable_result(band: int, mask: str, reason: str) -> dict:
    return {
        "band": band,
        "mask": mask,
        "available": False,
        "reason": reason[:300],
        "samples": [],
        **{metric: None for metric in METRICS},
        "rsrp": None,
        "rsrq": None,
        "sinr": None,
        "pci": "",
        "cell_id": "",
        "earfcn": "",
        "network_type": "",
        "connected": False,
    }


def _quality_key(result: dict) -> tuple:
    def low(name: str, default: float) -> float:
        value = result.get(name)
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def high(name: str, default: float) -> float:
        value = result.get(name)
        try:
            return -float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    # Lower loss/latency/jitter wins; SINR and upload break otherwise close
    # ties.  Download is deliberately handled before this key.
    return (
        low("loss", 100.0),
        low("ping", 1_000_000.0),
        low("jitter", 1_000_000.0),
        high("sinr", 1_000_000.0),
        high("upload", 1_000_000.0),
        high("download", 1_000_000.0),
    )


def choose_best(
    results: Iterable[dict],
    *,
    max_loss: float = 10.0,
    max_ping: float = 200.0,
    tie_ratio: float = 0.15,
) -> dict | None:
    candidates = []
    for result in results:
        if not result.get("available"):
            continue
        download = result.get("download")
        try:
            download = float(download)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(download) or download <= 0:
            continue
        loss = result.get("loss")
        ping = result.get("ping")
        try:
            if loss is not None and float(loss) > max_loss:
                continue
            if ping is not None and float(ping) > max_ping:
                continue
        except (TypeError, ValueError):
            continue
        candidates.append((download, result))
    if not candidates:
        return None

    fastest_download = max(download for download, _ in candidates)
    near = [
        result
        for download, result in candidates
        if download >= fastest_download * (1.0 - tie_ratio)
    ]
    return min(near, key=_quality_key)
