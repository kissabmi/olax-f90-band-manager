import copy
import json
import os
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from f90_band_manager import cli, cloudflare, health
from f90_band_manager.benchmark import SpeedtestRunner, create_benchmark
from f90_band_manager.cloudflare import (
    DOWNLOAD_BYTES,
    HOST,
    PREFLIGHT_BYTES,
    UPLOAD_BYTES,
    CloudflareRunner,
)
from f90_band_manager.config import Config, load_config
from f90_band_manager.errors import BenchmarkError, ConfigurationError
from f90_band_manager.network import NetworkPathError
from f90_band_manager.storage import Storage, _direct_sample_verified


@pytest.fixture
def transfer_setup(monkeypatch, direct_result):
    state = {"source": "192.168.100.100", "server": "198.51.100.20",
             "rx_bytes": 0, "tx_bytes": 0, "calls": [], "resolves": [], "paths": [],
             "changed": {}, "upload_paths": [], "move_counters": True}
    monkeypatch.setattr(cloudflare.shutil, "which", lambda _: "/usr/bin/curl")

    def paths(interface, destination, *, runner):
        assert interface == "usb0"
        result = copy.deepcopy(direct_result["samples"][0]["path_before"])
        result["source_ip"] = state["source"]
        result["probe_ip"] = destination
        for key in ("probe_route", "bound_probe_route"):
            result[key]["destination"] = destination
            result[key]["source_ip"] = state["source"]
        state["paths"].append((destination, copy.deepcopy(result)))
        hook = state.get("path_hook")
        if hook:
            hook(destination, result)
        return result

    def resolve(host, port, family, kind):
        assert family == socket.AF_INET and kind == socket.SOCK_STREAM
        state["resolves"].append(host)
        return [(family, kind, 6, "", (state["server"] if host == HOST else host, 0))]

    def runner(command, **kwargs):
        state["calls"].append((command, kwargs))
        if command[0] == "ping":
            assert command[command.index("-I") + 1] == state["source"]
            return SimpleNamespace(returncode=0, stdout=(
                "5 packets transmitted, 5 received, 0% packet loss\n"
                "rtt min/avg/max/mdev = 20.0/30.0/40.0/2.0 ms"), stderr="")
        assert command[0] == "curl"
        direction = "upload" if "--data-binary" in command else "download"
        if direction == "upload":
            upload = Path(command[command.index("--data-binary") + 1][1:])
            state["upload_paths"].append(upload)
            payload = upload.read_bytes()
            assert len(payload) == UPLOAD_BYTES and len(set(payload)) == 256
            assert upload.stat().st_mode & 0o777 == 0o600
            assert upload.parent.stat().st_mode & 0o777 == 0o700
            size = UPLOAD_BYTES
        else:
            size = int(command[-1].rsplit("=", 1)[1])
        if state["move_counters"]:
            state["rx_bytes" if direction == "download" else "tx_bytes"] += size + 1024
        raw = {"http_code": 200, "local_ip": state["source"], "remote_ip": "198.51.100.20",
               "size_download": size if direction == "download" else 0,
               "size_upload": size if direction == "upload" else 0,
               "speed_download": 2_000_000, "speed_upload": 1_000_000, "time_total": 1.0}
        raw.update(state["changed"])
        if state.get("runner_hook"):
            state["runner_hook"](direction)
        if state.get("exception"):
            raise state["exception"]
        return SimpleNamespace(returncode=state.get("exit", 0),
                               stdout=state.get("stdout", json.dumps(raw)), stderr="fixture failure")

    benchmark = CloudflareRunner(Config(olax_interface="usb0"), runner=runner,
                                 resolver=resolve, path_verifier=paths,
                                 counters=lambda _: {key: state[key] for key in ("rx_bytes", "tx_bytes")})
    return benchmark, state


def test_cloudflare_fixed_payload_options_evidence_cleanup(transfer_setup):
    benchmark, state = transfer_setup
    assert benchmark.preflight()["source_ip"] == state["source"]
    assert not state["calls"]
    benchmark.ready_for_scan()
    assert state["calls"][0][0][-1].endswith(f"bytes={PREFLIGHT_BYTES}")
    sample = benchmark.run()
    assert sample["download"] == 16 and sample["upload"] == 8
    assert (sample["ping"], sample["loss"], sample["jitter"]) == (30, 0, 2)
    assert sample["server_id"] == sample["backend"] == "cloudflare"
    assert sample["transferred_bytes"] == {"sent": UPLOAD_BYTES, "received": DOWNLOAD_BYTES}
    assert _direct_sample_verified(sample)
    assert all(not path.exists() for path in state["upload_paths"])
    assert state["resolves"].count(HOST) == 1
    for command, kwargs in state["calls"]:
        if command[0] != "curl":
            continue
        assert command[:2] == ["curl", "--disable"]
        for option, value in (("--noproxy", "*"), ("--proxy", ""), ("--proto", "=https"),
                              ("--max-redirs", "0"), ("--max-time", "90"),
                              ("--interface", state["source"]),
                              ("--resolve", f"{HOST}:443:198.51.100.20")):
            assert command[command.index(option) + 1] == value
        assert "--location" not in command and "--insecure" not in command
        assert kwargs["timeout"] == 100 and "proxy" not in kwargs["env"]
    assert all(record["route_checks_during"] >= 1 for record in sample["transfers"])
    assert {destination for destination, _ in state["paths"]} == {"1.1.1.1", "198.51.100.20"}


def test_cloudflare_pins_server_whole_scan_but_refreshes_source(transfer_setup):
    benchmark, state = transfer_setup
    benchmark.ready_for_scan()
    state.update(server="198.51.100.99", source="192.168.100.101")
    sample = benchmark.run()
    assert sample["server_ip"] == "198.51.100.20"
    assert sample["path_before"]["source_ip"] == "192.168.100.101"
    assert state["resolves"].count(HOST) == 1
    assert _direct_sample_verified(sample)


@pytest.mark.parametrize("changed,message,error", [
    ({"http_code": 302}, "HTTP 200", BenchmarkError),
    ({"http_code": 500}, "HTTP 200", BenchmarkError),
    ({"size_download": 8192}, "exact requested", BenchmarkError),
    ({"size_download": True}, "exact requested", BenchmarkError),
    ({"local_ip": "10.8.1.2"}, "local/remote", NetworkPathError),
    ({"remote_ip": "198.51.100.99"}, "local/remote", NetworkPathError),
    ({"speed_download": float("nan")}, "speed", BenchmarkError),
    ({"speed_download": 0}, "speed", BenchmarkError),
    ({"time_total": None}, "duration", BenchmarkError),
])
def test_cloudflare_rejects_bad_transfer(transfer_setup, changed, message, error):
    benchmark, state = transfer_setup
    state["changed"] = changed
    with pytest.raises(error, match=message):
        benchmark.ready_for_scan()


@pytest.mark.parametrize("failure", ["exit", "json", "root", "timeout", "oserror", "counter"])
def test_cloudflare_rejects_failures(transfer_setup, failure):
    benchmark, state = transfer_setup
    if failure == "exit":
        state["exit"] = 28
    elif failure in {"json", "root"}:
        state["stdout"] = "broken" if failure == "json" else "[]"
    elif failure == "timeout":
        state["exception"] = subprocess.TimeoutExpired("curl", 100)
    elif failure == "oserror":
        state["exception"] = OSError("fixture")
    else:
        state["move_counters"] = False
    with pytest.raises(BenchmarkError):
        benchmark.ready_for_scan()


@pytest.mark.parametrize("route", ["probe_route", "bound_probe_route"])
@pytest.mark.parametrize("destination", ["1.1.1.1", "198.51.100.20"])
def test_cloudflare_rejects_transient_route_drift(transfer_setup, route, destination):
    benchmark, state = transfer_setup
    caught = Event()
    active = Event()

    def path_hook(address, result):
        if active.is_set() and address == destination:
            result[route]["dev"] = "wlan0"
            caught.set()

    def transfer(_direction):
        active.set()
        assert caught.wait(3), "monitor did not check routes during transfer"
        active.clear()  # Routes are restored before curl returns.

    state.update(path_hook=path_hook, runner_hook=transfer)
    with pytest.raises(NetworkPathError, match="stable"):
        benchmark.ready_for_scan()


def test_cloudflare_rejects_source_drift_after_transfer(transfer_setup):
    benchmark, state = transfer_setup
    state["runner_hook"] = lambda _: state.update(source="192.168.100.102")
    with pytest.raises(NetworkPathError, match="route changed"):
        benchmark.ready_for_scan()


def test_cloudflare_upload_cleanup_on_failure(transfer_setup):
    benchmark, state = transfer_setup

    def fail_upload(direction):
        if direction == "upload":
            raise subprocess.TimeoutExpired("curl", 100)

    state["runner_hook"] = fail_upload
    with pytest.raises(BenchmarkError, match="timed out"):
        benchmark.run()
    assert len(state["upload_paths"]) == 1
    assert not state["upload_paths"][0].parent.exists()


@pytest.mark.parametrize("failure", ["amount", "counter", "reset"])
def test_cloudflare_upload_amount_and_counters_required(transfer_setup, failure):
    benchmark, state = transfer_setup
    original = benchmark._runner

    def runner(command, **kwargs):
        result = original(command, **kwargs)
        if "--data-binary" in command:
            if failure == "amount":
                raw = json.loads(result.stdout)
                raw["size_upload"] -= 1
                result.stdout = json.dumps(raw)
            elif failure == "counter":
                state["tx_bytes"] -= UPLOAD_BYTES
            else:
                state["rx_bytes"] = 0
        return result

    benchmark._runner = runner
    with pytest.raises(BenchmarkError, match="payload|counters"):
        benchmark.run()
    assert not state["upload_paths"][0].parent.exists()


def test_cloudflare_path_monitor_unexpected_failure_is_closed(transfer_setup):
    benchmark, state = transfer_setup
    active = Event()
    failed = Event()

    def path_hook(address, result):
        if active.is_set():
            failed.set()
            raise RuntimeError("fixture verifier failure")

    def transfer(_direction):
        active.set()
        assert failed.wait(3)
        active.clear()

    state.update(path_hook=path_hook, runner_hook=transfer)
    with pytest.raises(NetworkPathError, match="stable.*verifier failure"):
        benchmark.ready_for_scan()


def test_cloudflare_missing_curl_no_transfer(transfer_setup, monkeypatch):
    benchmark, state = transfer_setup
    monkeypatch.setattr(cloudflare.shutil, "which", lambda _: None)
    with pytest.raises(BenchmarkError, match="requires curl"):
        benchmark.ready_for_scan()
    assert not state["calls"]


def test_cloudflare_dns_failure_no_transfer(transfer_setup):
    benchmark, state = transfer_setup

    def fail(*_):
        raise OSError("DNS unavailable")

    benchmark._resolver = fail
    with pytest.raises(BenchmarkError, match="cannot resolve"):
        benchmark.ready_for_scan()
    assert not state["calls"]


def test_backend_factory_and_config(tmp_path, monkeypatch):
    monkeypatch.delenv("F90_SPEEDTEST_BACKEND", raising=False)
    assert isinstance(create_benchmark(Config()), CloudflareRunner)
    assert type(create_benchmark(Config(speedtest_backend="speedtest"))) is SpeedtestRunner
    assert load_config(tmp_path).speedtest_backend == "cloudflare"
    monkeypatch.setenv("F90_SPEEDTEST_BACKEND", "speedtest")
    assert load_config(tmp_path).speedtest_backend == "speedtest"
    with pytest.raises(ConfigurationError, match="BACKEND"):
        create_benchmark(Config(speedtest_backend="unknown"))


def test_backend_baseline_segregation(transfer_setup, tmp_path, direct_result):
    benchmark, _ = transfer_setup
    storage = Storage(tmp_path / "results", tmp_path / "state")
    cloud_result = copy.deepcopy(direct_result)
    cloud_result["samples"] = [benchmark.run()]
    payload = storage.save_scan(results=[cloud_result], best_band=7, previous_band=1, final_band=7, mode="scan")
    assert payload["direct_verified"]
    assert storage.load_direct_baseline(interface="usb0", backend="cloudflare") == (7, 20.0)
    assert storage.load_direct_baseline(interface="usb0", backend="speedtest") == (None, None)
    cloud_result["samples"].append(direct_result["samples"][0])
    storage.save_scan(results=[cloud_result], best_band=7, previous_band=1, final_band=7, mode="scan")
    assert storage.load_direct_baseline(interface="usb0", backend="cloudflare") == (None, None)
    assert storage.load_direct_baseline(interface="usb0", backend="speedtest") == (None, None)


@pytest.mark.parametrize("backend,server_id", [(None, "cloudflare"), ("speedtest", "cloudflare"),
                                               ("unknown", "42"), ("cloudflare", "42")])
def test_storage_server_id_requires_correct_backend(direct_result, backend, server_id):
    sample = direct_result["samples"][0]
    sample["server_id"] = server_id
    if backend is not None:
        sample["backend"] = backend
    assert not _direct_sample_verified(sample)


@pytest.mark.parametrize("field,value", [("server_ip", []), ("transfers", []),
                                         ("transferred_bytes", {"sent": 1, "received": 1})])
def test_storage_cloudflare_requires_full_evidence(transfer_setup, field, value):
    benchmark, _ = transfer_setup
    sample = benchmark.run()
    sample[field] = value
    assert not _direct_sample_verified(sample)


@pytest.mark.parametrize("field,value", [("http_code", 302), ("size_download", 1),
                                         ("local_ip", "10.8.1.2"), ("remote_ip", "198.51.100.99"),
                                         ("speed_download", float("inf")), ("time_total", 0)])
def test_storage_cloudflare_rechecks_transfer_evidence(transfer_setup, field, value):
    benchmark, _ = transfer_setup
    sample = benchmark.run()
    sample["transfers"][0]["transfer"][field] = value
    assert not _direct_sample_verified(sample)


def test_health_factory_filters_backend_baseline(monkeypatch, tmp_path, direct_result):
    config = Config(olax_interface="usb0")
    path = direct_result["samples"][0]["path_before"]
    called = []
    benchmark = SimpleNamespace(preflight=lambda: path, _verify_host=lambda *a, **kw: {"1.1.1.1": path})
    monkeypatch.setattr(health, "create_benchmark", lambda cfg: called.append(cfg.speedtest_backend) or benchmark)
    monkeypatch.setattr(health, "run_ping", lambda *a, **kw: {"ping": 30, "loss": 0})
    status = SimpleNamespace(connected=True, as_dict=lambda: {"band": 7})
    modem = SimpleNamespace(login=lambda: None, get_status=lambda: status)
    storage = Storage(tmp_path / "results", tmp_path / "state")
    storage.save_scan(results=[direct_result], best_band=7, previous_band=1, final_band=7, mode="scan")
    result = health.HealthChecker(config, modem, storage).check()
    assert called == ["cloudflare"] and result["baseline_download"] is None


def test_default_cloudflare_cli_factory_table_and_doctor(monkeypatch, capsys, tmp_path, direct_result):
    config = replace(Config(), olax_interface="usb0", results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda *_: config)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/fixture/tool")
    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "cloudflare" and payload["tools"]["curl_found"]
    called = []
    benchmark = SimpleNamespace(ready_for_scan=lambda: called.append("ready"))
    monkeypatch.setattr(cli, "create_benchmark", lambda cfg: called.append(cfg.speedtest_backend) or benchmark)
    monkeypatch.setattr(cli, "F90Modem", lambda _: object())
    monkeypatch.setattr(cli, "ScanRunner", lambda *args, **kwargs: SimpleNamespace(
        scan=lambda bands: {"best_band": 7, "results": {"7": direct_result,
                            "1": {"band": 1, "available": False, "reason": "no LTE"}}}))
    assert cli.main(["scan", "--allow-disruption"]) == 0
    output = capsys.readouterr().out
    assert called == ["cloudflare", "ready"]
    assert "Down Mbps" in output and "unavailable: no LTE" in output and "Selected B7" in output


def test_cloudflare_benchmark_cli_uses_factory_and_persists(transfer_setup, monkeypatch, capsys, tmp_path):
    benchmark, _ = transfer_setup
    config = replace(benchmark.config, speedtest_tests=1,
                     results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda *_: config)
    selected = []
    monkeypatch.setattr(cli, "create_benchmark", lambda cfg: selected.append(cfg.speedtest_backend) or benchmark)
    status = SimpleNamespace(connected=True, band=7, as_dict=lambda: {"band": 7})
    monkeypatch.setattr(cli, "F90Modem", lambda _: SimpleNamespace(get_status=lambda: status))
    assert cli.main(["benchmark"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert selected == ["cloudflare"] and output["backend"] == "cloudflare"
    assert output["direct_verified"] is True
    persisted = json.loads((config.results_dir / "current.json").read_text())
    assert persisted["result"]["samples"][0]["backend"] == "cloudflare"
    assert (config.results_dir / "current.json").stat().st_mode & 0o777 == 0o600
    assert not (config.results_dir / "latest.json").exists()


def test_random_upload_creation_error(transfer_setup, monkeypatch):
    benchmark, _ = transfer_setup

    def fail(_size):
        raise OSError("random unavailable")

    monkeypatch.setattr(os, "urandom", fail)
    with pytest.raises(BenchmarkError, match="prepare Cloudflare upload"):
        benchmark.run()
