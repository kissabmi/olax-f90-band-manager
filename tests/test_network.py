import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

from f90_band_manager.errors import BenchmarkError
from f90_band_manager.network import verify_path

OLAX = "usb0"
PROBE = "1.1.1.1"


def responses():
    return [
        [{"ifname": OLAX, "flags": ["UP", "LOWER_UP"], "operstate": "UP",
          "addr_info": [{"family": "inet", "local": "192.168.0.10"}]}],
        [{"dst": PROBE, "dev": OLAX, "gateway": "192.168.0.1",
          "prefsrc": "192.168.0.10", "flags": []}],
        [{"dst": PROBE, "dev": OLAX, "from": "192.168.0.10", "flags": []}],
    ]


class FakeIP:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data if data is not None else responses())
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        assert kwargs["timeout"] == 10
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.data.pop(0)))


def test_direct_olax_path_and_source_bound_route_are_checked():
    runner = FakeIP()
    result = verify_path(OLAX, runner=runner)
    assert result["transport"] == "direct-olax"
    assert result["source_ip"] == "192.168.0.10"
    assert result["probe_route"]["gateway"] == "192.168.0.1"
    assert not any(key.startswith("vpn") for key in result)
    assert runner.calls == [
        ["ip", "-j", "-4", "address", "show", "dev", OLAX],
        ["ip", "-j", "-4", "route", "get", PROBE],
        ["ip", "-j", "-4", "route", "get", PROBE, "from", "192.168.0.10"],
    ]


@pytest.mark.parametrize("index,field,value", [
    (0, "flags", []), (0, "operstate", "DOWN"),
    (0, "addr_info", []), (0, "flags", ["UP", "NO-CARRIER"]),
    (0, "ifname", "other0"), (0, "operstate", None), (0, "operstate", []),
    (1, "dev", "wlan0"), (2, "dev", "wlan0"),
    (1, "dev", "tun0"), (2, "dev", "amn0"),
    (1, "prefsrc", "10.8.0.2"), (2, "from", "192.168.0.11"),
    (1, "type", "blackhole"), (1, "flags", ["linkdown"]),
    (1, "nexthops", [{"dev": "wlan0"}]), (1, "prefsrc", None),
])
def test_rejects_wrong_interface_down_or_ambiguous_route(index, field, value):
    data = responses()
    data[index][0][field] = value
    with pytest.raises(BenchmarkError):
        verify_path(OLAX, runner=FakeIP(data))


@pytest.mark.parametrize("bad", [[], {}, None, [None], [{}, {}]])
def test_rejects_bad_ip_json_shape(bad):
    with pytest.raises(BenchmarkError):
        verify_path(OLAX, runner=FakeIP([bad]))


@pytest.mark.parametrize("olax,probe", [
    ("--help", PROBE), ("../usb0", PROBE), ("", PROBE),
    (OLAX, "probe.example"), (OLAX, "::1"),
    (OLAX, "127.0.0.1"), (OLAX, "0.0.0.0"),
    (OLAX, "224.0.0.1"), (OLAX, "255.255.255.255"),
])
def test_rejects_invalid_arguments_before_execution(olax, probe):
    runner = FakeIP()
    with pytest.raises(BenchmarkError):
        verify_path(olax, probe, runner=runner)
    assert not runner.calls


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("ip", 10)])
def test_subprocess_errors_are_benchmark_errors(failure):
    def runner(*args, **kwargs):
        raise failure
    with pytest.raises(BenchmarkError):
        verify_path(OLAX, runner=runner)


@pytest.mark.parametrize("code,output", [(1, "private stderr"), (0, "not json")])
def test_failure_output_is_not_leaked(code, output):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=code, stdout=output)
    with pytest.raises(BenchmarkError) as error:
        verify_path(OLAX, runner=runner)
    assert output not in str(error.value)


def test_snapshot_discards_unrelated_fields():
    data = responses()
    for item in data:
        item[0]["secret"] = "not-for-results"
    assert "not-for-results" not in json.dumps(verify_path(OLAX, runner=FakeIP(data)))


@pytest.mark.parametrize("field,value", [("valid_life_time", 0), ("tentative", True), ("dadfailed", True)])
def test_unusable_interface_addresses_are_rejected(field, value):
    data = responses()
    data[0][0]["addr_info"][0][field] = value
    with pytest.raises(BenchmarkError):
        verify_path(OLAX, runner=FakeIP(data))


def test_bound_route_can_omit_source_when_kernel_uses_requested_source():
    data = responses()
    del data[2][0]["from"]
    result = verify_path(OLAX, runner=FakeIP(data))
    assert result["bound_probe_route"]["source_ip"] == "192.168.0.10"
