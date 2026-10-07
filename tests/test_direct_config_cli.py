import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from f90_band_manager import cli
from f90_band_manager.benchmark import SpeedtestRunner
from f90_band_manager.config import Config, load_config
from f90_band_manager.errors import ConfigurationError


def test_old_vpn_variables_and_profile_do_not_affect_direct_config(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("F90_OLAX_INTERFACE=usb0\nF90_VPN_PEER=not-an-ip\nF90_VPN_INTERFACE=--bad\n"
                        "F90_VPN_CONFIG_FILE=/missing-profile\n")
    monkeypatch.setenv("F90_VPN_PEER", "still-invalid")
    config = load_config(tmp_path, env_file)
    config.require_direct_path()
    assert config.olax_interface == "usb0"
    assert not any(name.startswith("vpn_") for name in vars(config))
    paths = []
    monkeypatch.setattr("f90_band_manager.benchmark.verify_path",
                        lambda *args: paths.append(args) or {"transport": "direct-olax"})
    assert SpeedtestRunner(config).preflight() == {"transport": "direct-olax"}
    assert paths == [("usb0", "1.1.1.1")]


def test_network_commands_require_olax_configuration_before_requests():
    with pytest.raises(ConfigurationError, match="F90_OLAX_INTERFACE"):
        SpeedtestRunner(Config()).preflight()


@pytest.mark.parametrize("args", [["doctor"], ["set", "7", "--dry-run"],
                                  ["scan", "--dry-run"], ["auto", "--dry-run"]])
def test_local_cli_checks_do_not_need_network_path(monkeypatch, capsys, args):
    monkeypatch.setattr(cli, "load_config", lambda *_args: Config())

    def forbidden(*_args, **_kwargs):
        pytest.fail("local command attempted a network or modem request")

    monkeypatch.setattr(cli.SpeedtestRunner, "preflight", forbidden)
    monkeypatch.setattr(cli, "F90Modem", forbidden)
    assert cli.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport"] == "direct-olax"
    assert payload["olax_interface"] == ""
    assert not any("vpn" in name for name in payload)


def test_status_does_not_require_internet_path(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *_args: Config())
    monkeypatch.setattr(cli.SpeedtestRunner, "preflight", lambda *_: pytest.fail("unexpected path check"))
    modem = SimpleNamespace(authenticated=False, get_status=lambda: SimpleNamespace(as_dict=lambda: {"band": 7}, raw={}))
    monkeypatch.setattr(cli, "F90Modem", lambda _: modem)
    assert cli.main(["status", "--no-login"]) == 0
    assert json.loads(capsys.readouterr().out)["band"] == 7


@pytest.mark.parametrize("args", [["set", "7"], ["scan"], ["auto"]])
def test_disruption_guard_still_blocks_before_requests(monkeypatch, capsys, args):
    monkeypatch.setattr(cli, "load_config", lambda *_args: Config(olax_interface="usb0"))
    monkeypatch.setattr(cli.SpeedtestRunner, "ready_for_scan", lambda *_: pytest.fail("unexpected preflight"))
    monkeypatch.setattr(cli, "F90Modem", lambda *_: pytest.fail("unexpected modem access"))
    assert cli.main(args) == 2
    assert "--allow-disruption" in capsys.readouterr().err


def test_path_cli_reports_direct_metadata(monkeypatch, capsys, direct_result):
    monkeypatch.setattr(cli, "load_config", lambda *_args: replace(Config(), olax_interface="usb0"))
    monkeypatch.setattr(cli.SpeedtestRunner, "preflight", lambda *_: direct_result["samples"][0]["path_before"])
    assert cli.main(["path"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_ip"] == "192.168.100.100"
    assert payload["transport"] == "direct-olax"


def test_benchmark_cli_persists_only_direct_current_result(monkeypatch, capsys, tmp_path, direct_result):
    config = Config(olax_interface="usb0", speedtest_backend="speedtest", speedtest_tests=1,
                    results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda *_args: config)
    sample = direct_result["samples"][0]
    monkeypatch.setattr(cli.SpeedtestRunner, "preflight", lambda *_: sample["path_before"])
    monkeypatch.setattr(cli.SpeedtestRunner, "wait_for_path", lambda *_: sample["path_before"])
    monkeypatch.setattr(cli.SpeedtestRunner, "run", lambda *_: sample)
    status = SimpleNamespace(connected=True, band=7, as_dict=lambda: {"band": 7})
    monkeypatch.setattr(cli, "F90Modem", lambda _: SimpleNamespace(get_status=lambda: status))
    assert cli.main(["benchmark"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport"] == "direct-olax"
    assert payload["direct_verified"] is True
    current = json.loads((config.results_dir / "current.json").read_text())
    assert current["direct_verified"] is True
    assert not (config.results_dir / "latest.json").exists()
