"""Read-only IPv4 path preflight for direct USB OLAX measurements."""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path

from .errors import BenchmarkError


class NetworkPathError(BenchmarkError):
    """The requested direct OLAX path cannot be confirmed from routing state."""


def interface_counters(interface: str) -> dict[str, int]:
    """Read kernel byte counters for corroborating a routed speedtest."""
    _interface_name(interface)
    base = Path("/sys/class/net") / interface / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text().strip())
        tx = int((base / "tx_bytes").read_text().strip())
    except (OSError, ValueError) as exc:
        raise NetworkPathError(f"Не удалось прочитать счётчики {interface}") from exc
    if rx < 0 or tx < 0:
        raise NetworkPathError(f"Некорректные счётчики {interface}")
    return {"rx_bytes": rx, "tx_bytes": tx}


def _ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as exc:
        raise NetworkPathError("Для проверки пути нужен числовой IPv4-адрес") from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast or str(address) == "255.255.255.255":
        raise NetworkPathError("Адрес проверки пути не может быть локальным или широковещательным")
    return str(address)


def _interface_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,14}", name):
        raise NetworkPathError("Недопустимое имя сетевого интерфейса")


def _ip_json(arguments: list[str], runner) -> list[dict]:
    try:
        result = runner(["ip", "-j", "-4", *arguments], capture_output=True,
                        text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NetworkPathError("Не удалось выполнить проверку маршрута через ip") from exc
    if result.returncode:
        raise NetworkPathError("ip не смог подтвердить интерфейс или маршрут")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise NetworkPathError("ip вернул некорректный JSON") from exc
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise NetworkPathError("ip вернул пустой или неоднозначный результат")
    return data


def _addresses(interface: str, runner) -> set[str]:
    info = _ip_json(["address", "show", "dev", interface], runner)[0]
    flags = info.get("flags")
    # Some USB network devices report UNKNOWN while administratively UP.
    if (info.get("ifname") != interface or not isinstance(flags, list)
            or "UP" not in flags or info.get("operstate") not in ("UP", "UNKNOWN")
            or "NO-CARRIER" in flags):
        raise NetworkPathError(f"Интерфейс {interface} не поднят")
    entries = info.get("addr_info")
    if not isinstance(entries, list):
        raise NetworkPathError(f"У интерфейса {interface} нет IPv4")
    addresses = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("family") != "inet":
            continue
        if entry.get("valid_life_time") == 0 or entry.get("tentative") or entry.get("dadfailed"):
            continue
        addresses.add(_ipv4(entry.get("local")))
    if not addresses:
        raise NetworkPathError(f"У интерфейса {interface} нет пригодного IPv4")
    return addresses


def _route(destination: str, interface: str, addresses: set[str], runner,
           source: str | None = None) -> dict:
    arguments = ["route", "get", destination]
    if source:
        arguments += ["from", source]
    route = _ip_json(arguments, runner)[0]
    flags = route.get("flags", [])
    if (route.get("dev") != interface or route.get("type", "unicast") != "unicast"
            or not isinstance(flags, list) or "linkdown" in flags
            or route.get("nexthops") or route.get("error")):
        raise NetworkPathError(
            f"Маршрут к {destination} не подтверждён через OLAX {interface}; "
            "проверьте маршрутизацию и отключите VPN перед тестом, исключите путь через Wi-Fi"
        )
    selected = route.get("prefsrc") or route.get("src") or route.get("from") or source
    selected = _ipv4(selected)
    if selected not in addresses or (source is not None and selected != source):
        raise NetworkPathError(f"Исходный IPv4 маршрута не принадлежит {interface}")
    snapshot = {"destination": destination, "dev": interface, "source_ip": selected}
    if route.get("gateway"):
        snapshot["gateway"] = _ipv4(route["gateway"])
    return snapshot


def verify_path(olax_interface: str, probe_ip: str = "1.1.1.1", *, runner=subprocess.run) -> dict:
    """Require ordinary and source-bound IPv4 routes through the configured OLAX.

    This snapshot checks kernel routing, not reachability or arbitrary server
    destinations. The benchmark verifies those destinations separately.
    No routes or interface settings are changed.
    """
    _interface_name(olax_interface)
    probe_ip = _ipv4(probe_ip)
    olax_addresses = _addresses(olax_interface, runner)
    probe = _route(probe_ip, olax_interface, olax_addresses, runner)
    bound = _route(probe_ip, olax_interface, olax_addresses, runner, probe["source_ip"])
    return {"transport": "direct-olax", "olax_interface": olax_interface,
            "source_ip": probe["source_ip"], "probe_ip": probe_ip,
            "probe_route": probe, "bound_probe_route": bound}
