"""Command-line entry point (Phase 0/1). The GUI (PySide6) arrives in Phase 5.

Examples
--------
    python -m autodpt --sim                       # synthetic IGBT shot → analysis vs truth table
    python -m autodpt --sim --device sic --skew 4.7 --noise
    python -m autodpt presets                     # list threshold presets
    python -m autodpt bench-check config/examples/station.yaml                   # connection (read-only)
    python -m autodpt bench-check config/examples/station.yaml --level control   # + controllability
    python -m autodpt check-config config/examples/setup_fs820_hs_400v.yaml \
        --probes config/examples/probes.yaml
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from autodpt import __version__
from autodpt.data.eventlog import get_logger, setup_logging

_UNITS = {  # display scale and unit (template units)
    "e_on": (1e6, "uJ"), "e_off": (1e6, "uJ"), "e_rec": (1e6, "uJ"),
    "td_on": (1e9, "ns"), "t_r": (1e9, "ns"), "td_off": (1e9, "ns"), "t_f": (1e9, "ns"), "t_z": (1e9, "ns"),
    "t_rr": (1e9, "ns"), "t_a": (1e9, "ns"), "t_b": (1e9, "ns"), "q_rr": (1e9, "nC"),
    "i_rrm": (1, "A"), "softness": (1, "-"), "i_on": (1, "A"), "i_off": (1, "A"),
    "vdc_on": (1, "V"), "vdc_off": (1, "V"), "vce_peak_off": (1, "V"), "overshoot_dv": (1, "V"),
    "ic_peak_on": (1, "A"), "didt_on": (1e-9, "A/ns"), "didt_off": (1e-9, "A/ns"),
    "dvdt_on": (1e-9, "V/ns"), "dvdt_off": (1e-9, "V/ns"), "l_sigma_on": (1e9, "nH"),
    "comp_vce_max_on": (1, "V"), "comp_vge_max_on": (1, "V"), "comp_vge_min_off": (1, "V"),
}


def _cmd_sim(args: argparse.Namespace) -> int:
    from autodpt.analysis import synth
    from autodpt.analysis.analyse import analyse_shot
    from autodpt.analysis.preprocess import waveforms_from_raw
    from autodpt.config.models import AnalysisConfig
    from autodpt.config.presets import resolve_preset

    log = get_logger("ana")
    if args.device == "igbt":
        dev, circ = synth.igbt_device(), synth.SynthCircuit()
    else:
        dev, circ = synth.sic_device(), synth.SynthCircuit(v_dc=600.0, droop=6.0, l_sigma=15e-9, i_set=300.0)
    cfg = AnalysisConfig(preset=args.preset)
    ts = synth.generate_true(dev, circ, synth.SynthTiming())
    preset = resolve_preset(cfg.preset, "IGBT" if dev.dut_type == "IGBT" else "MOSFET")
    truth = synth.truth(ts, preset, cfg)
    tau = args.skew * 1e-9
    noise = {}
    if args.noise:
        noise = {"VCE_DUT": 0.005 * circ.v_dc, "IC_DUT": 0.005 * circ.i_set, "VGE_DUT": 0.1,
                 "VCE_COMP": 0.005 * circ.v_dc, "VGE_COMP": 0.1}
    raw, dt, t0 = synth.acquire(ts, synth.SynthAcq(bits=12, skew={"IC_DUT": tau}, noise_rms=noise))
    wf = waveforms_from_raw(raw, dt, t0)
    res = analyse_shot(wf, synth.meta_for(ts, single_skew={"IC_DUT": tau}), cfg, preset)
    log.info("simulated %s shot: V=%.0f V, I=%.0f A, skew=%.2f ns, noise=%s, preset=%s",
             dev.name, circ.v_dc, circ.i_set, args.skew, bool(noise), preset.name)

    methods = [m for m in ("none", "single", "statistic") if m in res.values]
    print(f"\nAutoDPT {__version__} - simulation | {dev.name} | preset {preset.name} "
          f"(verified={preset.verified}) | validity {res.validity}")
    if res.skew_estimate:
        e = res.skew_estimate
        print(f"statistic skew estimate: {e.tau * 1e9:+.2f} ns (true {args.skew:+.2f} ns), ok={e.ok}, "
              f"L_sigma={e.l_sigma * 1e9:.1f} nH, fit_error={e.fit_error:.3f}")
    print(f"flags: {', '.join(res.flags) or '-'}\n")
    hdr = f"{'parameter':16s}{'unit':>6s}{'truth':>12s}" + "".join(f"{m:>12s}{'err%':>8s}" for m in methods)
    print(hdr)
    print("-" * len(hdr))
    for key, (scale, unit) in _UNITS.items():
        if key not in truth:
            continue
        line = f"{key:16s}{unit:>6s}{truth[key] * scale:12.4g}"
        for m in methods:
            v = res.values[m].get(key, math.nan)
            err = (v / truth[key] - 1) * 100 if truth[key] else math.nan
            line += f"{v * scale:12.4g}{err:8.2f}"
        print(line)
    return 0 if res.validity != "INVALID" else 1


def _cmd_presets(_: argparse.Namespace) -> int:
    from autodpt.config.presets import available_presets, load_preset

    for name in available_presets():
        p = load_preset(name)
        print(f"{p.name:12s} {p.device_type:7s} verified={p.verified!s:5s} {p.standard}")
    return 0


def _cmd_check_config(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from autodpt.config import ProbeLibrary, Setup, load_model

    try:
        setup = load_model(args.setup, Setup)
        c_mf = setup.fixture.c_dclink_f * 1e3
        print(f"setup '{setup.name}': OK - {setup.matrix.n_points()} matrix points, "
              f"DUT {setup.dut.part_number} ({setup.dut.dut_type}), fixture C={c_mf:g} mF")
        v = max(setup.matrix.vdc_list)
        print(f"  stored energy at {v:g} V: {setup.fixture.stored_energy_j(v):.0f} J; bleed-down to "
              f"{setup.limits.v_safe:g} V: {setup.fixture.bleed_time_s(v, setup.limits.v_safe):.0f} s "
              f"(relay closed: {setup.fixture.bleed_time_s(v, setup.limits.v_safe, True):.1f} s)")
        if args.probes:
            for w in setup.check_probes(load_model(args.probes, ProbeLibrary)):
                print(f"  WARNING: {w}")
        return 0
    except (ValidationError, KeyError, ValueError) as exc:
        print(f"INVALID configuration:\n{exc}", file=sys.stderr)
        return 2


_CONTROL_NOTICE = """\
CONTROL-LEVEL BENCH CHECK — what will happen:
  * AFG CH1/CH2 outputs are switched OFF; load/levels/burst/ARB memory of the test channel are written.
  * PSU output is switched OFF (if ON); voltage/current SETPOINTS are changed and restored. The output
    is never enabled. Protection levels are only read.
  * Scope: timebase changed and restored; one waveform transferred (with --acquire: single-shot
    re-arm + force trigger, trigger mode restored).
Before continuing confirm: HV fixture discharged / PSU not connected to a charged link, gate-driver
input not connected to the AFG (or gate driver unpowered), personnel clear.
"""


def _cmd_bench_check(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from autodpt.benchcheck import run_bench_check, sim_bench, write_reports
    from autodpt.config import Station, load_model

    try:
        station = load_model(args.station, Station)
    except (ValidationError, OSError) as exc:
        print(f"INVALID station file: {exc}", file=sys.stderr)
        return 2
    if args.level == "control" and not args.sim and not args.yes:
        print(_CONTROL_NOTICE)
        if input("Type YES to continue: ").strip() != "YES":
            print("aborted")
            return 3
    rep = run_bench_check(station, args.level, sim=sim_bench() if args.sim else None, acquire=args.acquire)
    _, m = write_reports(rep, args.report_dir)
    width = max(len(r.check) for r in rep.results) if rep.results else 20
    print(f"\nBench check '{station.name}' — level {args.level} ({rep.mode})\n")
    for r in rep.results:
        print(f"  {r.status:4s}  {r.instrument:6s} {r.check:{width}s}  {r.detail}")
    print(f"\nOVERALL: {rep.overall}    report: {m}")
    return {"PASS": 0, "WARN": 0, "FAIL": 1}[rep.overall]


def _cmd_discover(args: argparse.Namespace) -> int:
    try:
        import pyvisa
    except ImportError:
        print("pyvisa not installed: pip install -e .[hw]")
        return 2
    rm = pyvisa.ResourceManager(args.visa_library) if args.visa_library else pyvisa.ResourceManager()
    print(f"VISA library: {rm.visalib}")
    try:
        found = rm.list_resources("?*")
    except Exception as exc:  # backend-specific
        print(f"discovery failed: {exc}")
        return 1
    for res in found:
        print("  ", res)
    if not found:
        print("  nothing found (VXI-11 broadcast discovery may be blocked; LeCroy in VICP mode is not "
              "discoverable — use the IPs from the instrument menus)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="autodpt", description="Automated double pulse test")
    ap.add_argument("--version", action="version", version=f"autodpt {__version__}")
    ap.add_argument("--log-dir", type=Path, default=None, help="write rotating event log here")
    ap.add_argument("--sim", action="store_true", help="simulation mode (no hardware): run a synthetic shot")
    ap.add_argument("--device", choices=("igbt", "sic"), default="igbt")
    ap.add_argument("--preset", default=None, help="threshold preset (default: auto from DUT type)")
    ap.add_argument("--skew", type=float, default=0.0, help="current-probe skew to simulate, ns")
    ap.add_argument("--noise", action="store_true", help="add 0.5 %% noise")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("presets", help="list threshold presets")
    cc = sub.add_parser("check-config", help="validate a setup file (ARC-05)")
    cc.add_argument("setup", type=Path)
    cc.add_argument("--probes", type=Path, default=None)
    bc = sub.add_parser("bench-check", help="check connection and controllability of the instruments")
    bc.add_argument("station", type=Path)
    bc.add_argument("--level", choices=("connect", "control"), default="connect",
                    help="connect = read-only (default); control = safe write/read-back/restore tests")
    bc.add_argument("--sim", action="store_true", help="run against simulated instruments")
    bc.add_argument("--acquire", action="store_true", help="scope: single-shot arm + force trigger")
    bc.add_argument("--yes", action="store_true", help="skip the safety confirmation (control level)")
    bc.add_argument("--report-dir", type=Path, default=Path("reports"))
    dc = sub.add_parser("discover", help="list VISA resources visible from this laptop")
    dc.add_argument("--visa-library", default="@py")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_dir)
    if args.cmd == "presets":
        return _cmd_presets(args)
    if args.cmd == "check-config":
        return _cmd_check_config(args)
    if args.cmd == "bench-check":
        return _cmd_bench_check(args)
    if args.cmd == "discover":
        return _cmd_discover(args)
    if args.sim:
        return _cmd_sim(args)
    print("Hardware mode is not available before Phase 2. Use --sim (see --help).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
