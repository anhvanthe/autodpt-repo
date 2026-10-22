"""Hardware-mode bench check through the real PyVISA (pyvisa-py) stack and real TCP sockets, against
simulated instruments served on localhost (NET-01, NET-02, NET-04, NET-08)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from autodpt.benchcheck import run_bench_check
from autodpt.config.models import InstrumentCfg, Station
from autodpt.hal.sim.instruments import AfgSim, LecroySim, MagnaSim
from autodpt.hal.transport import Responder

pytest.importorskip("pyvisa_py")


class LineServer:
    """Minimal SCPI-over-TCP server: newline-terminated commands, answers for queries."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    ans = self.responder.handle(line.decode("latin-1"))
                    if ans is not None and line.strip().endswith(b"?"):
                        conn.sendall((ans if isinstance(ans, bytes) else ans.encode()) + b"\n")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def servers() -> Iterator[dict[str, LineServer]]:
    s = {"psu": LineServer(MagnaSim()), "afg": LineServer(AfgSim(output_on=False)),
         "scope": LineServer(LecroySim(n_points=1000))}
    yield s
    for v in s.values():
        v.close()


@pytest.mark.req("NET-01", "NET-02", "NET-04", "NET-08", "NET-09")
def test_connect_level_over_real_visa_sockets(servers: dict[str, LineServer]) -> None:
    inst = {role: InstrumentCfg(model=role, resource=f"TCPIP0::127.0.0.1::{srv.port}::SOCKET", transport="socket",
                                timeout_ms=2000) for role, srv in servers.items()}
    st = Station(name="loopback", instruments=inst)  # type: ignore[arg-type]
    rep = run_bench_check(st, "connect", sim=None)
    fails = [r for r in rep.results if r.status == "FAIL"]
    assert not fails, fails
    for role in ("psu", "afg", "scope"):
        assert any(r.instrument == role and r.check.startswith("TCP reachability") and r.status == "PASS"
                   for r in rep.results)
        assert any(r.instrument == role and r.check.startswith("identify") and r.status == "PASS"
                   for r in rep.results)
    assert rep.info["psu_idn"]["model"].startswith("SL1000")  # type: ignore[index]


def test_unreachable_instrument_is_reported_not_hung() -> None:
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    inst = {role: InstrumentCfg(model=role, resource=f"TCPIP0::127.0.0.1::{port}::SOCKET", transport="socket",
                                timeout_ms=1000) for role in ("psu", "afg", "scope")}
    rep = run_bench_check(Station(name="dead", instruments=inst), "connect")  # type: ignore[arg-type]
    closed.close()
    assert rep.overall == "FAIL"
    tcp = [r for r in rep.results if r.check.startswith("TCP reachability")]
    assert len(tcp) == 3 and all(r.status == "FAIL" and "refused" in r.detail for r in tcp)
    assert all(r.status == "SKIP" for r in rep.results if r.check == "VISA open")
