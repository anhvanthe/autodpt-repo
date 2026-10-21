"""Threshold crossings and integration (SRS ANA-09, ANA-14, ANA-16)."""

from __future__ import annotations

import numpy as np

from autodpt.analysis.types import FloatArray


def find_crossing(x: FloatArray, t0: float, dt: float, level: float, direction: str,
                  i_start: int, i_stop: int, debounce: int = 3, search: str = "left",
                  hysteresis: float = 0.0) -> float | None:
    """Time at which `x` crosses `level` in `direction` ('rising'|'falling') within samples
    [i_start, i_stop).

    A crossing is a transition from not-beyond to beyond the level. It is accepted only if
    * the signal stays beyond the level for `debounce` samples (or until the window end), and
    * (Schmitt trigger) since the previous accepted crossing (or the window start) the signal was on
      the other side of the level by at least `hysteresis` for `debounce` consecutive samples — so
      noise on a slow ramp *towards* the level cannot fake a crossing in the opposite direction.
      Use a few × the channel's noise rms (ANA-14).
    The time is linearly interpolated between the two samples around the transition.
    `search='left'` returns the first accepted crossing, 'right' the last. None if none found.
    """
    i_start = max(int(i_start), 1)
    i_stop = min(int(i_stop), len(x))
    if i_stop - i_start < 2:
        return None
    seg = x[i_start - 1:i_stop]
    rising = direction == "rising"
    beyond = seg >= level if rising else seg <= level
    cand = np.flatnonzero(~beyond[:-1] & beyond[1:]) + 1  # index into seg of first beyond sample
    if cand.size == 0:
        return None
    n = len(beyond)
    arm_ok = None
    if hysteresis > 0.0:
        # sustained arming: `debounce` consecutive samples on the other side of level ∓ hysteresis
        arm = (seg <= level - hysteresis) if rising else (seg >= level + hysteresis)
        w = max(debounce, 1)
        csum = np.concatenate(([0], np.cumsum(arm.astype(np.int64))))
        arm_ok = (csum[w:] - csum[:-w]) >= w          # arm_ok[i]: arm[i:i+w] all True
    accepted: list[int] = []
    scan_from = 0  # Schmitt trigger: arming is searched since the last accepted crossing
    for k in cand:
        if arm_ok is not None:
            stop = k - max(debounce, 1) + 1
            if stop <= scan_from or not bool(np.any(arm_ok[scan_from:stop])):
                continue
        end = min(k + debounce, n)
        if not bool(np.all(beyond[k:end])):
            continue  # stays armed; the next candidate may be the real crossing
        accepted.append(int(k))
        scan_from = int(k)
        if search == "left":
            break
    if not accepted:
        return None
    kk = accepted[0] if search == "left" else accepted[-1]
    x0, x1 = float(seg[kk - 1]), float(seg[kk])
    frac = 0.0 if x1 == x0 else (level - x0) / (x1 - x0)
    i_abs = i_start - 1 + (kk - 1)
    return float(t0 + (i_abs + frac) * dt)


def interp_at(x: FloatArray, t0: float, dt: float, t: float) -> float:
    pos = (t - t0) / dt
    i = int(np.floor(pos))
    i = min(max(i, 0), len(x) - 2)
    f = pos - i
    return float(x[i] * (1.0 - f) + x[i + 1] * f)


def integrate(y: FloatArray, t0: float, dt: float, ta: float, tb: float) -> float:
    """Trapezoidal ∫ y dt from ta to tb including partial first/last intervals (ANA-16)."""
    if tb <= ta:
        return 0.0
    pa = (ta - t0) / dt
    pb = (tb - t0) / dt
    ia = int(np.ceil(pa))
    ib = int(np.floor(pb))
    ya = interp_at(y, t0, dt, ta)
    yb = interp_at(y, t0, dt, tb)
    if ib < ia:  # both limits inside one sample interval
        return 0.5 * (ya + yb) * (tb - ta)
    inner = y[ia:ib + 1]
    total = 0.5 * (ya + inner[0]) * ((ia - pa) * dt)
    if inner.size > 1:
        total += float(np.sum(0.5 * (inner[1:] + inner[:-1]))) * dt
    total += 0.5 * (inner[-1] + yb) * ((pb - ib) * dt)
    return float(total)


def linear_fit_at(x: FloatArray, t0: float, dt: float, i_a: int, i_b: int, t_eval: float) -> float:
    """Least-squares line through x[i_a:i_b] evaluated at t_eval (ANA-11)."""
    if i_b - i_a < 3:
        return float("nan")
    tt = t0 + dt * np.arange(i_a, i_b, dtype=np.float64)
    tm = float(np.mean(tt))
    k, c = np.polyfit(tt - tm, x[i_a:i_b], 1)
    return float(k * (t_eval - tm) + c)
