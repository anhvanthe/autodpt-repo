"""Bench check: connection and controllability (NET-02…NET-09, SAF-01, SAF-08, AFG-03, SCO-05)."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np
import pytest

from autodpt.app import main
from autodpt.benchcheck import BenchReport, ForbiddenCommand, Guard, run_bench_check, sim_bench
from autodpt.config import Station, load_model
from autodpt.hal.commands import ROLE_DEFAULT, load_command_set, parse_idn, parse_number
from autodpt.hal.lecroy import BlockError, build_wavedesc, make_ieee_block, parse_ieee_block, parse_wavedesc
from autodpt.hal.netcheck import endpoint_from_resource, tcp_probe
from autodpt.hal.sim.instruments import AfgSim, Faults, LecroySim, MagnaSim
from autodpt.hal.transport import MockTransport

STATION = Path(__file__).resolve().parents[2] / "config" / "examples" / "station.yaml"


def _station() -> Station:
    return load_model(STATION, Station)


def _by(rep: BenchReport, inst: str, check_prefix: str) -> list[str]:
    return [r.status for r in rep.results if r.instrument == inst and r.check.startswith(check_prefix)]


@pytest.mark.req("NET-02", "NET-03", "NET-04", "NET-06", "NET-09")
def test_simulated_bench_control_level_passes() -> None:
    bench = sim_bench()
    rep = run_bench_check(_station(), "control", sim=bench, acquire=True)
    assert rep.overall == "WARN"  # only because the simulated AFG starts with outputs ON
    assert not [r for r in rep.results if r.status == "FAIL"]
    assert _by(rep, "afg", "arbitrary-waveform upload") == ["PASS"]
    assert _by(rep, "scope", "WAVEDESC parser self-check") == ["PASS"]
    assert _by(rep, "psu", "setpoint write/read-back") == ["PASS"]


@pytest.mark.req("SAF-08", "SAF-01")
def test_outputs_forced_off_and_never_enabled() -> None:
    bench = sim_bench()
    run_bench_check(_station(), "control", sim=bench, acquire=True)
    afg, psu = bench["afg"], bench["psu"]
    assert isinstance(afg, AfgSim) and isinstance(psu, MagnaSim)
    assert afg.out == {"1": False, "2": False} and not psu.output_on
    sent = [c.upper() for c in afg.received + psu.received]
    assert not any(c.startswith(("OUTP:START", "*TRG")) or (c.endswith("STAT ON") and c.startswith("OUTP"))
                   for c in sent)
    assert psu.v_set == 0.0 and psu.i_set == 0.0  # setpoints restored


@pytest.mark.req("SAF-01")
def test_guard_blocks_forbidden_commands() -> None:
    g = Guard(MockTransport("psu", MagnaSim()), load_command_set("magna_sl"))
    with pytest.raises(ForbiddenCommand):
        g.write("OUTP:START")
    g2 = Guard(MockTransport("afg", AfgSim()), load_command_set("tek_afg31000"))
    for cmd in ("OUTP1:STAT ON", "*TRG"):
        with pytest.raises(ForbiddenCommand):
            g2.write(cmd)
    g2.write("OUTP1:STAT OFF")  # allowed


@pytest.mark.req("NET-09")
def test_connect_level_is_read_only() -> None:
    bench = sim_bench()
    rep = run_bench_check(_station(), "connect", sim=bench)
    for name in ("afg", "psu", "dcmon"):
        sim = bench[name]
        sent = sim.received  # type: ignore[attr-defined]
        assert all(c.strip().endswith("?") for c in sent), (name, [c for c in sent if not c.endswith("?")])
    assert _by(rep, "afg", "output state") == ["WARN"]  # reported, not changed
    assert bench["afg"].out["1"]  # type: ignore[attr-defined]


@pytest.mark.req("SAF-08")
def test_psu_output_on_is_stopped_and_reported() -> None:
    rep = run_bench_check(_station(), "control", sim=sim_bench(psu=MagnaSim(output_on=True)))
    assert _by(rep, "psu", "output forced") == ["WARN"]
    assert _by(rep, "psu", "setpoint") == ["PASS"]


def test_psu_that_does_not_stop_blocks_setpoint_tests() -> None:
    stuck = MagnaSim(output_on=True, faults=Faults(unknown_commands={"OUTP:STOP"}))
    rep = run_bench_check(_station(), "control", sim=sim_bench(psu=stuck))
    assert _by(rep, "psu", "output forced") == ["FAIL"]
    assert _by(rep, "psu", "setpoint") == ["SKIP"]
    assert rep.overall == "FAIL"


@pytest.mark.req("NET-04")
def test_rejected_command_reports_instrument_error() -> None:
    afg = AfgSim(faults=Faults(unknown_commands={"OUTP1:IMP INF"}))
    rep = run_bench_check(_station(), "control", sim=sim_bench(afg=afg))
    r = next(r for r in rep.results if r.check.startswith("load impedance"))
    assert r.status == "FAIL" and "Undefined header" in r.detail


@pytest.mark.req("NET-03")
def test_wrong_instrument_at_address() -> None:
    afg = AfgSim(faults=Faults(idn="KEYSIGHT,33622A,X,1.0"))
    rep = run_bench_check(_station(), "control", sim=sim_bench(afg=afg))
    assert _by(rep, "afg", "identify") == ["FAIL"]
    assert not _by(rep, "afg", "logic levels")  # no control on an unidentified instrument


def test_readback_mismatch_fails() -> None:
    rep = run_bench_check(_station(), "control", sim=sim_bench(psu=MagnaSim(faults=Faults(readback_offset=3.0))))
    assert _by(rep, "psu", "setpoint") == ["FAIL"]


def test_interlock_open_is_a_warning() -> None:
    rep = run_bench_check(_station(), "control", sim=sim_bench(psu=MagnaSim(ques=1 << 8)))
    r = next(r for r in rep.results if r.check.startswith("status / interlock"))
    assert r.status == "WARN" and "interlock" in r.detail


def test_scope_timeout_on_transfer() -> None:
    scope = LecroySim(faults=Faults(no_answer={"C1:WF? ALL"}), n_points=1000)
    rep = run_bench_check(_station(), "control", sim=sim_bench(scope=scope))
    assert _by(rep, "scope", "waveform transfer") == ["FAIL"]


@pytest.mark.req("NET-01", "NET-08")
def test_endpoint_parsing() -> None:
    e = endpoint_from_resource("TCPIP0::192.168.0.12::50505::SOCKET")
    assert e is not None and (e.host, e.port, e.kind) == ("192.168.0.12", 50505, "socket")
    e = endpoint_from_resource("TCPIP0::192.168.0.10::inst0::INSTR")
    assert e is not None and (e.port, e.kind) == (111, "vxi11")
    e = endpoint_from_resource("VICP::192.168.0.10")
    assert e is not None and (e.port, e.kind) == (1861, "vicp")
    assert endpoint_from_resource("USB0::0x0699::0x0356::C0001::INSTR") is None


@pytest.mark.req("NET-08")
def test_tcp_probe_open_and_closed() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    threading.Thread(target=srv.accept, daemon=True).start()
    ok, ms, _ = tcp_probe("127.0.0.1", port, attempts=1)
    assert ok and ms >= 0
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))      # bound but not listening -> connection refused
    cport = closed.getsockname()[1]
    ok, _, msg = tcp_probe("127.0.0.1", cport, timeout_s=0.5, attempts=1)
    closed.close()
    srv.close()
    assert not ok and msg


@pytest.mark.req("SCO-05")
@pytest.mark.parametrize("little", [True, False])
def test_wavedesc_roundtrip(little: bool) -> None:
    codes = (np.arange(-500, 500) * 13).astype(np.int16)
    payload = build_wavedesc(codes, gain=2.5e-4, offset=0.125, dt=1e-10, t0=-2e-6, little_endian=little)
    wf = parse_wavedesc(parse_ieee_block(b"ALL," + make_ieee_block(payload) + b"\n"))
    assert np.array_equal(wf.codes, codes) and wf.dt == pytest.approx(1e-10)
    assert wf.t0 == pytest.approx(-2e-6)
    raw = wf.to_raw_channel("VCE_DUT")
    assert np.allclose(raw.codes * raw.gain + raw.offset, wf.volts())
    assert np.allclose(wf.volts(), codes * np.float32(2.5e-4) - np.float32(0.125))
    with pytest.raises(BlockError):
        parse_ieee_block(make_ieee_block(payload)[:-10])


def test_command_sets_load_and_format() -> None:
    for name in set(ROLE_DEFAULT.values()):
        cs = load_command_set(name)
        for key in cs.commands:
            assert cs.fmt(key, ch="1", value="1")  # every template formats
    assert parse_idn("TEKTRONIX,AFG31022,C010101,FV:1.6.3")["model"] == "AFG31022"
    assert parse_number('"VERTICAL_GAIN       : 1.5259e-004"') == pytest.approx(1.5259e-4)
    assert parse_number("TDIV 5.00E-06 S") == pytest.approx(5e-6)


@pytest.mark.req("NET-09")
def test_cli_bench_check_sim(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["bench-check", str(STATION), "--sim", "--level", "control", "--acquire",
               "--report-dir", str(tmp_path)])
    assert rc == 0
    assert "OVERALL: WARN" in capsys.readouterr().out
    assert list(tmp_path.glob("bench_check_*.json")) and list(tmp_path.glob("bench_check_*.md"))
