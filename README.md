# AutoDPT

Automated double pulse test (DPT) application for IGBT and SiC MOSFET power modules — instrument
control (LeCroy WaveRunner 8108HD, Tektronix AFG31022, Magna-Power SL1000), switching analysis per
IEC 60747-9 / IEC 60747-8, data management and reporting. Specification: [`docs/SRS.md`](docs/SRS.md).

## Status

| Phase | Content | State |
|---|---|---|
| P0 | Repo skeleton, typed configuration, event log, CLI with simulation mode, CI | ✔ done |
| P1 | Analysis library + synthetic golden-waveform generator + tests (VER-SW-01) | ✔ done |
| P2a | Transport layer, command sets, instrument simulators, **bench check** (connection + controllability) | ✔ done — run it on the bench |
| P2 | Instrument HAL drivers (PowerSupply, PulseGenerator, Oscilloscope, …) | next |
| P3–P7 | Sequencer/safety, data layer, GUI, reports, commissioning | open |

## Quick start

```bash
python -m pip install -e ".[dev]"
python -m autodpt --sim                                   # synthetic IGBT shot: analysis vs truth
python -m autodpt --sim --device sic --skew 4.7 --noise   # what an uncorrected 4.7 ns skew does
python -m autodpt presets
python -m autodpt bench-check config/examples/station.yaml --sim --level control   # see docs/bench_check.md
python -m autodpt check-config config/examples/setup_fs820_hs_400v.yaml --probes config/examples/probes.yaml
python -m pytest -q                                       # 77 tests, ~15 s
python tools/accuracy_report.py                           # regenerates docs/analysis_accuracy.md
python tools/trace_matrix.py --check                      # regenerates docs/traceability.md
```

## Layout

```text
autodpt/config/     pydantic models (CFG-01..07), presets (IEC60747-9, IEC60747-8, LEGACY), loaders
autodpt/analysis/   pure NumPy/SciPy analysis (no Qt, no VISA) + synth.py golden waveforms
autodpt/data/       event log (HDF5/CSV/xlsx writers follow in P4)
autodpt/hal/        transports (PyVISA/mock), command sets (YAML), LeCroy WAVEDESC, netcheck, simulators
autodpt/benchcheck.py  connection + controllability check (NET-09)
autodpt/sequencer, gui, report   placeholders for later phases
config/examples/    probe library, station, an FS820 setup mirroring the result template
template/           result template (.xlsx) + template_map.yaml (SRS §8.3)
docs/               SRS, analysis accuracy report, traceability matrix
tests/              unit + golden tests (pytest markers carry requirement IDs)
```

## Analysis in one paragraph

`analyse_shot(waveforms, meta, cfg, preset)` → `ShotResult` with every template parameter for the
three de-skew methods (none / single / statistic). Reference levels follow SRS §7 (V_DC from the
off-state DUT voltage, I_off at 90 % V_CE, I_on via freewheel decay or the `IL` channel), crossings
use debounce + Schmitt hysteresis scaled to each channel's noise, recovery uses
i_D = I_L − i_C with I_L tracked through the tail, and the statistic de-skew fits
v_CE = V_DC − L_σ·di_C/dt(t+τ). See `docs/analysis_accuracy.md` for verified accuracy.
