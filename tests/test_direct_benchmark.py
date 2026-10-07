import json
import subprocess
from dataclasses import replace
from threading import Event

import pytest

from f90_band_manager import benchmark
from f90_band_manager.benchmark import SpeedtestRunner
from f90_band_manager.config import Config
from f90_band_manager.errors import BenchmarkError
from f90_band_manager.network import NetworkPathError


def _config() -> Config:
    return Config(
        speedtest_binary="speedtest-cli",
        olax_interface="usb0",
    )


def _path(probe_ip: str, *, gateway: str = "192.168.100.1") -> dict:
    return {
        "transport": "direct-olax",
        "olax_interface": "usb0",
        "probe_ip": probe_ip,
        "source_ip": "192.168.100.100",
        "probe_route": {"destination": probe_ip, "dev": "usb0", "source_ip": "192.168.100.100", "gateway": gateway},
        "bound_probe_route": {"destination": probe_ip, "dev": "usb0", "source_ip": "192.168.100.100", "gateway": gateway},
    }


def _setup(monkeypatch, *, drift=False, server_drift=False, server_source_mismatch=False):
    state = {"speedtest_done": False, "commands": [], "olax_moves": True,
             "counter_interfaces": [], "verified_ips": [], "server_id": "42"}

    def route(olax_interface, probe_ip="1.1.1.1"):
        assert olax_interface == "usb0"
        state["verified_ips"].append(probe_ip)
        changed = state["speedtest_done"] and (drift or (server_drift and probe_ip == "198.51.100.20"))
        gateway = "192.168.100.9" if changed else "192.168.100.1"
        result = _path(probe_ip, gateway=gateway)
        if server_source_mismatch and probe_ip == "198.51.100.20":
            result["source_ip"] = "192.168.100.101"
            result["probe_route"]["source_ip"] = "192.168.100.101"
            result["bound_probe_route"]["source_ip"] = "192.168.100.101"
        return result

    def dns(host, *_args):
        address = "198.51.100.20" if host == "server.example" else "198.51.100.21"
        return [(None, None, None, None, (address, 0))]

    def runner(command, **_kwargs):
        state["commands"].append(command)
        if command[0] == "ping":
            return subprocess.CompletedProcess(command, 0, "5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 1/2/3/1 ms", "")
        if "-c" in command:
            catalog = {"servers": [{"id": "42", "url": "https://server.example:8080/upload.php"}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
        assert "--source" in command and command[command.index("--source") + 1] == "192.168.100.100"
        assert "--server" in command and command[command.index("--server") + 1] == "42"
        state["speedtest_done"] = True
        data = {"download": 20_000_000, "upload": 10_000_000, "ping": 30,
                "bytes_sent": 200_000, "bytes_received": 400_000,
                 "server": {"id": state["server_id"], "url": "https://server.example:8080/upload.php"}}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(benchmark, "verify_path", route)
    monkeypatch.setattr(benchmark.socket, "getaddrinfo", dns)
    monkeypatch.setattr(benchmark.shutil, "which", lambda _name: "/usr/bin/speedtest-cli")
    def counters(interface):
        state["counter_interfaces"].append(interface)
        assert interface == "usb0"
        if not state["speedtest_done"]:
            return {"rx_bytes": 1_000_000, "tx_bytes": 1_000_000}
        if interface == "usb0" and not state["olax_moves"]:
            return {"rx_bytes": 1_000_000, "tx_bytes": 1_000_000}
        return {"rx_bytes": 1_500_000, "tx_bytes": 1_300_000}
    monkeypatch.setattr(benchmark, "interface_counters", counters)
    return runner, state


def test_speedtest_selects_and_checks_server_before_transfer(monkeypatch):
    runner, state = _setup(monkeypatch)
    sample = SpeedtestRunner(_config(), runner=runner).run()
    assert sample["download"] == 20.0
    assert list(sample["server_routes"]) == ["198.51.100.20"]
    assert "-c" in state["commands"][0]
    assert "--server" in state["commands"][1]
    assert sample["transport"] == "direct-olax"
    assert sample["path_before"] == sample["path_after"]
    assert set(sample["interface_byte_delta"]) == {"olax"}
    assert sample["transferred_bytes"] == {"sent": 200_000, "received": 400_000}
    assert state["counter_interfaces"] == ["usb0", "usb0"]
    catalog, _, ping = state["commands"]
    assert "source_address=sys.argv[1]" in catalog[2]
    assert catalog[3] == "192.168.100.100"
    assert ping[ping.index("-I") + 1] == "192.168.100.100"


def test_speedtest_rejects_route_change_during_transfer(monkeypatch):
    runner, _state = _setup(monkeypatch, drift=True)
    with pytest.raises(NetworkPathError, match="route changed"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_speedtest_rejects_server_only_route_change(monkeypatch):
    runner, _state = _setup(monkeypatch, server_drift=True)
    with pytest.raises(NetworkPathError, match="server routes changed"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_speedtest_rejects_server_with_different_olax_source(monkeypatch):
    runner, _state = _setup(monkeypatch, server_source_mismatch=True)
    with pytest.raises(BenchmarkError, match="no speedtest server"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_speedtest_rejects_traffic_that_did_not_cross_olax(monkeypatch):
    runner, state = _setup(monkeypatch)
    state["olax_moves"] = False
    with pytest.raises(NetworkPathError, match="olax counters"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_http_probes_without_verified_binding_stay_disabled():
    config = Config(warmup_url="https://example.test/", probe_url="https://example.test/")
    with pytest.raises(BenchmarkError, match="no verified OLAX source/route binding"):
        SpeedtestRunner(config).warmup()
    with pytest.raises(BenchmarkError, match="no verified OLAX source/route binding"):
        benchmark.http_probe(config)


def test_speedtest_rejects_unverified_returned_server(monkeypatch):
    runner, state = _setup(monkeypatch)
    state["server_id"] = "43"
    with pytest.raises(BenchmarkError, match="different from the verified selection"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_scan_pins_one_server_and_rechecks_it_for_each_sample(monkeypatch):
    runner, state = _setup(monkeypatch)
    instance = SpeedtestRunner(_config(), runner=runner)
    instance.run()
    state["speedtest_done"] = False
    instance.run()
    assert len([c for c in state["commands"] if "-c" in c and c[0] != "ping"]) == 1
    assert [c[c.index("--server") + 1] for c in state["commands"] if "--server" in c] == ["42", "42"]


def test_requested_server_cannot_be_replaced_by_catalog(monkeypatch):
    runner, _ = _setup(monkeypatch)
    with pytest.raises(BenchmarkError, match="no speedtest server"):
        SpeedtestRunner(replace(_config(), speedtest_server_id="99"), runner=runner).run()


def test_all_catalog_and_server_ipv4_routes_are_checked(monkeypatch):
    runner, state = _setup(monkeypatch)

    def dns(host, *_args):
        addresses = ["198.51.100.20", "198.51.100.22"] if host == "server.example" else ["198.51.100.21", "198.51.100.23"]
        return [(None, None, None, None, (address, 0)) for address in addresses]

    monkeypatch.setattr(benchmark.socket, "getaddrinfo", dns)
    sample = SpeedtestRunner(_config(), runner=runner).run()
    assert set(sample["server_routes"]) == {"198.51.100.20", "198.51.100.22"}
    assert {"198.51.100.21", "198.51.100.23"} <= set(state["verified_ips"])


@pytest.mark.parametrize("host", ["www.speedtest.net", "c.speedtest.net", "server.example"])
def test_any_unroutable_catalog_or_server_ipv4_prevents_transfer(monkeypatch, host):
    runner, state = _setup(monkeypatch)
    original = benchmark.verify_path

    def dns(name, *_args):
        addresses = ["198.51.100.20"]
        if name == host:
            addresses.append("198.51.100.24")
        return [(None, None, None, None, (address, 0)) for address in addresses]

    def route(interface, address):
        if address == "198.51.100.24":
            raise NetworkPathError("destination uses Wi-Fi or VPN")
        return original(interface, address)

    monkeypatch.setattr(benchmark.socket, "getaddrinfo", dns)
    monkeypatch.setattr(benchmark, "verify_path", route)
    with pytest.raises(BenchmarkError):
        SpeedtestRunner(_config(), runner=runner).run()
    assert not state["speedtest_done"]


def test_catalog_route_drift_prevents_transfer(monkeypatch):
    runner, state = _setup(monkeypatch)
    original = benchmark.verify_path

    def route(interface, address):
        catalog_done = any("-c" in c for c in state["commands"])
        gateway = "192.168.100.9" if catalog_done and address == "198.51.100.21" else "192.168.100.1"
        return _path(address, gateway=gateway) if catalog_done else original(interface, address)

    monkeypatch.setattr(benchmark, "verify_path", route)
    with pytest.raises(NetworkPathError, match="catalog routes changed"):
        SpeedtestRunner(_config(), runner=runner).run()
    assert not state["speedtest_done"]


def test_ping_route_drift_invalidates_completed_transfer(monkeypatch):
    runner, state = _setup(monkeypatch)

    def route(interface, address):
        ping_done = any(c[0] == "ping" for c in state["commands"])
        return _path(address, gateway="192.168.100.9" if ping_done else "192.168.100.1")

    monkeypatch.setattr(benchmark, "verify_path", route)
    with pytest.raises(NetworkPathError, match="ping route changed"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_route_watch_rejects_transient_drift_even_if_final_path_recovers(monkeypatch):
    runner, _ = _setup(monkeypatch)
    transfer_started, drift_seen = Event(), Event()

    def route(interface, address):
        if transfer_started.is_set() and not drift_seen.is_set():
            drift_seen.set()
            return _path(address, gateway="192.168.100.9")
        return _path(address)

    def transfer(command, **kwargs):
        if "--source" in command:
            transfer_started.set()
            assert drift_seen.wait(3), "route watcher did not observe transfer"
        return runner(command, **kwargs)

    monkeypatch.setattr(benchmark, "verify_path", route)
    with pytest.raises(NetworkPathError, match="not stable"):
        SpeedtestRunner(_config(), runner=transfer).run()


@pytest.mark.parametrize("address", ["198.51.100.20", "198.51.100.22"])
@pytest.mark.parametrize("drift", ["gateway", "vpn"])
def test_route_watch_rejects_transient_server_drift_with_stable_probe(monkeypatch, address, drift):
    runner, _ = _setup(monkeypatch)
    transfer_active, watch_cycle, drift_seen, probe_seen = Event(), Event(), Event(), Event()

    class WatchEvent(Event):
        def wait(self, timeout=None):
            if transfer_active.is_set() and probe_seen.is_set():
                watch_cycle.set()
            return super().wait(timeout)

    def dns(host, *_args):
        addresses = ["198.51.100.20", "198.51.100.22"] if host == "server.example" else ["198.51.100.21"]
        return [(None, None, None, None, (value, 0)) for value in addresses]

    def route(interface, destination):
        if transfer_active.is_set() and destination == "1.1.1.1":
            probe_seen.set()
        if transfer_active.is_set() and destination == address:
            drift_seen.set()
            watch_cycle.set()
            if drift == "vpn":
                raise NetworkPathError("selected server temporarily uses VPN")
            return _path(destination, gateway="192.168.100.9")
        return _path(destination)

    def transfer(command, **kwargs):
        if "--source" in command:
            transfer_active.set()
            try:
                assert watch_cycle.wait(3), "route watcher did not inspect the transfer window"
            finally:
                transfer_active.clear()
        return runner(command, **kwargs)

    monkeypatch.setattr(benchmark, "Event", WatchEvent)
    monkeypatch.setattr(benchmark, "verify_path", route)
    monkeypatch.setattr(benchmark.socket, "getaddrinfo", dns)
    with pytest.raises(NetworkPathError, match="not stable"):
        SpeedtestRunner(_config(), runner=transfer).run()
    assert drift_seen.is_set()


@pytest.mark.parametrize("change,message", [
    ({"bytes_sent": 0}, "no data"), ({"bytes_received": None}, "transferred bytes"),
    ({"server": {"id": "42", "url": "https://other.example/upload.php"}}, "different from"),
    ({"server": {"id": "42", "url": "https://[broken"}}, "invalid server URL"),
])
def test_invalid_speedtest_evidence_is_rejected(monkeypatch, change, message):
    runner, _ = _setup(monkeypatch)

    def modified(command, **kwargs):
        result = runner(command, **kwargs)
        if "--source" in command:
            data = json.loads(result.stdout)
            data.update(change)
            result.stdout = json.dumps(data)
        return result

    with pytest.raises(BenchmarkError, match=message):
        SpeedtestRunner(_config(), runner=modified).run()


def test_counter_read_failure_prevents_transfer(monkeypatch):
    runner, state = _setup(monkeypatch)

    def counters(_interface):
        raise NetworkPathError("counter unavailable")

    monkeypatch.setattr(benchmark, "interface_counters", counters)
    with pytest.raises(NetworkPathError, match="counter unavailable"):
        SpeedtestRunner(_config(), runner=runner).run()
    assert not state["speedtest_done"]


def test_source_change_after_transfer_invalidates_result(monkeypatch):
    runner, state = _setup(monkeypatch)

    def route(interface, address):
        path = _path(address)
        if state["speedtest_done"]:
            path["source_ip"] = "192.168.100.101"
            for name in ("probe_route", "bound_probe_route"):
                path[name]["source_ip"] = path["source_ip"]
        return path

    monkeypatch.setattr(benchmark, "verify_path", route)
    with pytest.raises(NetworkPathError, match="route changed"):
        SpeedtestRunner(_config(), runner=runner).run()


def test_direct_path_wait_recovers_without_vpn(monkeypatch):
    runner, state = _setup(monkeypatch)
    attempts = []

    def path(interface, address):
        attempts.append(interface)
        if len(attempts) == 1:
            raise NetworkPathError("OLAX is still rebooting")
        return _path(address)

    monkeypatch.setattr(benchmark, "verify_path", path)
    monkeypatch.setattr(benchmark.time, "sleep", lambda _seconds: None)
    assert SpeedtestRunner(_config(), runner=runner).wait_for_path()["transport"] == "direct-olax"
    assert attempts == ["usb0", "usb0"]
    assert not state["commands"]


def test_direct_path_wait_timeout_is_actionable(monkeypatch):
    runner, _ = _setup(monkeypatch)
    with pytest.raises(NetworkPathError, match="Direct OLAX path did not recover"):
        SpeedtestRunner(_config(), runner=runner).wait_for_path(timeout=0)
