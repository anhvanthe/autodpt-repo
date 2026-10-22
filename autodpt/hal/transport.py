"""Instrument transports (SRS ARC-03, NET-01, NET-04, DATA-04).

Every write/query is logged at DEBUG with the instrument name (event log). All I/O has timeouts.
`VisaTransport` uses PyVISA (pyvisa-py by default; NI-VISA + LeCroy VICP Passport for VICP).
`MockTransport` talks to an in-process simulator (simulation mode, NET-06).
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from autodpt.data.eventlog import get_logger


class TransportError(Exception):
    pass


class Responder(Protocol):
    def handle(self, command: str) -> str | bytes | None: ...


class Transport(Protocol):
    name: str

    def write(self, command: str) -> None: ...
    def query(self, command: str) -> str: ...
    def query_raw(self, command: str) -> bytes: ...
    def write_raw(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class VisaTransport:
    def __init__(self, name: str, resource: str, timeout_ms: int, visa_library: str = "@py",
                 read_termination: str | None = "\n", write_termination: str | None = "\n") -> None:
        import pyvisa  # local import: analysis-only installs need no VISA

        self.name = name
        self._log = get_logger(f"hal.{name}")
        self._res: Any = None
        try:
            self._rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
            self._res = self._rm.open_resource(resource, open_timeout=timeout_ms)
        except Exception as exc:  # pyvisa raises several types
            raise TransportError(f"{name}: cannot open {resource}: {exc}") from exc
        self._res.timeout = timeout_ms
        if read_termination is not None:
            self._res.read_termination = read_termination
        if write_termination is not None:
            self._res.write_termination = write_termination
        self._log.info("opened %s (library %s)", resource, visa_library or "default")

    def write(self, command: str) -> None:
        self._log.debug("> %s", command)
        try:
            self._res.write(command)
        except Exception as exc:
            raise TransportError(f"{self.name}: write '{command}' failed: {exc}") from exc

    def query(self, command: str) -> str:
        self._log.debug("> %s", command)
        try:
            ans = str(self._res.query(command)).strip()
        except Exception as exc:
            raise TransportError(f"{self.name}: query '{command}' failed: {exc}") from exc
        self._log.debug("< %s", ans)
        return ans

    def query_raw(self, command: str) -> bytes:
        self._log.debug("> %s (binary)", command)
        try:
            self._res.write(command)
            data = bytes(self._res.read_raw())
        except Exception as exc:
            raise TransportError(f"{self.name}: binary query '{command}' failed: {exc}") from exc
        self._log.debug("< %d bytes", len(data))
        return data

    def write_raw(self, data: bytes) -> None:
        self._log.debug("> %d bytes (binary)", len(data))
        try:
            self._res.write_raw(data)
        except Exception as exc:
            raise TransportError(f"{self.name}: binary write failed: {exc}") from exc

    def close(self) -> None:
        try:
            self._res.close()
        finally:
            self._log.info("closed")


class MockTransport:
    """In-process transport to a simulator; records every command (tests assert on it)."""

    def __init__(self, name: str, responder: Responder, latency_s: float = 0.0) -> None:
        self.name = name
        self.responder = responder
        self.latency_s = latency_s
        self.sent: list[str] = []
        self._log = get_logger(f"hal.{name}")

    def _do(self, command: str) -> str | bytes | None:
        if self.latency_s:
            time.sleep(self.latency_s)
        self.sent.append(command)
        self._log.debug("> %s", command if len(command) < 200 else command[:200] + "…")
        return self.responder.handle(command)

    def write(self, command: str) -> None:
        self._do(command)

    def query(self, command: str) -> str:
        ans = self._do(command)
        if ans is None:
            raise TransportError(f"{self.name}: timeout on '{command}'")
        return ans.decode("latin-1").strip() if isinstance(ans, bytes) else ans.strip()

    def query_raw(self, command: str) -> bytes:
        ans = self._do(command)
        if ans is None:
            raise TransportError(f"{self.name}: timeout on '{command}'")
        return ans if isinstance(ans, bytes) else ans.encode("latin-1")

    def write_raw(self, data: bytes) -> None:
        self._do(data.decode("latin-1"))

    def close(self) -> None:
        pass
