from __future__ import annotations

import fcntl
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from .benchmark import SpeedtestRunner, create_benchmark
from .config import Config
from .errors import ConfigurationError, F90Error, ScanBusy
from .modem import F90Modem, ModemStatus
from .network import NetworkPathError
from .scoring import aggregate_result, choose_best, unavailable_result
from .storage import Storage, now_iso

LOG = logging.getLogger(__name__)


class ScanLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise ScanBusy("another F90 scan is already running") from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class ScanRunner:
    def __init__(
        self,
        config: Config,
        modem: F90Modem,
        storage: Storage,
        benchmark: SpeedtestRunner | None = None,
        *,
        sleep: Callable = time.sleep,
        output: Callable[[str], None] = print,
    ):
        self.config = config
        self.modem = modem
        self.storage = storage
        self.benchmark = benchmark or create_benchmark(config)
        self._sleep = sleep
        self._output = output
        self._last_requested_band: int | None = None

    @property
    def lock_path(self) -> Path:
        return self.config.state_dir / "scan.lock"

    def _say(self, message: str) -> None:
        self._output(message)

    def _save_progress(self, bands, results, previous_band, *, state, final_band=None, error="") -> None:
        self.storage._atomic_json(self.config.results_dir / "scan-progress.json", {
            "schema": 1,
            "updated_at": now_iso(),
            "state": state,
            "transport": "direct-olax",
            "backend": self.config.speedtest_backend,
            "bands": list(bands),
            "previous_band": previous_band,
            "requested_band": self._last_requested_band,
            "final_band": final_band,
            "results": {str(result["band"]): result for result in results},
            "error": error[:300],
        })

    def _discover_previous(self) -> int | None:
        try:
            if not self.modem.authenticated:
                self.modem.login()
            status = self.modem.get_status()
            if status.band in self.config.bands:
                self._remember_band(status.band, source="observed")
                return status.band
        except F90Error as exc:
            LOG.warning("could not read current F90 band: %s", exc)
        saved = self.storage.load_last_good_band()
        return saved if saved in self.config.bands else None

    def _apply_and_wait(self, band: int) -> ModemStatus:
        self.modem.wait_until_online()
        self._last_requested_band = band
        self.modem.set_band(band)
        # The modem may disappear before LTE comes back.  Record the write
        # immediately so a timeout cannot make the caller believe the old
        # band is still active.
        self._last_requested_band = band
        # The radio may disappear immediately, while the web service can take
        # a few seconds to close.  Polling handles both reboot and radio-reset
        # firmware without assuming a fixed outage duration.
        self.modem.wait_until_online()
        status = self.modem.wait_for_lte(expected_band=band)
        self.benchmark.wait_for_path()
        return status

    def _remember_band(self, band: int, *, source: str) -> None:
        try:
            self.storage.save_last_good_band(band, source=source)
        except OSError as exc:
            # A full/read-only filesystem must not turn a successfully restored
            # modem into a reported restore failure.
            LOG.warning("could not persist last-good B%s: %s", band, exc)

    def _restore(self, band: int | None) -> bool:
        if band is None:
            self._say("No previous band is known; automatic restore is unavailable.")
            return False
        try:
            self._say(f"Restoring previous good band B{band}...")
            self._apply_and_wait(band)
            self._remember_band(band, source="restore")
            self._say(f"Restored B{band}.")
            return True
        except F90Error as exc:
            LOG.error("failed to restore B%s: %s", band, exc)
            self._say(f"Restore B{band} failed: {exc}")
            return False

    @staticmethod
    def _display_number(value: object, suffix: str = "") -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "n/a"
        return f"{number:.2f}{suffix}"

    def scan(self, bands: tuple[int, ...] | list[int] | None = None, *, mode: str = "scan") -> dict:
        bands = tuple(bands or self.config.bands)
        for band in bands:
            self.config.band_mask(band)

        with ScanLock(self.lock_path):
            previous_band = self._discover_previous()
            if previous_band is None:
                raise F90Error("No previous working Band is known; refusing scan without a restore target")
            self._say(
                f"Starting {mode} for {', '.join(f'B{band}' for band in bands)}"
                + (f" (previous B{previous_band})" if previous_band else "")
            )
            results: list[dict] = []
            current_band = previous_band
            self._last_requested_band = previous_band
            completed = False
            failure = ""
            self._save_progress(bands, results, previous_band, state="running")
            try:
                for band in bands:
                    self._say(f"Testing B{band}...")
                    try:
                        if current_band != band or self._last_requested_band != band:
                            status = self._apply_and_wait(band)
                            current_band = band
                        else:
                            self.modem.wait_until_online()
                            status = self.modem.wait_for_lte(expected_band=band)
                        self.benchmark.wait_for_path()
                        if self.config.stabilize_seconds:
                            self._sleep(self.config.stabilize_seconds)
                        self.benchmark.warmup()
                        samples = [self.benchmark.run() for _ in range(self.config.speedtest_tests)]
                        measured_status = self.modem.get_status()
                        current_band = measured_status.band if measured_status.connected else None
                        if not measured_status.connected or measured_status.band != band:
                            raise F90Error(f"Actual LTE Band after measurement is not the requested B{band}")
                        result = aggregate_result(band, status.as_dict(), samples, self.config.band_mask(band))
                        results.append(result)
                        self._say(
                            f"B{band}: {self._display_number(result['download'], ' Mbps')} down, "
                            f"{self._display_number(result['upload'], ' Mbps')} up, "
                            f"ping {self._display_number(result['ping'], ' ms')}"
                        )
                    except Exception as exc:
                        if isinstance(exc, (NetworkPathError, ConfigurationError)):
                            raise
                        reason = str(exc).replace("\n", " ")[:300]
                        LOG.warning("B%s unavailable: %s", band, reason)
                        self._say(f"B{band}: unavailable ({reason})")
                        results.append(unavailable_result(band, self.config.band_mask(band), reason))
                    self._save_progress(bands, results, previous_band, state="running")

                best = choose_best(
                    results,
                    max_loss=self.config.health_max_loss_pct,
                    max_ping=self.config.health_max_ping_ms,
                )
                best_band = int(best["band"]) if best else None
                final_band = current_band
                if best_band is not None:
                    self._say(f"Best band: B{best_band}")
                    if current_band != best_band or self._last_requested_band != best_band:
                        self._say(f"Applying B{best_band}...")
                        self._apply_and_wait(best_band)
                    final_band = best_band
                    self._say(f"Done. Active band: B{final_band}.")
                else:
                    self._say("No usable band found.")
                    if self._last_requested_band != previous_band or current_band != previous_band:
                        restored = self._restore(previous_band)
                        final_band = previous_band if restored else None
                    else:
                        final_band = previous_band
                payload = self.storage.save_scan(
                    results=results,
                    best_band=best_band,
                    previous_band=previous_band,
                    final_band=final_band,
                    mode=mode,
                )
                completed = True
                try:
                    self._save_progress(bands, results, previous_band, state="completed", final_band=final_band)
                except OSError as exc:
                    LOG.warning("scan result saved but final progress could not be updated: %s", exc)
                return payload
            except BaseException as exc:
                failure = str(exc) or type(exc).__name__
                raise
            finally:
                if not completed:
                    restored = True
                    if self._last_requested_band != previous_band or current_band != previous_band:
                        # Keep the rollback even if persisting partial evidence failed.
                        restored = self._restore(previous_band)
                    try:
                        self._save_progress(
                            bands, results, previous_band, state="aborted",
                            final_band=previous_band if restored else None, error=failure,
                        )
                    except OSError as exc:
                        LOG.error("could not persist aborted scan progress: %s", exc)
