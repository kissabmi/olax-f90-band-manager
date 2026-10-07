# OLAX F90 band manager

This is a Linux CLI for the USB OLAX F90 modem. Keep changes isolated to this
project: do not edit desktop configuration, routing, VPN settings or modem firmware.

- The only modem settings writes are `SET_FREQ_BAND` followed by `REBOOT_DEVICE`.
  Preserve unrelated settings. Other calls are reads or authentication.
- Benchmark directly through OLAX, without VPN. Validate source binding,
  ordinary and source-bound routes, server identity and OLAX byte counters.
  Never accept Wi-Fi or VPN traffic as a direct modem measurement.
- Preserve the scan lock, `--allow-disruption`, partial progress and fail-safe
  restore behavior. Use identical payloads and one pinned endpoint per scan.
- Use offline fixtures for code changes. Live measurements, Band changes and
  reboots require the owner's explicit request; only one process operates the modem.
- Keep `.env`, passwords, result/state data, local captures and work history
  out of Git. Local configuration and JSON/CSV state should have mode `600`.
- Do not enable the optional timer automatically. First verify live status,
  manual set/benchmark/scan and controlled interruption with successful restore.
- Run `python3 -m pytest -q`, `ruff check src tests` and compilation checks
  appropriate to the changes. Distinguish fixture checks from live validation.
