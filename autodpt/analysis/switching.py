"""Preset-driven switching times and energies, slopes and peaks (SRS ANA-14 … ANA-17)."""

from __future__ import annotations

import math

import numpy as np

from autodpt.analysis.edges import find_crossing, integrate
from autodpt.analysis.levels import Levels
from autodpt.analysis.types import Waveforms
from autodpt.config.models import AnalysisConfig
from autodpt.config.presets import Preset, PresetPoint

_SIG = {"vge": "VGE_DUT", "ic": "IC_DUT", "vce": "VCE_DUT"}


def gate_level(lv: Levels, frac: float, preset: Preset) -> float:
    """ANA-10: IEC presets reference the percentage to 0 V, LEGACY to the swing."""
    if preset.gate_reference == "zero":
        return frac * lv.vge_on
    return lv.vge_off + frac * (lv.vge_on - lv.vge_off)


def ref_level(pt: PresetPoint, event: str, lv: Levels, preset: Preset) -> float:
    if pt.signal == "vge":
        return gate_level(lv, pt.level, preset)
    if pt.signal == "ic":
        return pt.level * (lv.i_on if event == "on" else lv.i_off)
    return pt.level * (lv.vdc_on if event == "on" else lv.vdc_off)


def preset_params(wf: Waveforms, lv: Levels, preset: Preset, cfg: AnalysisConfig
                  ) -> tuple[dict[str, float], dict[str, tuple[float, float]], list[str]]:
    """Evaluate every preset parameter. Returns values, markers {key: (t_start, t_end)} and flags."""
    vals: dict[str, float] = {}
    markers: dict[str, tuple[float, float]] = {}
    flags: list[str] = []
    p_ce = wf.signals["VCE_DUT"] * wf.signals["IC_DUT"]
    for key, p in preset.params.items():
        a, b = lv.win_on if p.event == "on" else lv.win_off
        xs = wf.signals[_SIG[p.start.signal]]
        ts = find_crossing(xs, wf.t0, wf.dt, ref_level(p.start, p.event, lv, preset), p.start.direction,
                           a, b, cfg.debounce_samples, p.start.search, lv.h(_SIG[p.start.signal]))
        te = None
        if ts is not None:
            xe = wf.signals[_SIG[p.end.signal]]
            i_from = math.floor((ts - wf.t0) / wf.dt)
            te = find_crossing(xe, wf.t0, wf.dt, ref_level(p.end, p.event, lv, preset), p.end.direction,
                               i_from, b, cfg.debounce_samples, p.end.search, lv.h(_SIG[p.end.signal]))
        if ts is None or te is None:
            vals[key] = math.nan
            flags.append(f"THRESHOLD_NOT_REACHED:{key}")
            continue
        markers[key] = (ts, te)
        vals[key] = te - ts if p.kind == "time" else integrate(p_ce, wf.t0, wf.dt, ts, te)
    return vals, markers, flags


def slopes(wf: Waveforms, lv: Levels, cfg: AnalysisConfig) -> dict[str, float]:
    """Signed dv/dt and di/dt (falling = negative) between the slope levels (ANA-15 table)."""
    s1, s2 = sorted(cfg.slope_levels)
    db = cfg.debounce_samples
    out: dict[str, float] = {}

    def pair(sig: str, lo: float, hi: float, rising: bool, a: int, b: int
             ) -> tuple[float | None, float | None]:
        x = wf.signals[sig]
        first, second = (lo, hi) if rising else (hi, lo)
        d = "rising" if rising else "falling"
        t1 = find_crossing(x, wf.t0, wf.dt, first, d, a, b, db, hysteresis=lv.h(sig))
        if t1 is None:
            return None, None
        t2 = find_crossing(x, wf.t0, wf.dt, second, d, a, b, db, hysteresis=lv.h(sig))
        return t1, t2

    def slope(t1: float | None, t2: float | None, delta: float) -> float:
        return delta / (t2 - t1) if t1 is not None and t2 is not None and t2 > t1 else math.nan

    a, b = lv.win_on
    t1, t2 = pair("IC_DUT", s1 * lv.i_on, s2 * lv.i_on, True, a, b)
    out["didt_on"] = slope(t1, t2, (s2 - s1) * lv.i_on)
    out["ic_rise_time_on"] = (t2 - t1) if t1 is not None and t2 is not None else math.nan
    t1, t2 = pair("VCE_DUT", s1 * lv.vdc_on, s2 * lv.vdc_on, False, a, b)
    out["dvdt_on"] = slope(t1, t2, -(s2 - s1) * lv.vdc_on)

    a, b = lv.win_off
    t1, t2 = pair("VCE_DUT", s1 * lv.vdc_off, s2 * lv.vdc_off, True, a, b)
    out["dvdt_off"] = slope(t1, t2, (s2 - s1) * lv.vdc_off)
    out["vce_rise_time_off"] = (t2 - t1) if t1 is not None and t2 is not None else math.nan
    t1, t2 = pair("IC_DUT", s1 * lv.i_off, s2 * lv.i_off, False, a, b)
    out["didt_off"] = slope(t1, t2, -(s2 - s1) * lv.i_off)
    return out


def peaks(wf: Waveforms, lv: Levels) -> dict[str, float]:
    """ANA-17. 'vce_peak_off' (absolute) is the template's "Voltage overshoot"."""
    a, b = lv.win_off
    vpk = float(np.max(wf.signals["VCE_DUT"][a:b]))
    a2, b2 = lv.win_on
    return {
        "vce_peak_off": vpk,
        "overshoot_dv": vpk - lv.vdc_off,
        "ic_peak_on": float(np.max(wf.signals["IC_DUT"][a2:b2])),
    }
