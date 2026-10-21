"""Shot and point analysis entry points (SRS ANA-06, ANA-02, ANA-23, ANA-24).

``analyse_shot`` is a pure function: no I/O, no instrument access. It evaluates all parameters
for each de-skew method on the same raw data (ANA-24). ``analyse_point`` pools the per-shot
statistic skew over repetitions (ANA-23) and re-evaluates the statistic method with τ_stat.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from autodpt.analysis.deskew import estimate_skew, pooled_skew
from autodpt.analysis.levels import compute_levels
from autodpt.analysis.preprocess import (
    apply_filter,
    apply_skews,
    baseline_window,
    remove_current_baseline,
    rogowski_droop_compensation,
)
from autodpt.analysis.recovery import comp_monitors, reverse_recovery, stray_inductance
from autodpt.analysis.switching import peaks, preset_params, slopes
from autodpt.analysis.types import (
    METHODS,
    AnalysisError,
    DeskewMethod,
    ShotMeta,
    ShotResult,
    SkewEstimate,
    Waveforms,
)
from autodpt.analysis.validate import validate
from autodpt.config.models import AnalysisConfig
from autodpt.config.presets import Preset, resolve_preset


def preprocess_shot(wf: Waveforms, meta: ShotMeta, cfg: AnalysisConfig) -> tuple[Waveforms, dict[str, float]]:
    """ANA-08 steps 2–3 (baseline, droop). Returns a new Waveforms and the removed offsets."""
    w = Waveforms(dt=wf.dt, t0=wf.t0, signals=dict(wf.signals), clipped=dict(wf.clipped))
    base = baseline_window(w, meta.t_pre, cfg.baseline_window_frac)
    removed = remove_current_baseline(w, base)
    for sig, tau in meta.rogowski_tau.items():
        if w.has(sig):
            w.signals[sig] = rogowski_droop_compensation(w.signals[sig], w.dt, tau, start=base.start)
    return w, removed


def _evaluate(wf: Waveforms, meta: ShotMeta, cfg: AnalysisConfig, preset: Preset
              ) -> tuple[dict[str, float], list[str]]:
    lv = compute_levels(wf, meta, cfg)
    vals: dict[str, float] = {
        "vge_on_plateau": lv.vge_on, "vge_off_plateau": lv.vge_off,
        "vdc_pre": lv.vdc_pre, "vdc_off": lv.vdc_off, "vdc_on": lv.vdc_on,
        "vdc_offset": lv.vdc_pre - meta.v_set,
        "i_off": lv.i_off, "i_on": lv.i_on, "t_v90_off": lv.t_v90_off,
    }
    e1, e2, _, _ = meta.edges_commanded
    vals["afg_to_vge_delay_on"] = lv.gate_edges[0] - e1
    vals["afg_to_vge_delay_off"] = lv.gate_edges[1] - e2
    flags = list(lv.flags)
    p_vals, _markers, p_flags = preset_params(wf, lv, preset, cfg)
    vals.update(p_vals)
    flags += p_flags
    vals.update(slopes(wf, lv, cfg))
    pk = peaks(wf, lv)
    vals.update(pk)
    rr, rr_flags = reverse_recovery(wf, lv, meta, cfg)
    vals.update(rr)
    flags += rr_flags
    vals.update(stray_inductance(wf, lv, cfg, vals["didt_off"], pk["overshoot_dv"]))
    vals.update(comp_monitors(wf, lv))
    flags += validate(wf, lv, meta, cfg, vals)
    return vals, flags


def analyse_shot(wf: Waveforms, meta: ShotMeta, cfg: AnalysisConfig, preset: Preset | None = None,
                 statistic_skew: float | None = None) -> ShotResult:
    """Analyse one shot for all de-skew methods (ANA-02).

    `statistic_skew`: pooled τ_stat for the point (ANA-23). If None, this shot's own estimate is
    used when it is trustworthy; otherwise the statistic method is skipped (values absent)."""
    preset = preset or resolve_preset(cfg.preset, meta.dut_type)
    try:
        base, _ = preprocess_shot(wf, meta, cfg)
        est: SkewEstimate | None = estimate_skew(base, compute_levels(base, meta, cfg), meta, cfg,
                                                 center=meta.single_skew.get("IC_DUT", 0.0))
    except AnalysisError as exc:
        return ShotResult(values={}, skews={}, flags=[exc.flag], preset=_preset_info(preset))
    single = {s: meta.single_skew.get(s, 0.0) for s in base.signals}
    tau_stat = statistic_skew if statistic_skew is not None else (est.tau if est and est.ok else None)

    skews: dict[str, float | None] = {
        "none": 0.0, "single": single.get("IC_DUT", 0.0), "statistic": tau_stat}
    values: dict[str, dict[str, float]] = {}
    flags: list[str] = []
    for method in METHODS:
        if method == "statistic" and tau_stat is None:
            continue
        if method == "none":
            shifts: dict[str, float] = {}
        elif method == "single":
            shifts = single
        else:
            shifts = dict(single)
            shifts["IC_DUT"] = float(tau_stat)  # type: ignore[arg-type]
        w = apply_filter(apply_skews(base, shifts), cfg.filter)
        try:
            vals, f = _evaluate(w, meta, cfg, preset)
        except AnalysisError as exc:
            return ShotResult(values={}, skews={}, flags=[exc.flag], preset=_preset_info(preset))
        vals["skew"] = shifts.get("IC_DUT", 0.0)
        values[method] = vals
        if method == "none":
            flags = f
    if est is not None and not est.ok:
        flags.append("DESKEW_ILL_CONDITIONED")
    return ShotResult(values=values, skews=skews, flags=flags, preset=_preset_info(preset),
                      skew_estimate=est)


def analyse_point(shots: Sequence[tuple[Waveforms, ShotMeta]], cfg: AnalysisConfig,
                  preset: Preset | None = None) -> tuple[list[ShotResult], dict[str, float]]:
    """ANA-23: pool τ* over the repetitions of one point, then re-evaluate with τ_stat."""
    if not shots:
        return [], {}
    preset = preset or resolve_preset(cfg.preset, shots[0][1].dut_type)
    first = [analyse_shot(wf, m, cfg, preset) for wf, m in shots]
    ests = [r.skew_estimate for r in first if r.skew_estimate is not None]
    tau_stat, iqr, n_ok = pooled_skew(ests)
    results = first if tau_stat is None else [
        analyse_shot(wf, m, cfg, preset, statistic_skew=tau_stat) for wf, m in shots]
    summary = {"tau_stat": math.nan if tau_stat is None else tau_stat, "tau_iqr": iqr,
               "n_valid_skew": float(n_ok)}
    return results, summary


def _preset_info(p: Preset) -> dict[str, str]:
    return {"name": p.name, "version": p.version, "verified": str(p.verified).lower()}


def with_method(result: ShotResult, method: DeskewMethod) -> dict[str, float]:
    """Convenience accessor used by writers/tests."""
    return result.values.get(method, {})


__all__ = ["analyse_point", "analyse_shot", "preprocess_shot", "with_method"]
