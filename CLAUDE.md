# Instructions for Claude Code

* `docs/SRS.md` is the single source of truth. Implement phase by phase (§12); Phases 0–1 are done.
* Reference requirement IDs in docstrings and in tests via `@pytest.mark.req("ID", ...)`;
  run `python tools/trace_matrix.py --check`.
* Never invent instrument commands. Verify against the manuals in `docs/manuals/` (add them) and mark
  anything unverified with `# UNVERIFIED`. Appendix C of the SRS lists starting points only.
* `autodpt/analysis` must stay free of Qt and VISA (ARC-01). Every analysis change must keep
  `pytest tests/golden` green; if a tolerance has to change, regenerate `docs/analysis_accuracy.md`
  and explain why in CHANGELOG.md.
* Safety (SRS §2.4, §5): software is never the protection layer; output permissions are enforced in
  the HAL wrapper, not the GUI. Abort/safe-off paths must pre-empt queued commands.
* Quality gate before every commit: `ruff check autodpt tests tools`, `python -m mypy autodpt`,
  `python -m pytest -q`.
* Phase 2a (bench check, transports, command sets, simulators) is done. Command strings live ONLY in
  `autodpt/hal/commandsets/*.yaml`; after a passing on-bench `bench-check --level control`, set the
  confirmed commands to `verified: true`.
* Next: Phase 2 — HAL interfaces (`PowerSupply`, `PulseGenerator`, `Oscilloscope`, `DcLinkMonitor`,
  `DigitalIO`, `TemperatureSource/Controller`), mocks built on `analysis/synth.py`, then real drivers
  with read-only `--identify` smoke scripts.
