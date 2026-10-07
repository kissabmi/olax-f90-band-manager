from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import signal
import sys
from pathlib import Path

from . import __version__
from .benchmark import SpeedtestRunner, create_benchmark
from .config import load_config
from .errors import F90Error
from .health import HealthChecker, record_health
from .modem import F90Modem
from .runner import ScanLock, ScanRunner
from .scoring import aggregate_result
from .storage import Storage

LOG = logging.getLogger(__name__)


def _interrupt_on_term(_signum, _frame):
    # Let the set/scan exception path restore the last good LTE band.
    raise KeyboardInterrupt


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _band(value: str) -> int:
    match = re.fullmatch(r"[bB]?(\d+)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("band must look like 7 or B7")
    parsed = int(match.group(1))
    if parsed < 1:
        raise argparse.ArgumentTypeError("band must be positive")
    return parsed


def _bands(value: str) -> tuple[int, ...]:
    result = tuple(_band(item) for item in value.split(",") if item.strip())
    if not result:
        raise argparse.ArgumentTypeError("band list cannot be empty")
    if len(set(result)) != len(result):
        raise argparse.ArgumentTypeError("band list contains duplicates")
    return result


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f90-band",
        description="Safely benchmark and select an LTE band on OLAX F90",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", type=Path, help="configuration file (default: project .env)")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="read current network and signal status")
    status.add_argument("--no-login", action="store_true", help="do not attempt authentication")

    raw = sub.add_parser("raw-get", help="read one or more raw proc_get commands")
    raw.add_argument("commands", nargs="+", help="command names, e.g. lte_band nv_arfcn")
    raw.add_argument("--login", action="store_true", help="authenticate before reading")

    sub.add_parser("path", help="verify direct OLAX routing without modem writes")

    benchmark = sub.add_parser("benchmark", help="speedtest the current LTE band directly through OLAX")
    benchmark.add_argument("--tests", type=int, help="speed tests for the current band")

    set_parser = sub.add_parser("set", help="set one band and wait for LTE registration")
    set_parser.add_argument("band", type=_band)
    set_parser.add_argument("--dry-run", action="store_true", help="show the request without sending it")

    scan = sub.add_parser("scan", help="benchmark every configured band")
    scan.add_argument("--bands", type=_bands, help="override the configured band list")
    scan.add_argument("--tests", type=int, help="speed tests per band")
    scan.add_argument("--dry-run", action="store_true", help="show masks without changing the modem")

    auto = sub.add_parser("auto", help="health-check; scan only after repeated degradation")
    auto.add_argument("--force-scan", action="store_true", help="run the configured fast scan now")
    auto.add_argument("--dry-run", action="store_true", help="show the configured scan plan")

    sub.add_parser("doctor", help="check local configuration and available tools")
    for disruptive in (set_parser, scan, auto):
        disruptive.add_argument("--allow-disruption", action="store_true",
                                help="allow modem reboot and interruption of its internet link")
    return parser


def _make_config(args: argparse.Namespace):
    config = load_config(_project_root(), args.env_file)
    if args.command in {"scan", "benchmark"} and args.tests is not None:
        if args.tests < 1:
            raise F90Error("--tests must be >= 1")
        # Config is frozen, so create a copy only for this invocation.
        from dataclasses import replace

        config = replace(config, speedtest_tests=args.tests)
    return config


def _validate_band(config, band: int) -> None:
    if band not in config.bands:
        raise F90Error(f"B{band} is not in F90_BANDS: {','.join(map(str, config.bands))}")
    config.band_mask(band)


def _apply_single(config, band: int, storage: Storage) -> int:
    modem = F90Modem(config)
    previous = None
    try:
        modem.login()
        current = modem.get_status()
        previous = current.band if current.band in config.bands else storage.load_last_good_band()
    except F90Error:
        # A read-only status may be unavailable, but changing still requires a
        # successful login below.  The persisted last-good band is retained.
        previous = storage.load_last_good_band()

    if previous == band:
        modem.wait_until_online()
        verified = modem.wait_for_lte(expected_band=band)
        create_benchmark(config).wait_for_path()
        try:
            storage.save_last_good_band(band, source="verified")
        except OSError as exc:
            LOG.warning("could not persist last-good B%s: %s", band, exc)
        _json({"band": band, "active": verified.as_dict(), "changed": False})
        return 0

    try:
        modem.wait_until_online()
        modem.set_band(band)
        modem.wait_until_online()
        verified = modem.wait_for_lte(expected_band=band)
        create_benchmark(config).wait_for_path()
        try:
            storage.save_last_good_band(band, source="set")
        except OSError as exc:
            LOG.warning("could not persist last-good B%s: %s", band, exc)
        _json({"band": band, "active": verified.as_dict(), "changed": True})
        return 0
    except (Exception, KeyboardInterrupt):
        if previous is not None and previous != band:
            try:
                print(f"Attempting fail-safe restore B{previous}...", file=sys.stderr)
                modem.wait_until_online()
                modem.set_band(previous)
                modem.wait_until_online()
                modem.wait_for_lte(expected_band=previous)
                try:
                    storage.save_last_good_band(previous, source="restore")
                except OSError as exc:
                    LOG.warning("could not persist last-good B%s: %s", previous, exc)
                print(f"Restored B{previous}.", file=sys.stderr)
            except F90Error as restore_error:
                LOG.error("fail-safe restore failed: %s", restore_error)
        raise


def _dry_scan(config, bands: tuple[int, ...]) -> int:
    for band in bands:
        _validate_band(config, band)
    _json(
        {
            "base_url": config.base_url,
            "transport": "direct-olax",
            "backend": config.speedtest_backend,
            "olax_interface": config.olax_interface,
            "bands": [{"band": band, "work_lte_band": config.band_mask(band)} for band in bands],
            "write_endpoint": "/reqproc/proc_post",
            "read_endpoint": "/reqproc/proc_get",
        }
    )
    return 0


def _doctor(config) -> int:
    tools = {
        "curl_found": bool(shutil.which("curl")),
        "speedtest_binary": config.speedtest_binary,
        "speedtest_found": bool(shutil.which(config.speedtest_binary) or Path(config.speedtest_binary).is_file()),
        "speedtest_cli_fallback_found": bool(shutil.which("speedtest-cli")),
        "ping_found": bool(shutil.which("ping")),
    }
    _json(
        {
            "base_url": config.base_url,
            "username": config.username,
            "password_configured": bool(config.password or config.password_file),
            "auth_mode": config.auth_mode,
            "bands": list(config.bands),
            "fast_scan_bands": list(config.fast_scan_bands),
            "masks": {str(band): config.band_mask(band) for band in config.bands},
            "status_commands": list(config.status_commands),
            "results_dir": str(config.results_dir),
            "state_dir": str(config.state_dir),
            "tools": tools,
            "speedtest_server_id_pinned": bool(config.speedtest_server_id),
            "transport": "direct-olax",
            "backend": config.speedtest_backend,
            "olax_interface": config.olax_interface,
            "direct_path_configured": bool(config.olax_interface),
            "note": "No modem request was made by doctor.",
        }
    )
    return 0


def _scan_table(payload: dict, config) -> None:
    print(f"Comparison ({config.speedtest_backend}, direct OLAX; medians):")
    print("Band  Samples  Down Mbps  Up Mbps  Ping ms  Loss %  Jitter ms  Status")

    def number(value):
        return "n/a" if value is None else f"{value:.2f}"

    for result in payload.get("results", {}).values():
        print(f"B{result['band']:<4} {len(result.get('samples', [])):>7}  "
              f"{number(result.get('download')):>9}  {number(result.get('upload')):>7}  "
              f"{number(result.get('ping')):>7}  {number(result.get('loss')):>6}  "
              f"{number(result.get('jitter')):>9}  "
              f"{'available' if result.get('available') else 'unavailable: ' + result.get('reason', '')}")
    best = payload.get("best_band")
    if best is None:
        print("No usable band passed the download/quality criteria.")
    else:
        print(f"Selected B{best}: fastest-download group (within 15%); "
              "ties prefer loss, ping, jitter, SINR, upload, then download. "
              f"Limits: loss <= {config.health_max_loss_pct:g}%, ping <= {config.health_max_ping_ms:g}ms.")


def _auto(config, args: argparse.Namespace) -> int:
    if args.dry_run:
        return _dry_scan(config, config.fast_scan_bands)
    benchmark_runner = create_benchmark(config)
    benchmark_runner.preflight()
    storage = Storage(config.results_dir, config.state_dir)
    modem = F90Modem(config)
    health = HealthChecker(config, modem, storage).check()
    state = record_health(storage, health, config.health_bad_checks_required)
    current = (health.get("status") or {}).get("band")
    if health.get("bad"):
        reasons = ", ".join(health.get("reasons") or ["unknown"])
        print(f"Health: degraded{f' on B{current}' if current else ''} ({reasons})")
    else:
        print(f"Health: OK{f' on B{current}' if current else ''}. No action required.")

    if not args.force_scan and not state["degraded"]:
        return 0
    benchmark_runner.ready_for_scan()
    bands = config.fast_scan_bands
    print("Starting fast scan..." if not args.force_scan else "Starting forced fast scan...")
    payload = ScanRunner(config, modem, storage, benchmark=benchmark_runner).scan(bands, mode="fast-scan")
    _scan_table(payload, config)
    if payload.get("best_band") is None and bands != config.bands:
        print("Fast scan found no usable band; starting full scan...")
        payload = ScanRunner(config, modem, storage, benchmark=benchmark_runner).scan(config.bands, mode="full-scan")
        _scan_table(payload, config)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    previous_term_handler = None
    if args.command in {"set", "scan", "auto"} and not args.dry_run:
        previous_term_handler = signal.signal(signal.SIGTERM, _interrupt_on_term)
    try:
        config = _make_config(args)
        if args.command in {"set", "scan", "auto"} and not args.dry_run and not args.allow_disruption:
            raise F90Error("this operation reboots OLAX and interrupts its link; use --allow-disruption only during a test window")
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "path":
            _json(SpeedtestRunner(config).preflight())
            return 0
        if args.command == "status":
            modem = F90Modem(config)
            auth_error = ""
            if not args.no_login and (config.password or config.password_file):
                try:
                    modem.login()
                except F90Error as exc:
                    auth_error = str(exc)
            status = modem.get_status()
            payload = status.as_dict()
            payload["raw"] = status.raw
            payload["authenticated"] = modem.authenticated
            if auth_error:
                payload["auth_error"] = auth_error
            _json(payload)
            return 0
        if args.command == "raw-get":
            modem = F90Modem(config)
            if args.login:
                modem.login()
            _json(modem.get_json(tuple(args.commands)))
            return 0
        if args.command == "benchmark":
            benchmark_runner = create_benchmark(config)
            benchmark_runner.preflight()
            modem = F90Modem(config)
            status = modem.get_status()
            if not status.connected or status.band not in config.bands:
                raise F90Error("OLAX is not registered on a configured LTE band")
            storage = Storage(config.results_dir, config.state_dir)
            with ScanLock(config.state_dir / "scan.lock"):
                benchmark_runner.wait_for_path()
                benchmark_runner.warmup()
                samples = [benchmark_runner.run() for _ in range(config.speedtest_tests)]
                result = aggregate_result(status.band, status.as_dict(), samples, config.band_mask(status.band))
                payload = storage.save_current(result)
            _json({"tested_at": payload["tested_at"], "band": status.band,
                    "transport": payload["transport"], "direct_verified": payload["direct_verified"],
                    "backend": config.speedtest_backend,
                   "download": result["download"], "upload": result["upload"],
                   "ping": result["ping"], "samples": len(samples),
                   "result_file": str(storage.current_path)})
            return 0
        if args.command == "set":
            _validate_band(config, args.band)
            if args.dry_run:
                return _dry_scan(config, (args.band,))
            storage = Storage(config.results_dir, config.state_dir)
            create_benchmark(config).ready_for_scan()
            with ScanLock(config.state_dir / "scan.lock"):
                return _apply_single(config, args.band, storage)
        if args.command == "scan":
            bands = args.bands or config.bands
            if args.dry_run:
                return _dry_scan(config, bands)
            for band in bands:
                _validate_band(config, band)
            benchmark_runner = create_benchmark(config)
            benchmark_runner.ready_for_scan()
            storage = Storage(config.results_dir, config.state_dir)
            payload = ScanRunner(config, F90Modem(config), storage, benchmark=benchmark_runner).scan(bands)
            _scan_table(payload, config)
            return 0
        if args.command == "auto":
            return _auto(config, args)
        parser.error(f"unknown command: {args.command}")
    except F90Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        if previous_term_handler is not None:
            signal.signal(signal.SIGTERM, previous_term_handler)
