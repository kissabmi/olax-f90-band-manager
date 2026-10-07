import json
from pathlib import Path

import pytest

from f90_band_manager import cli
from f90_band_manager.config import Config
from f90_band_manager.errors import BenchmarkError, ScanBusy
from f90_band_manager.modem import ModemStatus
from f90_band_manager.network import NetworkPathError
from f90_band_manager.runner import ScanLock, ScanRunner
from f90_band_manager.storage import Storage


def make_status(band: int, connected: bool = True) -> ModemStatus:
    return ModemStatus(
        band=band,
        band_raw=str(band),
        network_type="LTE" if connected else "",
        connected=connected,
        rsrp=-100,
        rsrq=-12,
        sinr=4,
        pci="12",
        cell_id="cell",
        earfcn="123",
        raw={},
    )


class FakeModem:
    def __init__(self):
        self.authenticated = False
        self.band = 7
        self.set_calls = []

    def login(self):
        self.authenticated = True

    def get_status(self):
        return make_status(self.band)

    def wait_until_online(self):
        pass

    def set_band(self, band):
        self.set_calls.append(band)
        self.band = band
        self.authenticated = False

    def wait_for_lte(self, expected_band=None):
        assert expected_band == self.band
        self.authenticated = True
        return make_status(self.band)


class FailingBenchmark:
    def wait_for_path(self):
        pass

    def warmup(self):
        pass

    def run(self):
        raise BenchmarkError("offline")


class WorkingBenchmark(FailingBenchmark):
    def run(self):
        return {"download": 12, "upload": 4, "ping": 40, "jitter": 2, "loss": 0}


class FailingRegistrationModem(FakeModem):
    def __init__(self):
        super().__init__()
        self.band = 1

    def wait_for_lte(self, expected_band=None):
        if expected_band != 7:
            return super().wait_for_lte(expected_band)
        raise BenchmarkError("B7 never registered")


def test_scan_restores_previous_band_when_no_band_is_usable(tmp_path: Path):
    config = Config(
        password="secret",
        bands=(1, 7),
        fast_scan_bands=(1, 7),
        band_masks={
            1: "1,0,0,0,0,0,0,0,0",
            7: "64,0,0,0,0,0,0,0,0",
        },
        stabilize_seconds=0,
        results_dir=tmp_path / "results",
        state_dir=tmp_path / "state",
    )
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()
    payload = ScanRunner(
        config,
        modem,
        storage,
        benchmark=FailingBenchmark(),
        sleep=lambda _: None,
        output=lambda _: None,
    ).scan((1,))
    assert payload["best_band"] is None
    assert payload["final_band"] == 7
    assert modem.band == 7
    assert modem.set_calls == [1, 7]
    assert storage.load_last_good_band() == 7


def test_scan_restores_after_band_write_when_lte_registration_times_out(tmp_path: Path):
    config = Config(
        password="secret",
        bands=(1, 7),
        fast_scan_bands=(1, 7),
        band_masks={
            1: "1,0,0,0,0,0,0,0,0",
            7: "64,0,0,0,0,0,0,0,0",
        },
        stabilize_seconds=0,
        results_dir=tmp_path / "results",
        state_dir=tmp_path / "state",
    )
    storage = Storage(config.results_dir, config.state_dir)
    modem = FailingRegistrationModem()
    payload = ScanRunner(
        config,
        modem,
        storage,
        benchmark=FailingBenchmark(),
        sleep=lambda _: None,
        output=lambda _: None,
    ).scan((7,))

    assert payload["best_band"] is None
    assert payload["final_band"] == 1
    assert modem.band == 1
    assert modem.set_calls == [7, 1]


def test_direct_path_failure_restores_previous_band_and_releases_lock(tmp_path):
    config = Config(olax_interface="usb0", bands=(1, 7), fast_scan_bands=(1, 7),
                    stabilize_seconds=0, results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    modem = FakeModem()
    storage = Storage(config.results_dir, config.state_dir)

    class LostPath(WorkingBenchmark):
        def wait_for_path(self):
            if modem.band == 1:
                raise NetworkPathError("route now uses Wi-Fi")

    with pytest.raises(NetworkPathError, match="Wi-Fi"):
        ScanRunner(config, modem, storage, benchmark=LostPath(), output=lambda _: None).scan((1,))
    assert modem.set_calls == [1, 7]
    assert modem.band == 7
    assert storage.load_last_good_band() == 7
    assert not storage.latest_path.exists()
    with ScanLock(config.state_dir / "scan.lock"):
        pass


def test_busy_scan_lock_prevents_modem_changes(tmp_path):
    config = Config(results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    modem = FakeModem()
    runner = ScanRunner(config, modem, Storage(config.results_dir, config.state_dir), benchmark=WorkingBenchmark())
    with ScanLock(runner.lock_path), pytest.raises(ScanBusy):
        runner.scan((1,))
    assert not modem.set_calls
    assert not modem.authenticated


def test_single_set_restores_on_direct_path_failure(monkeypatch, tmp_path):
    config = Config(olax_interface="usb0", results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()
    monkeypatch.setattr(cli, "F90Modem", lambda _: modem)
    class LostPath(WorkingBenchmark):
        def wait_for_path(self):
            raise NetworkPathError("route uses VPN")

    monkeypatch.setattr(cli, "create_benchmark", lambda _: LostPath())
    with pytest.raises(NetworkPathError, match="VPN"):
        cli._apply_single(config, 1, storage)
    assert modem.set_calls == [1, 7]
    assert storage.load_last_good_band() == 7


def test_scan_reapplies_previous_band_after_failed_registration(tmp_path: Path):
    config = Config(
        password="secret", bands=(7, 1), fast_scan_bands=(7, 1),
        band_masks={7: "64,0,0,0,0,0,0,0,0", 1: "1,0,0,0,0,0,0,0,0"},
        stabilize_seconds=0, speedtest_tests=1,
        results_dir=tmp_path / "results", state_dir=tmp_path / "state",
    )
    modem = FailingRegistrationModem()
    payload = ScanRunner(
        config, modem, Storage(config.results_dir, config.state_dir),
        benchmark=WorkingBenchmark(), sleep=lambda _: None, output=lambda _: None,
    ).scan((7, 1))
    assert payload["results"]["1"]["available"] is True
    assert payload["best_band"] == 1
    assert modem.set_calls == [7, 1]


def test_scan_persists_each_band_and_completes_progress(tmp_path):
    config = Config(bands=(1, 7), fast_scan_bands=(1, 7), speedtest_tests=1,
                    stabilize_seconds=0, results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()
    progress = []

    class ObserveProgress(WorkingBenchmark):
        def run(self):
            progress.append(json.loads((config.results_dir / "scan-progress.json").read_text()))
            return super().run()

    ScanRunner(config, modem, storage, benchmark=ObserveProgress(), output=lambda _: None).scan((7, 1))
    assert progress[0]["results"] == {}
    assert set(progress[1]["results"]) == {"7"}
    final = json.loads((config.results_dir / "scan-progress.json").read_text())
    assert final["state"] == "completed" and set(final["results"]) == {"7", "1"}
    assert (config.results_dir / "scan-progress.json").stat().st_mode & 0o777 == 0o600


def test_interrupt_preserves_partial_results_and_restores(tmp_path):
    config = Config(bands=(1, 7), fast_scan_bands=(1, 7), speedtest_tests=1,
                    stabilize_seconds=0, results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()

    class InterruptSecondBand(WorkingBenchmark):
        def run(self):
            if modem.band == 1:
                raise KeyboardInterrupt
            return super().run()

    with pytest.raises(KeyboardInterrupt):
        ScanRunner(config, modem, storage, benchmark=InterruptSecondBand(), output=lambda _: None).scan((7, 1))
    progress = json.loads((config.results_dir / "scan-progress.json").read_text())
    assert progress["state"] == "aborted" and set(progress["results"]) == {"7"}
    assert progress["error"] == "KeyboardInterrupt"
    assert progress["final_band"] == 7 and modem.band == 7
    assert modem.set_calls == [1, 7]
    assert not storage.latest_path.exists()
    with ScanLock(config.state_dir / "scan.lock"):
        pass


def test_scan_rejects_measurement_if_modem_changes_band(tmp_path):
    config = Config(speedtest_tests=1, stabilize_seconds=0,
                    results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()

    class DifferentBand(WorkingBenchmark):
        def run(self):
            modem.band = 3
            return super().run()

    payload = ScanRunner(config, modem, storage, benchmark=DifferentBand(), output=lambda _: None).scan((1,))
    assert not payload["results"]["1"]["available"]
    assert "Actual LTE Band" in payload["results"]["1"]["reason"]
    assert payload["best_band"] is None and modem.band == 7


def test_scan_restores_drift_while_testing_initial_band(tmp_path):
    config = Config(speedtest_tests=1, stabilize_seconds=0,
                    results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()

    class DifferentBand(WorkingBenchmark):
        def run(self):
            modem.band = 3
            return super().run()

    payload = ScanRunner(config, modem, storage, benchmark=DifferentBand(), output=lambda _: None).scan((7,))
    assert not payload["results"]["7"]["available"]
    assert payload["best_band"] is None
    assert payload["final_band"] == modem.band == 7
    assert modem.set_calls == [7]


def test_final_progress_write_failure_preserves_published_winner(tmp_path, monkeypatch):
    config = Config(speedtest_tests=1, stabilize_seconds=0,
                    results_dir=tmp_path / "results", state_dir=tmp_path / "state")
    storage = Storage(config.results_dir, config.state_dir)
    modem = FakeModem()
    runner = ScanRunner(config, modem, storage, benchmark=WorkingBenchmark(), output=lambda _: None)
    save_progress = runner._save_progress

    def fail_completed(*args, **kwargs):
        if kwargs["state"] == "completed":
            raise OSError("fixture disk full")
        return save_progress(*args, **kwargs)

    monkeypatch.setattr(runner, "_save_progress", fail_completed)
    payload = runner.scan((1,))
    assert payload["best_band"] == payload["final_band"] == modem.band == 1
    assert storage.load_latest()["final_band"] == 1
    assert modem.set_calls == [1]
