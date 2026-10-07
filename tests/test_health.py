from types import SimpleNamespace

import pytest

from f90_band_manager import health
from f90_band_manager.benchmark import SpeedtestRunner
from f90_band_manager.config import Config
from f90_band_manager.network import NetworkPathError
from f90_band_manager.storage import Storage


def prepare(monkeypatch, tmp_path, direct_result, *, drift=False):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    path = direct_result["samples"][0]["path_before"]
    calls = []

    def preflight(_self):
        calls.append("preflight")
        return {} if drift and calls.count("preflight") > 1 else path

    def verify(_self, host, *, source_ip):
        assert host == "1.1.1.1"
        assert source_ip == "192.168.100.100"
        calls.append("routes")
        return {host: path}

    def ping(host, **kwargs):
        assert host == "1.1.1.1"
        assert kwargs["source_ip"] == "192.168.100.100"
        calls.append("ping")
        return {"ping": 30, "jitter": 1, "loss": 0, "error": ""}

    monkeypatch.setattr(SpeedtestRunner, "preflight", preflight)
    monkeypatch.setattr(SpeedtestRunner, "_verify_host", verify)
    monkeypatch.setattr(health, "run_ping", ping)
    status = SimpleNamespace(connected=True, as_dict=lambda: {"band": 7})
    modem = SimpleNamespace(login=lambda: None, get_status=lambda: status)
    return health.HealthChecker(Config(olax_interface="usb0", speedtest_backend="speedtest"), modem, storage), storage, calls


def test_health_uses_only_verified_direct_scan_and_bound_ping(monkeypatch, tmp_path, direct_result):
    checker, storage, calls = prepare(monkeypatch, tmp_path, direct_result)
    storage.save_scan(results=[direct_result], best_band=7, previous_band=1, final_band=7, mode="scan")
    result = checker.check()
    assert result["baseline_download"] == 20.0
    assert result["baseline_band"] == 7
    assert result["transport"] == "direct-olax"
    assert result["path_before"] == result["path_after"]
    assert result["bad"] is False
    assert result["probe"]["ok"] is None
    assert calls == ["preflight", "routes", "ping", "routes", "preflight"]


def test_health_ignores_old_vpn_baseline(monkeypatch, tmp_path, direct_result):
    checker, storage, _ = prepare(monkeypatch, tmp_path, direct_result)
    storage._atomic_json(storage.latest_path, {"vpn_verified": True, "best_band": 7,
                                              "results": {"7": {"download": 999}}})
    result = checker.check()
    assert result["baseline_band"] is None
    assert result["baseline_download"] is None


def test_health_rejects_route_drift(monkeypatch, tmp_path, direct_result):
    checker, _, _ = prepare(monkeypatch, tmp_path, direct_result, drift=True)
    with pytest.raises(NetworkPathError, match="OLAX route changed"):
        checker.check()
