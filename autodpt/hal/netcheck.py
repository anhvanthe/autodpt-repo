"""Network-level checks between the laptop and the instruments (SRS NET-01, NET-07, NET-08).

These run before VISA so that a failure is reported as what it is: wrong IP/subnet, cable, port
closed (remote interface disabled on the instrument), or a VISA/driver problem.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import dataclass

#: VXI-11 needs the ONC-RPC portmapper (TCP 111); VICP uses TCP 1861 (LeCroy).
PORT_VXI11 = 111
PORT_VICP = 1861


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    kind: str  # vxi11 | socket | vicp | hislip | usb


def endpoint_from_resource(resource: str, default_socket_port: int | None = None) -> Endpoint | None:
    """Host/port that must be reachable for a VISA resource string. None for USB/other."""
    r = resource.strip()
    m = re.fullmatch(r"VICP::([^:]+)(::INSTR)?", r, re.I)
    if m:
        return Endpoint(m.group(1), PORT_VICP, "vicp")
    m = re.fullmatch(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", r, re.I)
    if m:
        return Endpoint(m.group(1), int(m.group(2)), "socket")
    m = re.fullmatch(r"TCPIP\d*::([^:]+)::hislip\d+(::INSTR)?", r, re.I)
    if m:
        return Endpoint(m.group(1), 4880, "hislip")
    m = re.fullmatch(r"TCPIP\d*::([^:]+)(::inst\d+)?(::INSTR)?", r, re.I)
    if m:
        return Endpoint(m.group(1), PORT_VXI11, "vxi11")
    return None


def tcp_probe(host: str, port: int, timeout_s: float = 1.5, attempts: int = 3) -> tuple[bool, float, str]:
    """TCP connect test. Returns (ok, best latency in ms, message)."""
    best = float("inf")
    last = ""
    for _ in range(attempts):
        t = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                best = min(best, (time.perf_counter() - t) * 1e3)
        except TimeoutError:
            last = "timeout (no answer: wrong IP, cable, or instrument off)"
        except ConnectionRefusedError:
            last = "connection refused (host is up but the port is closed: remote interface disabled?)"
        except OSError as exc:
            last = str(exc)
    if best < float("inf"):
        return True, best, f"TCP {port} open"
    return False, float("nan"), last


def local_address_towards(host: str) -> str | None:
    """IPv4 address of the laptop interface used to reach `host` (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return str(s.getsockname()[0])
    except OSError:
        return None


def same_subnet(a: str, b: str, prefix: int = 24) -> bool:
    try:
        return ipaddress.ip_address(b) in ipaddress.ip_network(f"{a}/{prefix}", strict=False)
    except ValueError:
        return False


def default_route_shared(instrument_host: str) -> bool | None:
    """NET-07: True if the interface towards the instruments is also the default-route interface
    (i.e. the HV bench LAN may be reachable from / bridged to another network)."""
    lab = local_address_towards(instrument_host)
    internet = local_address_towards("8.8.8.8")
    if lab is None or internet is None:
        return None
    return lab == internet and not ipaddress.ip_address(lab).is_loopback
