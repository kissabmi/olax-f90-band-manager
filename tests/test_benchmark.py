from f90_band_manager.benchmark import parse_speedtest_json


def test_parse_ookla_json_bandwidth_is_bytes_per_second():
    parsed = parse_speedtest_json(
        {
            "download": {"bandwidth": 2_500_000},
            "upload": {"bandwidth": 1_250_000},
            "ping": {"latency": 32.0, "jitter": 3.0},
            "packetLoss": 0.5,
            "server": {"id": 123},
        }
    )
    assert parsed["download"] == 20.0
    assert parsed["upload"] == 10.0
    assert parsed["ping"] == 32.0
    assert parsed["jitter"] == 3.0
    assert parsed["loss"] == 0.5


def test_parse_speedtest_cli_json_uses_bits_per_second():
    parsed = parse_speedtest_json(
        {"download": 20_000_000, "upload": 10_000_000, "ping": 32.0},
        legacy=True,
    )
    assert parsed["download"] == 20.0
    assert parsed["upload"] == 10.0
    assert parsed["source"] == "speedtest-cli"

