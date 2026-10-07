import copy
import json

import pytest

from f90_band_manager.storage import Storage


def save_scan(storage, result):
    return storage.save_scan(results=[result], best_band=7, previous_band=1, final_band=7, mode="scan")


def test_new_current_and_scan_are_explicit_verified_direct(tmp_path, direct_result):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    current = storage.save_current(direct_result)
    scan = save_scan(storage, direct_result)
    for payload in (current, scan):
        assert payload["schema"] == 2
        assert payload["transport"] == "direct-olax"
        assert payload["direct_verified"] is True
        assert "vpn_verified" not in payload
    assert current["mode"] == "current-band-direct"
    assert storage.load_direct_baseline(interface="usb0") == (7, 20.0)
    assert storage.load_direct_baseline(interface="usb1") == (None, None)
    assert storage.latest_path.stat().st_mode & 0o777 == 0o600
    assert storage.current_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["legacy", "vpn", "flag-only", "unverified", "current"])
def test_old_or_unverified_data_cannot_supply_baseline(tmp_path, direct_result, kind):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    payload = save_scan(storage, direct_result)
    if kind == "legacy":
        payload.pop("transport")
        payload["schema"] = 1
    elif kind == "vpn":
        payload["transport"] = "vpn-olax"
        payload["vpn_verified"] = True
    elif kind == "flag-only":
        payload["results"]["7"]["samples"] = [{"transport": "direct-olax"}]
    elif kind == "unverified":
        payload["direct_verified"] = False
    else:
        payload["mode"] = "current-band-direct"
    storage._atomic_json(storage.latest_path, payload)
    original = storage.latest_path.read_bytes()
    assert storage.load_direct_baseline(interface="usb0") == (None, None)
    assert storage.latest_path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("transport", "vpn"), ("vpn_path_before", {"vpn_interface": "amn0"}),
    ("path_after", {}), ("path_before", {}), ("server_routes", {}),
    ("server_id", ""), ("server_host", ""),
    ("interface_byte_delta", {"vpn": {"rx_bytes": 500_000, "tx_bytes": 300_000}}),
    ("interface_byte_delta", {"olax": {"rx_bytes": 0, "tx_bytes": 300_000}}),
    ("transferred_bytes", {"sent": 200_000, "received": 0}),
    ("transferred_bytes", None),
])
def test_invalid_sample_evidence_is_not_verified(tmp_path, direct_result, field, value):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    direct_result["samples"][0][field] = value
    assert save_scan(storage, direct_result)["direct_verified"] is False
    assert storage.save_current(direct_result)["direct_verified"] is False
    assert storage.load_direct_baseline(interface="usb0") == (None, None)


@pytest.mark.parametrize("route,field,value", [
    ("probe_route", "dev", "wlan0"), ("bound_probe_route", "dev", "amn0"),
    ("probe_route", "source_ip", "10.8.1.2"),
    ("bound_probe_route", "destination", "8.8.8.8"),
    ("probe_route", "gateway", "127.0.0.1"),
])
def test_mislabeled_direct_path_cannot_supply_baseline(tmp_path, direct_result, route, field, value):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    sample = direct_result["samples"][0]
    sample["path_before"][route][field] = value
    sample["path_after"] = copy.deepcopy(sample["path_before"])
    assert save_scan(storage, direct_result)["direct_verified"] is False
    assert storage.load_direct_baseline(interface="usb0") == (None, None)


def test_legacy_csv_is_appended_without_changing_header(tmp_path, direct_result):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    original = "timestamp,band,download\nold,1,5\n"
    storage.history_path.write_text(original)
    save_scan(storage, direct_result)
    assert storage.history_path.read_text().startswith(original)


def test_unavailable_or_missing_samples_are_not_direct_verified(tmp_path):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    for result in ({"band": 7, "available": False}, {"band": 7, "available": True, "samples": []}):
        assert save_scan(storage, result)["direct_verified"] is False


@pytest.mark.parametrize("value", [None, True, "20", -1, float("nan"), float("inf")])
def test_bad_download_cannot_supply_baseline(tmp_path, direct_result, value):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    direct_result["download"] = value
    save_scan(storage, direct_result)
    assert storage.load_direct_baseline(interface="usb0") == (None, None)


def test_malformed_history_is_ignored_without_rewriting(tmp_path):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    storage.latest_path.write_text(json.dumps({"schema": 2, "transport": "direct-olax",
                                              "direct_verified": True, "mode": "scan", "results": []}))
    assert storage.load_direct_baseline(interface="usb0") == (None, None)


@pytest.mark.parametrize("mode", [[], {}, None, 7, True])
def test_invalid_mode_is_ignored_without_rewriting(tmp_path, direct_result, mode):
    storage = Storage(tmp_path / "results", tmp_path / "state")
    payload = save_scan(storage, direct_result)
    payload["mode"] = mode
    storage._atomic_json(storage.latest_path, payload)
    original = storage.latest_path.read_bytes()
    assert storage.load_direct_baseline(interface="usb0") == (None, None)
    assert storage.latest_path.read_bytes() == original
