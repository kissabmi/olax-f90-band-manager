import copy

import pytest


@pytest.fixture
def direct_result():
    source = "192.168.100.100"

    def path(destination):
        route = {"destination": destination, "dev": "usb0", "source_ip": source,
                 "gateway": "192.168.100.1"}
        return {"transport": "direct-olax", "olax_interface": "usb0", "source_ip": source,
                "probe_ip": destination, "probe_route": route,
                "bound_probe_route": copy.deepcopy(route)}

    before = path("1.1.1.1")
    sample = {"transport": "direct-olax", "download": 20.0, "upload": 10.0,
              "ping": 30.0, "jitter": 1.0, "loss": 0.0, "server_id": "42",
              "server_host": "server.example", "server_routes": {"198.51.100.20": path("198.51.100.20")},
              "path_before": before, "path_after": copy.deepcopy(before),
              "interface_byte_delta": {"olax": {"rx_bytes": 500_000, "tx_bytes": 300_000}},
              "transferred_bytes": {"sent": 200_000, "received": 400_000}}
    return {"band": 7, "available": True, "download": 20.0, "upload": 10.0,
            "ping": 30.0, "samples": [sample]}
