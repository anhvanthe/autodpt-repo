"""Pre-processing (SRS ANA-05, ANA-08, ANA-03).

Order: scale codes → baseline removal (currents) → Rogowski droop compensation → de-skew time
shift → optional filter. Each function is pure; the caller records the parameters used.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
from scipy.signal import butter, filtfilt

from autodpt.analysis.types import FloatArray, RawChannel, Waveforms
from autodpt.config.models import CURRENT_SIGNALS, FilterCfg


def waveforms_from_raw(channels: Iterable[RawChannel], dt: float, t0: float) -> Waveforms:
    """Scale raw codes to physical units, apply channel polarity and detect clipping (SCO-02)."""
    signals: dict[str, FloatArray] = {}
    clipped: dict[str, bool] = {}
    for ch in channels:
        x = ch.codes.astype(np.float64) * ch.gain + ch.offset
        if ch.invert:
            x = -x
        signals[ch.signal] = x
        clipped[ch.signal] = bool(np.any(ch.codes >= ch.code_max) or np.any(ch.codes <= ch.code_min))
    lengths = {len(v) for v in signals.values()}
    if len(lengths) != 1:
        raise ValueError(f"channels have different lengths: {lengths}")
    return Waveforms(dt=dt, t0=t0, signals=signals, clipped=clipped)


def baseline_window(wf: Waveforms, t_pre: float, frac: tuple[float, float]) -> slice:
    """Pre-pulse window [frac0·t_pre, frac1·t_pre] after the trigger (current known zero)."""
    a = wf.index(frac[0] * t_pre)
    b = wf.index(frac[1] * t_pre)
    if b - a < 10:
        raise ValueError("baseline window too short: increase t_pre or the record pre-trigger")
    return slice(a, b)


def remove_current_baseline(wf: Waveforms, window: slice,
                            signals: Iterable[str] = CURRENT_SIGNALS) -> dict[str, float]:
    """ANA-08 step 2: subtract the mean over the baseline window from current channels (in place).
    Returns the offsets removed."""
    removed: dict[str, float] = {}
    for s in signals:
        if wf.has(s):
            off = float(np.mean(wf.signals[s][window]))
            wf.signals[s] = wf.signals[s] - off
            removed[s] = off
    return removed


def rogowski_droop_compensation(x: FloatArray, dt: float, tau: float, start: int = 0) -> FloatArray:
    """ANA-05: first-order inverse of the coil's LF droop, i_corr = i + (1/τ)·∫ i dt (from `start`)."""
    y = x.copy()
    integ = np.zeros_like(x)
    seg = x[start:]
    integ[start:] = np.concatenate(([0.0], np.cumsum(0.5 * (seg[1:] + seg[:-1]) * dt)))
    y += integ / tau
    return y


def shift_signal(x: FloatArray, dt: float, tau: float) -> FloatArray:
    """ANA-08 step 4 / ANA-21: correct a channel that lags by `tau` (s): y(t) = x(t + tau).
    Fractional shifts by linear interpolation; end values are held."""
    if tau == 0.0:
        return x
    n = len(x)
    pos = np.arange(n, dtype=np.float64) + tau / dt
    return np.interp(pos, np.arange(n, dtype=np.float64), x)


def apply_skews(wf: Waveforms, skews: Mapping[str, float]) -> Waveforms:
    signals = {k: (shift_signal(v, wf.dt, skews[k]) if skews.get(k, 0.0) else v)
               for k, v in wf.signals.items()}
    return Waveforms(dt=wf.dt, t0=wf.t0, signals=signals, clipped=dict(wf.clipped))


def apply_filter(wf: Waveforms, cfg: FilterCfg) -> Waveforms:
    """ANA-03: none / centred moving average / zero-phase Butterworth low-pass."""
    if cfg.kind == "none":
        return wf
    out: dict[str, FloatArray] = {}
    if cfg.kind == "moving_average":
        k = np.ones(cfg.n_samples) / cfg.n_samples
        for s, x in wf.signals.items():
            pad = cfg.n_samples // 2
            xp = np.pad(x, (pad, cfg.n_samples - 1 - pad), mode="edge")
            out[s] = np.convolve(xp, k, mode="valid")
    else:
        wn = 2.0 * cfg.cutoff_hz * wf.dt
        if not 0 < wn < 1:
            raise ValueError("low-pass cutoff must be below Nyquist")
        b, a = butter(cfg.order, wn)
        for s, x in wf.signals.items():
            out[s] = np.asarray(filtfilt(b, a, x), dtype=np.float64)
    return Waveforms(dt=wf.dt, t0=wf.t0, signals=out, clipped=dict(wf.clipped))
