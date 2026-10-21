"""Gate edges, plateaus and reference levels (SRS ANA-09 … ANA-13)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autodpt.analysis.edges import find_crossing, integrate, linear_fit_at
from autodpt.analysis.preprocess import baseline_window
from autodpt.analysis.types import AnalysisError, ShotMeta, Waveforms
from autodpt.config.models import AnalysisConfig

_GATE_PRE = 0.2e-6  # gate edges are searched from 0.2 µs before the commanded edge


@dataclass
class Levels:
    vge_on: float
    vge_off: float
    gate_edges: tuple[float, float, float, float]  # 50 % crossings: on1, off1, on2, off2
    vdc_pre: float
    vdc_off: float
    vdc_on: float
    t_v90_off: float
    i_off: float
    i_on: float
    win_off: tuple[int, int]
    win_on: tuple[int, int]
    hyst: dict[str, float] = field(default_factory=dict)   # crossing hysteresis per signal (ANA-14)
    flags: list[str] = field(default_factory=list)

    def h(self, signal: str) -> float:
        return self.hyst.get(signal, 0.0)

    @property
    def t2_eff(self) -> float:
        return self.gate_edges[2] - self.gate_edges[1]


def _median(x: np.ndarray, a: int, b: int) -> float:
    if b - a < 1:
        raise AnalysisError("EDGE_MISSING", "empty reference window")
    return float(np.median(x[a:b]))


def compute_levels(wf: Waveforms, meta: ShotMeta, cfg: AnalysisConfig) -> Levels:
    flags: list[str] = []
    vge = wf.signals["VGE_DUT"]
    vce = wf.signals["VCE_DUT"]
    ic = wf.signals["IC_DUT"]
    base = baseline_window(wf, meta.t_pre, cfg.baseline_window_frac)
    e1, e2, e3, e4 = meta.edges_commanded

    # noise per channel from the baseline window → crossing hysteresis (ANA-14)
    hyst = {s: cfg.hysteresis_sigma * float(np.std(x[base])) for s, x in wf.signals.items()}

    # ANA-10 plateaus
    vge_off = float(np.median(vge[base]))
    vge_on = _median(vge, wf.index(e1 + 0.7 * meta.t1), wf.index(e2))
    lvl50 = 0.5 * (vge_on + vge_off)

    # ANA-09 gate edges (50 % of swing), searched after each commanded edge
    commanded = (e1, e2, e3, e4)
    directions = ("rising", "falling", "rising", "falling")
    edges: list[float] = []
    for k, (ec, d) in enumerate(zip(commanded, directions, strict=True)):
        stop_t = commanded[k + 1] if k < 3 else ec + cfg.gate_search_s
        stop_t = min(stop_t, ec + cfg.gate_search_s)
        tc = find_crossing(vge, wf.t0, wf.dt, lvl50, d, wf.index(ec - _GATE_PRE), wf.index(stop_t),
                           cfg.debounce_samples, hysteresis=hyst["VGE_DUT"])
        if tc is None:
            raise AnalysisError("EDGE_MISSING", f"gate edge {k + 1} ({d}) not found near {ec:.3e} s")
        edges.append(tc)
    g1, g2, g3, g4 = edges
    t2_eff = g3 - g2

    # ANA-13 DC-link reference (VDC channel preferred)
    vref = wf.signals["VDC"] if wf.has("VDC") else vce
    vdc_pre = float(np.median(vref[base]))
    vdc_off = _median(vref, wf.index(g2 + 0.50 * t2_eff), wf.index(g2 + 0.75 * t2_eff))
    vdc_on = _median(vref, wf.index(g3 - 0.25 * t2_eff), wf.index(g3 - 0.05 * t2_eff))

    # Event windows start before the *commanded* (AFG) edge: with a long Miller plateau (IGBT) the
    # 90 % V_GE crossing lies far ahead of the 50 % gate edge.
    win_off = (wf.index(min(e2, g2) - cfg.window_pre_s), wf.index(g2 + min(cfg.window_post_max_s, t2_eff)))
    win_on = (wf.index(min(e3, g3) - cfg.window_pre_s), wf.index(g3 + min(cfg.window_post_max_s, g4 - g3)))

    # ANA-11 switching currents.
    # I_off = DUT current at t_v90 (V_CE at 90 % of V_DC,off, rising). The conduction ramp is fitted
    # during t1 and extrapolated only to the start of the voltage rise (t_v10); during the rise the
    # inductor sees V_DC − v_CE instead of V_DC, so the current gain is integrated from the
    # measured v_CE:  I_off = fit(t_v10) + (1/L)·∫[t_v10, t_v90] (V_DC,off − v_CE) dt.
    db = cfg.debounce_samples
    hv = hyst["VCE_DUT"]
    t_v10 = find_crossing(vce, wf.t0, wf.dt, 0.1 * vdc_off, "rising", win_off[0], win_off[1], db,
                          hysteresis=hv)
    t_v90 = None
    if t_v10 is not None:
        t_v90 = find_crossing(vce, wf.t0, wf.dt, 0.9 * vdc_off, "rising", wf.index(t_v10) - 1,
                              win_off[1], db, hysteresis=hv)
    if t_v10 is None or t_v90 is None:
        flags.append("TV90_NOT_FOUND")
        t_v10 = t_v90 = g2
    span = g2 - g1
    ia = wf.index(g1 + cfg.ioff_fit_window_frac[0] * span)
    ib = wf.index(g1 + cfg.ioff_fit_window_frac[1] * span - cfg.ioff_fit_guard_s)
    gain = integrate(vdc_off - vce, wf.t0, wf.dt, t_v10, t_v90) / meta.l_load
    i_off = linear_fit_at(ic, wf.t0, wf.dt, ia, ib, t_v10) + gain

    if wf.has("IL"):
        il = wf.signals["IL"]
        i_on = linear_fit_at(il, wf.t0, wf.dt, wf.index(g3 - 0.4 * t2_eff),
                             wf.index(g3 - cfg.ioff_fit_guard_s), g3)
    else:
        # freewheel decay during the off time; the inductor current stops changing once the
        # DUT voltage has risen (t_v90), so the decay interval is t_v90 … second turn-on.
        i_on = i_off - cfg.v_f_est * (g3 - t_v90) / meta.l_load
        flags.append("I_ON_FROM_I_OFF")

    return Levels(vge_on=vge_on, vge_off=vge_off, gate_edges=(g1, g2, g3, g4), vdc_pre=vdc_pre,
                  vdc_off=vdc_off, vdc_on=vdc_on, t_v90_off=t_v90, i_off=i_off, i_on=i_on,
                  win_off=win_off, win_on=win_on, hyst=hyst, flags=flags)
