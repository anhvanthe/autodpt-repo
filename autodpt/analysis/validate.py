"""Shot validation (SRS ANA-30, ANA-31, ANA-27 warnings)."""

from __future__ import annotations

import math

import numpy as np

from autodpt.analysis.levels import Levels
from autodpt.analysis.types import ShotMeta, Waveforms
from autodpt.config.models import AnalysisConfig


def validate(wf: Waveforms, lv: Levels, meta: ShotMeta, cfg: AnalysisConfig,
             vals: dict[str, float]) -> list[str]:
    flags: list[str] = []
    for sig, clipped in wf.clipped.items():
        if clipped:
            flags.append(f"CLIPPED:{sig}")
    if meta.i_set > 0 and abs(lv.i_off - meta.i_set) > cfg.current_tolerance * meta.i_set:
        flags.append("CURRENT_OUT_OF_TOL")
    if meta.v_set > 0 and abs(lv.vdc_off - meta.v_set) > cfg.vdc_tolerance * meta.v_set:
        flags.append("VDC_OUT_OF_TOL")
    if lv.vdc_pre > 0 and (lv.vdc_pre - lv.vdc_off) > cfg.droop_warn_frac * lv.vdc_pre:
        flags.append("DROOP_HIGH")
    for k in ("e_on", "e_off", "e_rec"):
        v = vals.get(k, math.nan)
        if math.isfinite(v) and v < 0:
            flags.append(f"NEGATIVE_ENERGY:{k}")

    # bandwidth / sample-count checks on the fastest edges (ANA-30)
    scope_tr = cfg.scope_rise_time_s
    for key, sig in (("ic_rise_time_on", "IC_DUT"), ("vce_rise_time_off", "VCE_DUT")):
        tr = vals.get(key, math.nan)
        if not math.isfinite(tr):
            continue
        probe_tr = meta.probe_rise_time.get(sig, 0.0)
        sys_tr = math.hypot(probe_tr, scope_tr)
        if tr < cfg.bw_factor * sys_tr:
            flags.append(f"BW_LIMITED_EDGE:{sig}")
        if tr / wf.dt < cfg.min_edge_samples:
            flags.append(f"LOW_SAMPLES_ON_EDGE:{sig}")

    # ringing settled at the end of the turn-off window
    _, b = lv.win_off
    tail = wf.signals["VCE_DUT"][max(b - 20, 0):b]
    if tail.size and abs(float(np.mean(tail)) - lv.vdc_off) > cfg.ringing_settle_frac * lv.vdc_off:
        flags.append("RINGING_NOT_SETTLED")

    # complementary gate monitors (ANA-27)
    vmax = max(vals.get("comp_vge_max_on", math.nan), vals.get("comp_vge_max_off", math.nan))
    vmin = min(vals.get("comp_vge_min_on", math.nan), vals.get("comp_vge_min_off", math.nan))
    if meta.comp_vth_min is not None and math.isfinite(vmax) and vmax > meta.comp_vth_min:
        flags.append("COMP_GATE_NEAR_VTH")
    if meta.comp_vge_min_rating is not None and math.isfinite(vmin) and vmin < meta.comp_vge_min_rating:
        flags.append("COMP_GATE_UNDERSHOOT")
    return flags
