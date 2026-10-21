"""Statistic de-skew (SRS ANA-22, ANA-23).

During the turn-on current rise the complementary diode still conducts, so the DUT voltage is
    v_CE(t) = V_DC − L_σ · di_C/dt(t)      (true, aligned signals).
If the current channel lags by τ (i_meas(t) = i_true(t − τ)), then v_CE(t) = V_DC − L_σ·i_meas'(t + τ).
For each τ on a grid the model is fitted by least squares (V_DC, L_σ free); τ* minimises the
residual. A sharp minimum and a dip well above the noise are required for a trustworthy τ*.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy.signal import savgol_filter

from autodpt.analysis.edges import find_crossing
from autodpt.analysis.levels import Levels
from autodpt.analysis.preprocess import baseline_window
from autodpt.analysis.types import ShotMeta, SkewEstimate, Waveforms
from autodpt.config.models import AnalysisConfig


def _fit_residual(v: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """LSQ v ≈ a + b·x; returns (residual sum of squares, b)."""
    xm, vm = float(np.mean(x)), float(np.mean(v))
    dx = x - xm
    sxx = float(np.dot(dx, dx))
    if sxx == 0.0:
        return float(np.sum((v - vm) ** 2)), 0.0
    b = float(np.dot(dx, v - vm)) / sxx
    r = v - vm - b * dx
    return float(np.dot(r, r)), b


def estimate_skew(wf: Waveforms, lv: Levels, meta: ShotMeta, cfg: AnalysisConfig,
                  center: float = 0.0) -> SkewEstimate:
    """Estimate the IC_DUT skew. The search covers center ± tau_max; pass the single de-skew value
    as `center` so that large fixed probe delays (Rogowski: tens of ns) stay inside the range."""
    c = cfg.deskew
    ic, vce = wf.signals["IC_DUT"], wf.signals["VCE_DUT"]
    a, b = lv.win_on
    db = cfg.debounce_samples
    h = lv.h("IC_DUT")
    t02 = find_crossing(ic, wf.t0, wf.dt, 0.02 * lv.i_on, "rising", a, b, db, hysteresis=h)
    t98 = find_crossing(ic, wf.t0, wf.dt, 0.98 * lv.i_on, "rising", a, b, db, hysteresis=h)
    if t02 is None or t98 is None:
        return SkewEstimate(math.nan, math.nan, 0.0, 0.0, False, "current rise not found")
    span = t98 - t02
    # recovery current peak (= start of the v_CE collapse): search shortly after the rise only —
    # later in t3 the conduction ramp can exceed it (SiC: small I_rrm)
    pa, pb = wf.index(t02), wf.index(t02 + 3.0 * span)
    t_ic_peak = wf.t0 + wf.dt * (pa + int(np.argmax(ic[pa:pb])))
    # window in *true* time: the measured current is late by ~center
    w_start = t02 - center - (0.2 * span + c.tau_max_s)
    w_stop = min(t98 + 0.2 * span, t_ic_peak - c.tau_max_s) - center
    if w_stop - w_start < 10 * wf.dt:
        return SkewEstimate(math.nan, math.nan, 0.0, 0.0, False, "fit window too short")
    ia, ib = wf.index(w_start), wf.index(w_stop)

    # Savitzky–Golay derivative of the current over a region covering all shifted windows
    n_sg = max(5, round(c.sg_window_s / wf.dt) | 1)
    reach = int((abs(center) + c.tau_max_s) / wf.dt) + n_sg
    ra = max(0, ia - reach)
    rb = min(len(ic), ib + reach)
    didt = np.asarray(savgol_filter(ic[ra:rb], n_sg, 2, deriv=1, delta=wf.dt), dtype=np.float64)
    pos_r = np.arange(ra, rb, dtype=np.float64)
    # identical SG smoothing on v_CE keeps the model consistent (same effective bandwidth)
    vs = np.asarray(savgol_filter(vce[ra:rb], n_sg, 2), dtype=np.float64)
    v = vs[ia - ra:ib - ra]
    pos_w = np.arange(ia, ib, dtype=np.float64)

    taus = center + np.arange(-c.tau_max_s, c.tau_max_s + 0.5 * c.step_s, c.step_s)
    res = np.empty(len(taus))
    slopes_b = np.empty(len(taus))
    for k, tau in enumerate(taus):
        x = np.interp(pos_w + tau / wf.dt, pos_r, didt)
        res[k], slopes_b[k] = _fit_residual(v, x)
    k_min = int(np.argmin(res))
    tau_star = float(taus[k_min])
    if 0 < k_min < len(taus) - 1:  # parabolic refinement
        y0, y1, y2 = res[k_min - 1], res[k_min], res[k_min + 1]
        den = y0 - 2 * y1 + y2
        if den > 0:
            tau_star += 0.5 * (y0 - y2) / den * c.step_s
    l_sigma = -float(slopes_b[k_min])

    # confidence (ANA-22)
    base = baseline_window(wf, meta.t_pre, cfg.baseline_window_frac)
    noise = float(np.std(vce[base]))
    noise_s = float(np.std(savgol_filter(vce[base], n_sg, 2)))  # noise after the same smoothing
    dip = abs(l_sigma) * float(np.max(np.abs(didt)))
    snr = math.inf if noise == 0 else dip / noise
    # model error: residual rms beyond the instrument noise, relative to the dip amplitude
    r_rms2 = float(res[k_min]) / max(len(v), 1)
    fit_error = math.sqrt(max(r_rms2 - noise_s * noise_s, 0.0)) / dip if dip > 0 else math.inf
    r_star = max(float(res[k_min]), 1e-30)
    off = round(c.contrast_offset_s / c.step_s)
    neigh = [res[j] for j in (k_min - off, k_min + off) if 0 <= j < len(res)]
    contrast = min(neigh) / r_star if neigh else 0.0
    edge_hit = k_min in (0, len(taus) - 1)
    ok = (snr >= c.min_snr and contrast >= c.min_contrast and l_sigma > 0 and not edge_hit
          and fit_error <= c.max_fit_error)
    reason = "" if ok else (
        "minimum at search limit" if edge_hit else
        f"snr={snr:.1f}, contrast={contrast:.2f}, fit_error={fit_error:.3f}, L_sigma={l_sigma:.3e}")
    return SkewEstimate(tau=tau_star, l_sigma=l_sigma, snr=snr, contrast=contrast, ok=ok, reason=reason,
                        fit_error=fit_error)


def pooled_skew(estimates: Sequence[SkewEstimate]) -> tuple[float | None, float, int]:
    """ANA-23: median of valid per-shot τ*, its IQR and the number of valid shots."""
    good = np.array([e.tau for e in estimates if e.ok and math.isfinite(e.tau)])
    if good.size == 0:
        return None, math.nan, 0
    q75, q25 = np.percentile(good, [75, 25])
    return float(np.median(good)), float(q75 - q25), int(good.size)
