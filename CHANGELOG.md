# Changelog

## 0.1.0 — 2026-10-22 — Phase 0 + Phase 1

### Added
* Package skeleton per SRS §4.2, `pyproject.toml`, CI (ruff, mypy strict on analysis/config, pytest,
  traceability check) for Windows and Linux.
* Typed configuration (pydantic v2): probes, channel map, fixture (incl. Appendix E bleeder/relay
  and derived energies/bleed times), DUT, gate drive, limits, test matrix, analysis config, station.
  Example files in `config/examples/`.
* Threshold presets as data: `IEC60747-9` (IGBT), `IEC60747-8` (MOSFET) — both `verified: false`
  until checked against the standards (open point O8) — and `LEGACY` (draft table).
* Event log with ISO-8601 ms timestamps, rotating file and in-memory buffer (DATA-04).
* CLI: `--sim` simulation mode, `presets`, `check-config`.
* Analysis library (SRS §7): pre-processing, gate edges and reference levels, preset-driven times and
  energies, signed slopes, peaks, reverse recovery, L_σ, complementary-device monitors, validation
  flags, three de-skew methods, point-level skew pooling.
* `analysis/synth.py`: circuit-consistent synthetic DPT waveforms (IGBT-like and SiC-like) with
  independent ground truth; scope model with skew, noise, quantisation, polarity, clipping.
* 58 tests; `docs/analysis_accuracy.md`; `docs/traceability.md`.

### Findings fed back into the SRS (v1.2, items marked [P1])
* ANA-14 windows must start at the commanded AFG edge — the IGBT 90 % V_GE crossing precedes the 50 %
  gate edge by ~0.5 µs (Miller plateau).
* ANA-14 needs Schmitt hysteresis: noise on the current ramp produced false falling crossings 1 µs early.
* ANA-11: extrapolating the t1 ramp to t_v90 over-states I_off by ~0.4 % (IGBT); now fit to t_v10 plus
  the measured current gain during the voltage rise. I_on via freewheel decay instead of the t3 ramp.
* ANA-18: recovery results are very sensitive to I_on (0.06 % → 1.7 % on Q_rr for SiC).
* ANA-22: search centred on the single de-skew value; fit-error criterion added after an out-of-range
  skew produced a confidently wrong estimate.
