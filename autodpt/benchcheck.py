"""Bench check: connection and controllability of all instruments (SRS NET-02…NET-04, NET-07…NET-09).

Levels
------
* ``connect`` — read-only: laptop/network checks, TCP reachability, VISA open, *IDN? and supported-model
  check, error queues, round-trip latency, output-state report. Sends no setting commands.
* ``control`` — additionally proves that each instrument accepts and executes commands: write → read
  back → restore, on settings that are harmless with outputs off; AFG arbitrary-waveform upload with
  point-count read-back; scope waveform transfer, throughput and descriptor self-check.

Safety (SRS §2.4, SAF-08): at ``control`` level the AFG outputs and the PSU output are forced OFF first
and verified OFF at the end. The check never enables an output, never triggers the AFG and refuses
every command listed as ``forbidden`` in the command set (guard at transport level).
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from autodpt.config.models import InstrumentCfg, Station
from autodpt.data.eventlog import get_logger
from autodpt.hal.commands import ROLE_DEFAULT, CommandSet, load_command_set, parse_idn, parse_number
from autodpt.hal.lecroy import make_ieee_block, parse_ieee_block, parse_wavedesc
from autodpt.hal.netcheck import (
    default_route_shared,
    endpoint_from_resource,
    local_address_towards,
    same_subnet,
    tcp_probe,
)
from autodpt.hal.transport import MockTransport, Responder, Transport, TransportError, VisaTransport

Status = Literal["PASS", "WARN", "FAIL", "SKIP", "INFO"]
Level = Literal["connect", "control"]
_RANK = {"PASS": 0, "INFO": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
log = get_logger("bench")


class ForbiddenCommand(RuntimeError):
    pass


@dataclass
class CheckResult:
    instrument: str
    check: str
    status: Status
    detail: str
    ms: float = 0.0


@dataclass
class BenchReport:
    station: str
    level: str
    mode: str
    started: str
    results: list[CheckResult] = field(default_factory=list)
    info: dict[str, object] = field(default_factory=dict)

    def add(self, r: CheckResult) -> CheckResult:
        self.results.append(r)
        lvl = {"FAIL": 40, "WARN": 30}.get(r.status, 20)
        log.log(lvl, "[%s] %-28s %-4s %s", r.instrument, r.check, r.status, r.detail)
        return r

    @property
    def overall(self) -> Status:
        worst = max((_RANK[r.status] for r in self.results), default=0)
        return {0: "PASS", 1: "WARN", 2: "FAIL"}[worst]  # type: ignore[return-value]

    def to_json(self) -> str:
        return json.dumps({"station": self.station, "level": self.level, "mode": self.mode,
                           "started": self.started, "overall": self.overall, "info": self.info,
                           "results": [asdict(r) for r in self.results]}, indent=2, default=str)

    def to_markdown(self) -> str:
        lines = [f"# Bench check — {self.station}", "",
                 f"* Started: {self.started}  ", f"* Level: {self.level} ({self.mode})  ",
                 f"* **Overall: {self.overall}**", "", "| Instrument | Check | Status | Detail |",
                 "|---|---|---|---|"]
        for r in self.results:
            lines.append(f"| {r.instrument} | {r.check} | {r.status} | {r.detail.replace('|', '/')} |")
        return "\n".join(lines) + "\n"


class Guard:
    """Wraps a transport; refuses forbidden commands (HAL-level permission, SAF-01)."""

    def __init__(self, inner: Transport, cs: CommandSet) -> None:
        self.inner, self.cs, self.name = inner, cs, inner.name

    def _check(self, command: str) -> None:
        if self.cs.is_forbidden(command):
            raise ForbiddenCommand(f"{self.name}: '{command}' is not allowed in bench-check")

    def write(self, command: str) -> None:
        self._check(command)
        self.inner.write(command)

    def query(self, command: str) -> str:
        self._check(command)
        return self.inner.query(command)

    def query_raw(self, command: str) -> bytes:
        self._check(command)
        return self.inner.query_raw(command)

    def write_raw(self, data: bytes) -> None:
        self._check(data[:64].decode("latin-1", "replace"))
        self.inner.write_raw(data)

    def close(self) -> None:
        self.inner.close()


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------


def read_errors(t: Transport | Guard, cs: CommandSet, max_reads: int = 20) -> list[str]:
    """Drain the instrument's error queue. Empty list = no error."""
    errs: list[str] = []
    if cs.error_style == "lecroy":
        for key in ("cmr_q", "exr_q"):
            v = t.query(cs.fmt(key)).strip()
            try:
                if int(float(parse_number(v))) != 0:
                    errs.append(f"{cs.fmt(key)} = {v}")
            except ValueError:
                errs.append(f"{cs.fmt(key)} -> {v!r}")
        return errs
    for _ in range(max_reads):
        v = t.query(cs.fmt("error")).strip()
        try:
            code = int(float(v.split(",")[0]))
        except ValueError:
            errs.append(f"unparseable error response {v!r}")
            break
        if code == 0:
            break
        errs.append(v)
    return errs


def _close(a: float, b: float, rel: float, abs_: float) -> bool:
    return abs(a - b) <= max(abs_, rel * abs(b))


class _Runner:
    def __init__(self, rep: BenchReport, inst: str, t: Transport | Guard, cs: CommandSet) -> None:
        self.rep, self.inst, self.t, self.cs = rep, inst, t, cs

    def step(self, check: str, fn: Callable[[], tuple[Status, str]], errors: bool = True) -> Status:
        t0 = time.perf_counter()
        try:
            status, detail = fn()
            if errors:
                errs = read_errors(self.t, self.cs)
                if errs:
                    status, detail = "FAIL", f"{detail}; instrument reported: {'; '.join(errs)}"
        except ForbiddenCommand as exc:
            status, detail = "FAIL", f"BLOCKED: {exc}"
        except (TransportError, ValueError, KeyError) as exc:
            status, detail = "FAIL", str(exc)
        self.rep.add(CheckResult(self.inst, check, status, detail, (time.perf_counter() - t0) * 1e3))
        return status


# --------------------------------------------------------------------------------------------------
# per-instrument controllability
# --------------------------------------------------------------------------------------------------


def _psu_control(r: _Runner) -> dict[str, float]:
    t, cs = r.t, r.cs
    state: dict[str, float] = {}

    def outputs() -> tuple[Status, str]:
        if t.query(cs.fmt("output_q")).strip() in ("1", "ON"):
            t.write(cs.fmt("output_stop"))
            if t.query(cs.fmt("output_q")).strip() in ("1", "ON"):
                return "FAIL", "output is ON and did not stop — DO NOT continue; check the supply manually"
            return "WARN", "output was ON — stopped with OUTP:STOP (SAF-08); verify the DC link is discharged"
        return "PASS", "output OFF (standby)"

    out_ok = r.step("output forced/verified OFF", outputs)

    def status_regs() -> tuple[Status, str]:
        q = int(parse_number(t.query(cs.fmt("ques_cond"))))
        active = [name for bit, name in cs.ques_bits.items() if q & (1 << bit)]
        if not active:
            return "PASS", f"STAT:QUES:COND = {q} (no faults)"
        if active == ["INTERLOCK"]:
            return "WARN", "interlock input open — expected while the enclosure is open, must close before HV"
        return "WARN", f"active: {', '.join(active)} (clear with OUTP:PROT:CLE after fixing the cause)"

    r.step("status / interlock", status_regs)

    def protections() -> tuple[Status, str]:
        ovt = parse_number(t.query(cs.fmt("ovt_q")))
        oct_ = parse_number(t.query(cs.fmt("oct_q")))
        state.update(ovt=ovt, oct=oct_)
        return "PASS", f"OVT {ovt:g} V, OCT {oct_:g} A (read only)"

    r.step("protection trip levels", protections)
    if out_ok == "FAIL":
        r.rep.add(CheckResult(r.inst, "setpoint write/read-back", "SKIP", "output not confirmed OFF"))
        return state

    def setpoints() -> tuple[Status, str]:
        v0 = parse_number(t.query(cs.fmt("volt_q")))
        i0 = parse_number(t.query(cs.fmt("curr_q")))
        v_test, i_test = 10.0, 0.10
        t.write(cs.fmt("volt_set", value=f"{v_test:.3f}"))
        t.write(cs.fmt("curr_set", value=f"{i_test:.4f}"))
        v1 = parse_number(t.query(cs.fmt("volt_q")))
        i1 = parse_number(t.query(cs.fmt("curr_q")))
        t.write(cs.fmt("volt_set", value=f"{v0:.4f}"))
        t.write(cs.fmt("curr_set", value=f"{i0:.4f}"))
        v2 = parse_number(t.query(cs.fmt("volt_q")))
        i2 = parse_number(t.query(cs.fmt("curr_q")))
        ok = _close(v1, v_test, 0.01, 0.5) and _close(i1, i_test, 0.01, 0.01)
        restored = _close(v2, v0, 0.01, 0.5) and _close(i2, i0, 0.01, 0.01)
        detail = (f"VOLT 10 V → {v1:g}, CURR 0.1 A → {i1:g}; restored to {v2:g} V / {i2:g} A "
                  f"(output stayed OFF)")
        if not ok:
            return "FAIL", "read-back mismatch: " + detail
        return ("PASS" if restored else "WARN"), detail

    r.step("setpoint write/read-back", setpoints)

    def measure() -> tuple[Status, str]:
        v = parse_number(t.query(cs.fmt("meas_volt")))
        state["v_meas"] = v
        if abs(v) > 50.0:
            return "WARN", f"MEAS:VOLT = {v:.1f} V with output OFF — DC link charged? (informational, SAF-04)"
        return "PASS", f"MEAS:VOLT = {v:.2f} V"

    r.step("voltage measurement", measure)

    def final_off() -> tuple[Status, str]:
        on = t.query(cs.fmt("output_q")).strip() in ("1", "ON")
        return ("FAIL", "output ON at end of check!") if on else ("PASS", "output OFF")

    r.step("output OFF at end", final_off)
    return state


def _afg_control(r: _Runner, ch: str, load: str, logic_high: float = 5.0) -> None:
    t, cs = r.t, r.cs
    other = "2" if ch == "1" else "1"

    def outputs_off() -> tuple[Status, str]:
        was = {c: t.query(cs.fmt("output_q", ch=c)).strip() for c in ("1", "2")}
        for c in ("1", "2"):
            t.write(cs.fmt("output_off", ch=c))
        now = {c: t.query(cs.fmt("output_q", ch=c)).strip() for c in ("1", "2")}
        if any(v in ("1", "ON") for v in now.values()):
            return "FAIL", f"output still ON after OFF command: {now}"
        if any(v in ("1", "ON") for v in was.values()):
            return "WARN", f"outputs were ON ({was}) — switched OFF (SAF-08)"
        return "PASS", "CH1/CH2 outputs OFF"

    if r.step("outputs forced/verified OFF", outputs_off) == "FAIL":
        r.rep.add(CheckResult(r.inst, "controllability", "SKIP", "outputs not confirmed OFF"))
        return

    def impedance() -> tuple[Status, str]:
        t.write(cs.fmt("imp_set", ch=ch, value=load))
        ans = t.query(cs.fmt("imp_q", ch=ch))
        v = float("inf") if "INF" in ans.upper() else parse_number(ans)
        ok = v > 1e6 if load == "INF" else _close(v, 50.0, 0.01, 0.1)
        msg = f"CH{ch} load set {load} → read {v:g}"
        return ("PASS", msg) if ok else ("FAIL", msg + " (level at the driver would be wrong, AFG-03)")

    r.step("load impedance (AFG-03)", impedance)

    def levels() -> tuple[Status, str]:
        t.write(cs.fmt("high_set", ch=ch, value=f"{logic_high:g}"))
        t.write(cs.fmt("low_set", ch=ch, value="0"))
        hi = parse_number(t.query(cs.fmt("high_q", ch=ch)))
        lo = parse_number(t.query(cs.fmt("low_q", ch=ch)))
        ok = _close(hi, logic_high, 0.01, 0.01) and _close(lo, 0.0, 0.0, 0.01)
        msg = f"CH{ch} HIGH {hi:g} V / LOW {lo:g} V (logic levels into {load})"
        return ("PASS", msg) if ok else ("FAIL", msg)

    r.step("logic levels 0/5 V", levels)

    def burst() -> tuple[Status, str]:
        t.write(cs.fmt("func_set", ch=ch, value=f"EMEM{ch}"))
        t.write(cs.fmt("burst_mode", ch=ch, value="TRIG"))
        t.write(cs.fmt("burst_ncyc", ch=ch, value="1"))
        t.write(cs.fmt("burst_state", ch=ch, value="ON"))
        f = t.query(cs.fmt("func_q", ch=ch)).strip().upper()
        n = parse_number(t.query(cs.fmt("burst_ncyc_q", ch=ch)))
        s = t.query(cs.fmt("burst_state_q", ch=ch)).strip().upper()
        ok = f.startswith("EMEM") and n == 1 and s in ("1", "ON")
        msg = f"FUNC {f}, BURS:NCYC {n:g}, BURS:STAT {s}"
        return ("PASS", msg) if ok else ("FAIL", msg)

    r.step("ARB function + burst ×1", burst)

    def arb() -> tuple[Status, str]:
        n = 1000
        hi = cs.arb_max_code or 16383
        wave = np.zeros(n, dtype=">u2")
        wave[100:600] = hi     # test double pulse: long first pulse, gap, short second pulse
        wave[700:800] = hi
        block = make_ieee_block(wave.tobytes())
        t.write_raw(cs.fmt("arb_upload", ch=ch).encode("latin-1") + block + b"\n")
        pts = int(parse_number(t.query(cs.fmt("arb_points_q", ch=ch))))
        msg = f"uploaded {n}-point test double pulse to EMEM{ch}, read back {pts} points"
        return ("PASS", msg) if pts == n else ("FAIL", msg)

    r.step("arbitrary-waveform upload", arb)

    def final_off() -> tuple[Status, str]:
        now = {c: t.query(cs.fmt("output_q", ch=c)).strip() for c in (ch, other)}
        bad = [c for c, v in now.items() if v in ("1", "ON")]
        return ("FAIL", f"output ON at end: CH{bad}") if bad else ("PASS", "outputs OFF")

    r.step("outputs OFF at end", final_off)


def _scope_control(r: _Runner, ch: str, acquire: bool) -> None:
    t, cs = r.t, r.cs
    ch = ch.upper().lstrip("C")

    def setup() -> tuple[Status, str]:
        for key in ("chdr_off", "cfmt", "cord"):
            t.write(cs.fmt(key))
        return "PASS", "CHDR OFF, CFMT DEF9,WORD,BIN, CORD LO accepted"

    r.step("communication format", setup)

    def timebase() -> tuple[Status, str]:
        tb0 = parse_number(t.query(cs.fmt("tdiv_q")))
        tb1 = tb0 * 2 if tb0 < 1 else tb0 / 2
        t.write(cs.fmt("tdiv_set", value=f"{tb1:.3E}"))
        rb = parse_number(t.query(cs.fmt("tdiv_q")))
        t.write(cs.fmt("tdiv_set", value=f"{tb0:.3E}"))
        rb0 = parse_number(t.query(cs.fmt("tdiv_q")))
        ok, restored = _close(rb, tb1, 0.02, 0), _close(rb0, tb0, 0.02, 0)
        msg = f"TDIV {tb0:.3g} → set {tb1:.3g} → read {rb:.3g}; restored {rb0:.3g}"
        if not ok:
            return "FAIL", msg
        return ("PASS" if restored else "WARN"), msg

    r.step("timebase write/read-back", timebase)

    if acquire:
        def single_shot() -> tuple[Status, str]:
            mode0 = t.query(cs.fmt("trmd_q")).strip()
            t.query(cs.fmt("inr_q"))  # clear
            t.write(cs.fmt("trmd_set", value="SINGLE"))
            t.write(cs.fmt("arm"))
            t.write(cs.fmt("force_trig"))
            t.write(cs.fmt("wait", value="5"))
            t.query(cs.fmt("opc_q"))
            inr = int(parse_number(t.query(cs.fmt("inr_q"))))
            if mode0.upper() in ("AUTO", "NORM", "SINGLE", "STOP"):
                t.write(cs.fmt("trmd_set", value=mode0.upper()))
            msg = f"TRMD SINGLE, ARM, FRTR → INR bit0 = {inr & 1}; trigger mode restored to {mode0}"
            return ("PASS", msg) if inr & 1 else ("FAIL", msg + " (no acquisition)")

        r.step("single-shot arm + force trigger", single_shot)

    def transfer() -> tuple[Status, str]:
        t0 = time.perf_counter()
        raw = r.t.query_raw(cs.fmt("wf_all", ch=ch))
        dt_s = time.perf_counter() - t0
        wf = parse_wavedesc(parse_ieee_block(raw))
        n = int(wf.desc["WAVE_ARRAY_COUNT"])
        rate = len(raw) / dt_s / 1e6 if dt_s > 0 else float("inf")
        r.rep.info["scope_transfer_MBps"] = round(rate, 2)
        est = 8 * 1_000_000 * 2 / (rate * 1e6) if rate > 0 else float("nan")
        msg = (f"C{ch}: {n} points, {len(raw) / 1e6:.2f} MB in {dt_s * 1e3:.0f} ms = {rate:.1f} MB/s; "
               f"8 ch × 1 Mpts would take ≈ {est:.1f} s")
        if n == 0:
            return "WARN", msg + " — channel has no data, re-run with --acquire"
        if rate < 8.0:
            return "WARN", msg + " — consistent with a 100 Mbit/s link (NFR-PERF-03)"
        return "PASS", msg

    r.step("waveform transfer + throughput", transfer)

    def descriptor() -> tuple[Status, str]:
        wf = parse_wavedesc(parse_ieee_block(r.t.query_raw(cs.fmt("wf_all", ch=ch))))
        g_txt = parse_number(t.query(cs.fmt("inspect", ch=ch, value="VERTICAL_GAIN")))
        h_txt = parse_number(t.query(cs.fmt("inspect", ch=ch, value="HORIZ_INTERVAL")))
        g_bin, h_bin = float(wf.desc["VERTICAL_GAIN"]), float(wf.desc["HORIZ_INTERVAL"])
        ok = _close(g_bin, g_txt, 1e-3, 0) and _close(h_bin, h_txt, 1e-3, 0)
        msg = (f"binary VERTICAL_GAIN {g_bin:.6g} vs INSPECT {g_txt:.6g}; HORIZ_INTERVAL {h_bin:.6g} vs "
               f"{h_txt:.6g}")
        return ("PASS", msg) if ok else ("FAIL", msg + " — WAVEDESC offsets wrong for this firmware")

    r.step("WAVEDESC parser self-check", descriptor)


def _dcmon_control(r: _Runner, v_psu: float | None) -> None:
    t, cs = r.t, r.cs

    def measure() -> tuple[Status, str]:
        v = parse_number(t.query(cs.fmt("meas_vdc")))
        r.rep.info["dclink_v_dmm"] = v
        if v_psu is None:
            return "PASS", f"DC link {v:.2f} V"
        ok = _close(v, v_psu, 0.01, 2.0)
        msg = f"DMM {v:.2f} V vs PSU read-back {v_psu:.2f} V"
        return ("PASS", msg) if ok else ("WARN", msg + " — disagree: check monitor wiring (SAF-12, App. F)")

    r.step("DC-link measurement", measure)


# --------------------------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------------------------

ROLE_ORDER = ("afg", "psu", "scope", "dcmon")


def run_bench_check(station: Station, level: Level = "connect", sim: Mapping[str, Responder] | None = None,
                    acquire: bool = False, latency_reads: int = 10) -> BenchReport:
    rep = BenchReport(station=station.name, level=level, mode="simulation" if sim is not None else "hardware",
                      started=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    roles = [r for r in ROLE_ORDER if r in station.instruments]
    transports: dict[str, Guard] = {}
    sets: dict[str, CommandSet] = {}

    # ---- laptop / network ---------------------------------------------------------------------------
    if sim is None:
        hosts = {}
        for role in roles:
            cs = load_command_set(station.instruments[role].command_set or ROLE_DEFAULT[role])
            ep = endpoint_from_resource(station.instruments[role].resource, cs.default_socket_port)
            if ep:
                hosts[role] = ep
        if hosts:
            first = next(iter(hosts.values())).host
            local = local_address_towards(first)
            rep.info["laptop_ip"] = local
            off = [f"{r}={e.host}" for r, e in hosts.items() if local and not same_subnet(local, e.host)]
            rep.add(CheckResult("laptop", "interface / subnet", "WARN" if off or not local else "PASS",
                                f"laptop {local} ; outside its /24: {', '.join(off)}" if off
                                else f"laptop {local}, all instruments in the same /24"))
            shared = default_route_shared(first)
            if shared:
                rep.add(CheckResult("laptop", "network isolation (NET-07)", "WARN",
                                    "the instrument LAN is also the default route — OK only if the router's "
                                    "WAN port is unplugged and Wi-Fi is off"))
            elif shared is False:
                rep.add(CheckResult("laptop", "network isolation (NET-07)", "PASS",
                                    "instrument LAN is not the default route"))
        for role, ep in hosts.items():
            ok, ms, msg = tcp_probe(ep.host, ep.port)
            rep.add(CheckResult(role, f"TCP reachability ({ep.kind})", "PASS" if ok else "FAIL",
                                f"{ep.host}:{ep.port} {msg}" + (f", {ms:.1f} ms" if ok else ""), ms if ok else 0))
    else:
        rep.add(CheckResult("laptop", "network", "SKIP", "simulation mode — no network used"))

    # ---- open + identify --------------------------------------------------------------------------
    for role in roles:
        icfg: InstrumentCfg = station.instruments[role]
        cs = load_command_set(icfg.command_set or ROLE_DEFAULT[role])
        sets[role] = cs
        if any(r.instrument == role and r.status == "FAIL" for r in rep.results):
            rep.add(CheckResult(role, "VISA open", "SKIP", "not reachable"))
            continue
        t0 = time.perf_counter()
        try:
            raw_t: Transport
            if sim is not None:
                raw_t = MockTransport(role, sim[role])
            else:
                raw_t = VisaTransport(role, icfg.resource, icfg.timeout_ms, station.visa_library,
                                      icfg.read_termination, icfg.write_termination)
            transports[role] = Guard(raw_t, cs)
            rep.add(CheckResult(role, "VISA open", "PASS", icfg.resource, (time.perf_counter() - t0) * 1e3))
        except (TransportError, ImportError) as exc:
            hint = " (VICP needs NI-VISA + LeCroy VICP Passport; or select LXI/VXI-11 on the scope)" \
                if "VICP" in icfg.resource.upper() else ""
            rep.add(CheckResult(role, "VISA open", "FAIL", f"{exc}{hint}"))
            continue
        tr = transports[role]
        run = _Runner(rep, role, tr, cs)

        def identify(tr: Guard = tr, cs: CommandSet = cs, icfg: InstrumentCfg = icfg, role: str = role
                     ) -> tuple[Status, str]:
            idn = parse_idn(tr.query(cs.fmt("idn")))
            rep.info[f"{role}_idn"] = idn
            text = f"{idn['vendor']} {idn['model']} s/n {idn['serial']} fw {idn['firmware']}"
            if not cs.idn_match.matches(idn["vendor"], idn["model"]):
                return "FAIL", f"{text} — does not match command set '{cs.name}' / configured {icfg.model}"
            return "PASS", text

        if run.step("identify (*IDN?, NET-02/03)", identify, errors=False) == "FAIL":
            continue
        try:
            pre = read_errors(tr, cs)
            rep.add(CheckResult(role, "error queue", "WARN" if pre else "PASS",
                                f"pre-existing: {'; '.join(pre)}" if pre else "empty"))
        except TransportError as exc:
            rep.add(CheckResult(role, "error queue", "FAIL", str(exc)))
        times = []
        try:
            for _ in range(latency_reads):
                t1 = time.perf_counter()
                tr.query(cs.fmt("idn"))
                times.append((time.perf_counter() - t1) * 1e3)
            med = statistics.median(times)
            rep.add(CheckResult(role, "round-trip latency", "PASS" if med < 100 else "WARN",
                                f"median {med:.1f} ms, max {max(times):.1f} ms over {latency_reads} queries", med))
        except TransportError as exc:
            rep.add(CheckResult(role, "round-trip latency", "FAIL", str(exc)))
        unv = cs.unverified()
        if unv:
            rep.add(CheckResult(role, "command set", "INFO",
                                f"'{cs.name}': {len(unv)} commands not yet confirmed in the manual — "
                                f"confirmed by the control checks: {', '.join(unv)}"))
        if level == "connect":
            _report_output_state(rep, role, tr, cs)

    # ---- controllability ----------------------------------------------------------------------------
    if level == "control":
        v_psu: float | None = None
        for role in roles:
            if role not in transports or any(r.instrument == role and r.check.startswith("identify")
                                             and r.status == "FAIL" for r in rep.results):
                continue
            run = _Runner(rep, role, transports[role], sets[role])
            if role == "afg":
                _afg_control(run, station.instruments["afg"].test_channel, station.afg_load)
            elif role == "psu":
                v_psu = _psu_control(run).get("v_meas")
            elif role == "scope":
                _scope_control(run, station.instruments["scope"].test_channel, acquire)
            elif role == "dcmon":
                _dcmon_control(run, v_psu)

    for tr in transports.values():
        tr.close()
    return rep


def _report_output_state(rep: BenchReport, role: str, tr: Guard, cs: CommandSet) -> None:
    """Read-only output report at connect level."""
    try:
        if role == "psu":
            on = tr.query(cs.fmt("output_q")).strip() in ("1", "ON")
            rep.add(CheckResult(role, "output state", "WARN" if on else "PASS",
                                "output ON (read-only level: not changed)" if on else "output OFF"))
        elif role == "afg":
            st = {c: tr.query(cs.fmt("output_q", ch=c)).strip() for c in ("1", "2")}
            on_ch = [c for c, v in st.items() if v in ("1", "ON")]
            rep.add(CheckResult(role, "output state", "WARN" if on_ch else "PASS",
                                f"CH{','.join(on_ch)} ON (read-only level: not changed)" if on_ch else "CH1/CH2 OFF"))
    except TransportError as exc:
        rep.add(CheckResult(role, "output state", "FAIL", str(exc)))


def write_reports(rep: BenchReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    j = out_dir / f"bench_check_{stamp}.json"
    m = out_dir / f"bench_check_{stamp}.md"
    j.write_text(rep.to_json(), encoding="utf-8")
    m.write_text(rep.to_markdown(), encoding="utf-8")
    return j, m


def sim_bench(**faults: object) -> dict[str, Responder]:
    """Default simulated bench (all instruments healthy, AFG outputs initially ON to exercise SAF-08)."""
    from autodpt.hal.sim.instruments import AfgSim, DmmSim, LecroySim, MagnaSim

    bench: dict[str, Responder] = {"afg": AfgSim(), "psu": MagnaSim(), "scope": LecroySim(n_points=100_000),
                                   "dcmon": DmmSim()}
    bench.update(faults)  # type: ignore[arg-type]
    return bench
