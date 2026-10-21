"""Diode reverse recovery, stray inductance and complementary-device monitors
(SRS ANA-12, ANA-18, ANA-19, ANA-27)."""

from __future__ import annotations

import math

import numpy as np

from autodpt.analysis.edges import find_crossing, integrate
from autodpt.analysis.levels import Levels
from autodpt.analysis.types import ShotMeta, Waveforms
from autodpt.config.models import AnalysisConfig

_NAN_RR = ("t_rr", "q_rr", "i_rrm", "softness", "t_a", "t_b", "e_rec")


def diode_signals(wf: Waveforms, lv: Levels, meta: ShotMeta) -> tuple[np.ndarray, np.ndarray, bool]:
    """ANA-12 over the turn-on window: returns (i_D, v_D, v_D_derived).

    I_L(t) = I_on + (1/L)·∫ v_D dt (the load inductor is across the complementary device), so the
    inductor current is tracked through the recovery tail instead of assumed constant.
    i_D = I_L − i_C (forward/freewheeling positive; reverse recovery negative)."""
    a, b = lv.win_on
    ic = wf.signals["IC_DUT"][a:b]
    derived = not wf.has("VCE_COMP")
    if derived:
        vd = lv.vdc_on - wf.signals["VCE_DUT"][a:b]
    else:
        vd = wf.signals["VCE_COMP"][a:b]
    if wf.has("IL"):
        il = wf.signals["IL"][a:b]
    else:
        integ = np.concatenate(([0.0], np.cumsum(0.5 * (vd[1:] + vd[:-1]) * wf.dt)))
        il = lv.i_on + integ / meta.l_load
    return il - ic, vd, derived


def _refine_min(y: np.ndarray, t0: float, dt: float, k: int, top: float = 0.05,
                max_half: int = 40) -> tuple[float, float]:
    """Sub-sample minimum of the recovery peak (ANA-18).

    Least-squares parabola through the contiguous samples around index k that lie within the
    top `top` fraction of the peak (|y| ≥ (1−top)·|y[k]|), at least ±2 samples. Using the whole
    top of the peak averages noise instead of following the single most negative (noise-biased)
    sample. Returns (t_peak, I_rrm)."""
    thr = (1.0 - top) * y[k]
    a = k
    while a > 0 and k - a < max_half and y[a - 1] <= thr:
        a -= 1
    b = k
    while b < len(y) - 1 and b - k < max_half and y[b + 1] <= thr:
        b += 1
    a, b = min(a, k - 2), max(b, k + 2)
    a, b = max(a, 0), min(b + 1, len(y))
    if b - a < 3:
        return t0 + k * dt, -float(y[k])
    xx = np.arange(a, b, dtype=np.float64) - k
    c2, c1, c0 = np.polyfit(xx, y[a:b], 2)
    if c2 <= 0:
        return t0 + k * dt, -float(y[k])
    xv = float(np.clip(-c1 / (2 * c2), xx[0], xx[-1]))
    return t0 + (k + xv) * dt, -float(c0 + c1 * xv + c2 * xv * xv)


def reverse_recovery(wf: Waveforms, lv: Levels, meta: ShotMeta, cfg: AnalysisConfig
                     ) -> tuple[dict[str, float], list[str]]:
    flags: list[str] = []
    i_d, v_d, derived = diode_signals(wf, lv, meta)
    if derived:
        flags.append("VD_DERIVED")
    a, _ = lv.win_on
    t0w = wf.t0 + a * wf.dt
    dt = wf.dt
    db = cfg.debounce_samples
    out = dict.fromkeys(_NAN_RR, math.nan)

    h = lv.h("IC_DUT")
    t0 = find_crossing(i_d, t0w, dt, 0.0, "falling", 0, len(i_d), db, hysteresis=h)
    if t0 is None:
        flags.append("THRESHOLD_NOT_REACHED:t_rr")
        return out, flags
    k0 = int((t0 - t0w) / dt)
    kp = k0 + int(np.argmin(i_d[k0:]))
    t_peak, irrm = _refine_min(i_d, t0w, dt, kp)
    if irrm < 0.005 * abs(lv.i_on):
        flags.append("NO_RECOVERY")
        out.update(i_rrm=max(irrm, 0.0), q_rr=0.0, e_rec=0.0)
        return out, flags
    hi, lo = max(cfg.rr_levels), min(cfg.rr_levels)
    # tail levels: searched from the peak itself (already "armed"), so no hysteresis is needed
    t_hi = find_crossing(i_d, t0w, dt, -hi * irrm, "rising", kp, len(i_d), db)
    t_lo = None
    if t_hi is not None:
        t_lo = find_crossing(i_d, t0w, dt, -lo * irrm, "rising", int((t_hi - t0w) / dt), len(i_d), db)
    if t_hi is None or t_lo is None or t_lo <= t_hi:
        flags.append("THRESHOLD_NOT_REACHED:t_rr")
        out["i_rrm"] = irrm
        return out, flags
    t_end = t_lo + lo * (t_lo - t_hi) / (hi - lo)  # zero of the line through the two points
    if cfg.qrr_end == "2pct":
        tq = find_crossing(i_d, t0w, dt, -0.02 * irrm, "rising", kp, len(i_d), db) or t_end
    else:
        tq = t_end
    neg = np.maximum(-i_d, 0.0)
    out.update(
        t_rr=t_end - t0,
        i_rrm=irrm,
        t_a=t_peak - t0,
        t_b=t_end - t_peak,
        softness=(t_end - t_peak) / (t_peak - t0) if t_peak > t0 else math.nan,
        q_rr=integrate(neg, t0w, dt, t0, tq),
        e_rec=integrate(v_d * -i_d, t0w, dt, t0, tq),
    )
    return out, flags


def stray_inductance(wf: Waveforms, lv: Levels, cfg: AnalysisConfig, didt_off: float,
                     overshoot_dv: float) -> dict[str, float]:
    """ANA-19: L_σ,on = (V_DC,on − mean v_CE) / (di/dt) during the turn-on current rise;
    L_σ,off = ΔV_peak / |di/dt|_off (indicative only)."""
    a, b = lv.win_on
    ic, vce = wf.signals["IC_DUT"], wf.signals["VCE_DUT"]
    lo, hi = sorted(cfg.lsigma_levels)
    t_lo = find_crossing(ic, wf.t0, wf.dt, lo * lv.i_on, "rising", a, b, cfg.debounce_samples,
                         hysteresis=lv.h("IC_DUT"))
    t_hi = None
    if t_lo is not None:
        t_hi = find_crossing(ic, wf.t0, wf.dt, hi * lv.i_on, "rising", a, b,
                             cfg.debounce_samples, hysteresis=lv.h("IC_DUT"))
    l_on = math.nan
    if t_lo is not None and t_hi is not None:
        ia, ib = math.ceil((t_lo - wf.t0) / wf.dt), math.floor((t_hi - wf.t0) / wf.dt) + 1
        if ib - ia >= 3:
            tt = wf.t0 + wf.dt * np.arange(ia, ib)
            k = float(np.polyfit(tt - tt.mean(), ic[ia:ib], 1)[0])
            if k > 0:
                l_on = (lv.vdc_on - float(np.mean(vce[ia:ib]))) / k
    l_off = overshoot_dv / abs(didt_off) if didt_off and math.isfinite(didt_off) else math.nan
    return {"l_sigma_on": l_on, "l_sigma_off": l_off}


def comp_monitors(wf: Waveforms, lv: Levels) -> dict[str, float]:
    """ANA-27 complementary-device extremes (template columns AJ…AO). NaN if channel absent."""
    out: dict[str, float] = {}
    (a1, b1), (a2, b2) = lv.win_on, lv.win_off

    def ext(sig: str, a: int, b: int, fn: str) -> float:
        if not wf.has(sig):
            return math.nan
        seg = wf.signals[sig][a:b]
        return float(np.max(seg) if fn == "max" else np.min(seg))

    out["comp_i_max_off"] = ext("IC_COMP", a2, b2, "max")
    out["comp_vce_max_on"] = ext("VCE_COMP", a1, b1, "max")
    out["comp_vge_max_on"] = ext("VGE_COMP", a1, b1, "max")
    out["comp_vge_max_off"] = ext("VGE_COMP", a2, b2, "max")
    out["comp_vge_min_on"] = ext("VGE_COMP", a1, b1, "min")
    out["comp_vge_min_off"] = ext("VGE_COMP", a2, b2, "min")
    return out
