"""Synthetic double-pulse waveforms with known ground truth (SRS ANA-41, SIM-03).

Model (circuit-consistent, on a fine time grid, default 20 ps):
* Load inductor L across the complementary device: dI_L/dt = v_comp / L.
* Loop KVL: V_link = v_CE + v_comp + L_σ · di_C/dt, and i_C = I_L − i_D.
* Turn-on: current rises with a short di/dt ramp-in then constant di/dt; the diode recovers with a
  linear ramp to −I_rrm (t_a) and an exponential tail (τ_rr); v_CE shows the L_σ·di/dt dip, then
  collapses linearly once the recovery peak is passed.
* Turn-off: Miller delay, linear voltage rise, linear current fall with L_σ·di/dt overshoot,
  optional IGBT tail current, damped ringing.
* Gate: piecewise-linear with Miller plateau; complementary gate with Miller-induced bumps.

Ground truth is computed from the noise-free fine-grid model by a separate, simple implementation
(no debounce, no decimation) of the definitions — it does not call the analysis package. The
scope view (`acquire`) decimates, applies channel skew, noise, 12-bit quantisation and polarity.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import numpy as np

from autodpt.analysis.types import FloatArray, RawChannel, ShotMeta
from autodpt.config.models import AnalysisConfig
from autodpt.config.presets import Preset

# --------------------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthDevice:
    name: str
    dut_type: str  # "IGBT" | "MOSFET"
    v_on: float
    v_off: float
    v_miller: float
    t_g1: float          # gate ramp V_off ↔ V_miller
    t_g2: float          # gate ramp V_miller ↔ V_on
    d_drv_on: float      # AFG edge → gate starts rising
    d_drv_off: float
    # turn-on
    k_on: float          # di/dt (A/s)
    t_k: float           # di/dt ramp-in time
    i_rrm: float
    tau_rr: float
    t_vf: float          # v_CE collapse time
    v_sat: float
    # turn-off
    d_store: float       # extra Miller delay before v_CE rises
    t_vr: float
    k_off: float
    tail_frac: float     # IGBT tail current as fraction of I0
    tau_tail: float
    ring_f: float
    ring_tau: float
    ring_i_frac: float   # current ringing amplitude as fraction of I0 (SiC)
    peak_round: float    # rounding of the recovery peak, fraction of I_rrm
    # complementary gate
    comp_v_off: float
    comp_bump_pos: float
    comp_bump_neg: float


def igbt_device() -> SynthDevice:
    """Si IGBT module-like edges (HybridPACK-class, moderate R_g)."""
    return SynthDevice(
        name="synthetic IGBT", dut_type="IGBT", v_on=15.5, v_off=-6.5, v_miller=9.0,
        t_g1=60e-9, t_g2=80e-9, d_drv_on=300e-9, d_drv_off=300e-9,
        k_on=2e9, t_k=10e-9, i_rrm=120.0, tau_rr=60e-9, t_vf=80e-9, v_sat=1.3,
        d_store=300e-9, t_vr=100e-9, k_off=2e9, tail_frac=0.15, tau_tail=300e-9,
        ring_f=20e6, ring_tau=100e-9, ring_i_frac=0.0, peak_round=0.03,
        comp_v_off=-6.5, comp_bump_pos=1.4, comp_bump_neg=0.7)


def sic_device() -> SynthDevice:
    """SiC MOSFET module-like edges (fast, ringing, small recovery)."""
    return SynthDevice(
        name="synthetic SiC", dut_type="MOSFET", v_on=18.0, v_off=-5.0, v_miller=10.0,
        t_g1=15e-9, t_g2=20e-9, d_drv_on=200e-9, d_drv_off=200e-9,
        k_on=8e9, t_k=2e-9, i_rrm=30.0, tau_rr=8e-9, t_vf=20e-9, v_sat=0.6,
        d_store=50e-9, t_vr=25e-9, k_off=8e9, tail_frac=0.0, tau_tail=1e-9,
        ring_f=60e6, ring_tau=40e-9, ring_i_frac=0.01, peak_round=0.03,
        comp_v_off=-5.0, comp_bump_pos=2.0, comp_bump_neg=1.5)


@dataclass(frozen=True)
class SynthCircuit:
    v_dc: float = 400.0       # link voltage after the first pulse
    droop: float = 4.0        # V_pre − V_dc
    l_load: float = 20e-6
    l_sigma: float = 25e-9
    i_set: float = 200.0
    v_f: float = 1.5          # freewheel diode forward drop (inductor current decays during t2)


@dataclass(frozen=True)
class SynthTiming:
    t_pre: float = 2e-6
    t1: float | None = None   # None → L·I/V (+ gate delay compensation)
    t2: float = 3e-6
    t3: float = 3e-6
    t_post: float = 1.5e-6

    def resolve_t1(self, c: SynthCircuit, d: SynthDevice) -> float:
        """Commanded t1. If not given: L·I/V_avg minus the gate-path delay difference, i.e. what
        the pulse planner's learned correction converges to (SEQ-03)."""
        if self.t1 is not None:
            return self.t1
        t_eff = c.l_load * c.i_set / (c.v_dc + 0.5 * c.droop)
        delta = (d.d_drv_off + d.t_g2 + d.d_store + 0.5 * d.t_vr) - (d.d_drv_on + d.t_g1 + 0.5 * d.t_vf)
        return t_eff - delta


# --------------------------------------------------------------------------------------
# Fine-grid model
# --------------------------------------------------------------------------------------


@dataclass
class TrueShot:
    dt: float
    t: FloatArray
    sig: dict[str, FloatArray]   # vge, vce, ic, i_d, vcomp, il, vlink, vge_c, ic_comp
    ev: dict[str, float]         # event times and currents
    dev: SynthDevice
    circ: SynthCircuit
    timing: SynthTiming
    t1: float


def _cumtrapz(y: FloatArray, dt: float) -> FloatArray:
    return np.concatenate(([0.0], np.cumsum(0.5 * (y[1:] + y[:-1]) * dt)))


def generate_true(dev: SynthDevice, circ: SynthCircuit, timing: SynthTiming,
                  dt: float = 20e-12) -> TrueShot:
    t1 = timing.resolve_t1(circ, dev)
    e1 = timing.t_pre
    e2 = e1 + t1
    e3 = e2 + timing.t2
    e4 = e3 + timing.t3
    t_end = e4 + timing.t_post
    t = np.arange(0.0, t_end, dt)
    n = len(t)
    v_pre = circ.v_dc + circ.droop
    ls, lload = circ.l_sigma, circ.l_load

    vce = np.empty(n)
    ic = np.zeros(n)
    i_d = np.zeros(n)
    vcomp = np.zeros(n)
    il = np.zeros(n)
    vlink = np.full(n, circ.v_dc)

    def idx(x: float) -> int:
        return min(max(int(np.searchsorted(t, x)), 0), n)

    # ---- phase 1: first pulse, up to start of current fall --------------------------
    tg_on1 = e1 + dev.d_drv_on
    ti0_1 = tg_on1 + dev.t_g1
    tvfe_1 = ti0_1 + dev.t_vf
    tg_off1 = e2 + dev.d_drv_off
    tv0_1 = tg_off1 + dev.t_g2 + dev.d_store
    tv1_1 = tv0_1 + dev.t_vr
    vlink[:] = np.where(t < ti0_1, v_pre,
                        np.where(t < tv0_1, v_pre - circ.droop * (t - ti0_1) / (tv0_1 - ti0_1), circ.v_dc))
    a1 = idx(tv1_1)
    tt = t[:a1]
    v = np.where(tt < ti0_1, v_pre, np.interp(tt, [ti0_1, tvfe_1], [v_pre, dev.v_sat]))
    v = np.where(tt >= tv0_1, np.interp(tt, [tv0_1, tv1_1], [dev.v_sat, circ.v_dc + circ.v_f]), v)
    vce[:a1] = v
    vcomp[:a1] = vlink[:a1] - v
    il[:a1] = _cumtrapz(vcomp[:a1], dt) / lload
    ic[:a1] = il[:a1]

    # ---- turn-off (generic) -----------------------------------------------------------
    def turn_off(a: int, b: int, t_v1: float, i0: float, il_start: float) -> float:
        """Fill [a, b) from current-fall start t_v1; returns end of current fall."""
        tt = t[a:b]
        tf = t_v1 + (1.0 - dev.tail_frac) * i0 / dev.k_off
        x = tt - tf
        fall = i0 - dev.k_off * (tt - t_v1)
        after = (dev.tail_frac * i0 * np.exp(-np.maximum(x, 0) / dev.tau_tail)
                 + dev.ring_i_frac * i0 * np.exp(-np.maximum(x, 0) / dev.ring_tau)
                 * np.sin(2 * math.pi * dev.ring_f * np.maximum(x, 0)))
        i_c = np.where(tt < tf, fall, after)
        ring = ls * dev.k_off * np.exp(-np.maximum(x, 0) / dev.ring_tau) * np.cos(
            2 * math.pi * dev.ring_f * np.maximum(x, 0))
        v_c = np.where(tt < tf, circ.v_dc + circ.v_f + ls * dev.k_off, circ.v_dc + circ.v_f + ring)
        didt = np.gradient(i_c, dt)
        didt[tt < tf] = -dev.k_off
        v_cm = circ.v_dc - v_c - ls * didt
        vce[a:b] = v_c
        ic[a:b] = i_c
        vcomp[a:b] = v_cm
        il[a:b] = il_start + _cumtrapz(v_cm, dt) / lload
        i_d[a:b] = il[a:b] - i_c
        return tf

    i0_1 = float(il[a1 - 1])
    tg_on2 = e3 + dev.d_drv_on
    ti0_2 = tg_on2 + dev.t_g1
    b1 = idx(ti0_2)
    tfe_1 = turn_off(a1, b1, tv1_1, i0_1, i0_1)

    # ---- phase 3: second turn-on with recovery, conduction, start of 2nd turn-off ------
    i_on = float(il[b1 - 1])
    t_ramp_end = ti0_2 + dev.t_k
    i_at_ramp = 0.5 * dev.k_on * dev.t_k
    tpk = t_ramp_end + (i_on + dev.i_rrm - i_at_ramp) / dev.k_on
    tvfe_2 = tpk + dev.t_vf
    tg_off2 = e4 + dev.d_drv_off
    tv0_2 = tg_off2 + dev.t_g2 + dev.d_store
    tv1_2 = tv0_2 + dev.t_vr
    a3, b3 = b1, idx(tv1_2)
    tt = t[a3:b3]
    tau = tt - ti0_2
    rise = np.where(tau < dev.t_k, 0.5 * dev.k_on * tau**2 / dev.t_k, dev.k_on * (tau - 0.5 * dev.t_k))
    # diode current: ramp r(t) and recovery tail e(t) joined by a smooth maximum (rounded peak,
    # width w); i_D = max(r, e) would be the ideal corner at t_pk.
    r = i_on - rise
    e = -dev.i_rrm * np.exp(-(tt - tpk) / dev.tau_rr)
    w = dev.peak_round * dev.i_rrm
    idd = w * np.logaddexp(r / w, e / w)
    did = np.gradient(idd, dt)
    k_pk = int(np.searchsorted(tt, tpk))
    v_pk = circ.v_dc + circ.v_f + ls * float(did[min(k_pk, len(did) - 1)])
    v_dip = circ.v_dc + circ.v_f + ls * did  # KVL while the diode still conducts: v_comp = −V_F
    v_c = np.where(tt < tpk, v_dip, np.interp(tt, [tpk, tvfe_2], [v_pk, dev.v_sat]))
    v_c = np.where(tt >= tv0_2, np.interp(tt, [tv0_2, tv1_2], [dev.v_sat, circ.v_dc + circ.v_f]), v_c)
    v_cm = circ.v_dc - v_c + ls * did
    vce[a3:b3] = v_c
    vcomp[a3:b3] = v_cm
    il[a3:b3] = i_on + _cumtrapz(v_cm, dt) / lload
    i_d[a3:b3] = idd
    ic[a3:b3] = il[a3:b3] - idd

    # ---- phase 4: second turn-off and freewheel ----------------------------------------
    i0_2 = float(il[b3 - 1])
    tfe_2 = turn_off(b3, n, tv1_2, i0_2, i0_2)

    # ---- gate (DUT) -------------------------------------------------------------------
    bp_t = [0.0,
            tg_on1, tg_on1 + dev.t_g1, tvfe_1, tvfe_1 + dev.t_g2,
            tg_off1, tg_off1 + dev.t_g2, tfe_1, tfe_1 + dev.t_g1,
            tg_on2, tg_on2 + dev.t_g1, tvfe_2, tvfe_2 + dev.t_g2,
            tg_off2, tg_off2 + dev.t_g2, tfe_2, tfe_2 + dev.t_g1, t_end]
    bp_v = [dev.v_off,
            dev.v_off, dev.v_miller, dev.v_miller, dev.v_on,
            dev.v_on, dev.v_miller, dev.v_miller, dev.v_off,
            dev.v_off, dev.v_miller, dev.v_miller, dev.v_on,
            dev.v_on, dev.v_miller, dev.v_miller, dev.v_off, dev.v_off]
    vge = np.interp(t, bp_t, bp_v)

    # ---- complementary gate: Miller-induced bumps ---------------------------------------
    c_pos = tpk + 0.5 * dev.t_vf
    c_neg = tv0_1 + 0.5 * dev.t_vr
    vge_c = (dev.comp_v_off
             + dev.comp_bump_pos * np.exp(-0.5 * ((t - c_pos) / (dev.t_vf / 4)) ** 2)
             - dev.comp_bump_neg * np.exp(-0.5 * ((t - c_neg) / (dev.t_vr / 4)) ** 2))

    sig = {"vge": vge, "vce": vce, "ic": ic, "i_d": i_d, "vcomp": vcomp, "il": il,
           "vlink": vlink, "vge_c": vge_c, "ic_comp": -i_d}
    ev = {"e1": e1, "e2": e2, "e3": e3, "e4": e4, "ti0_1": ti0_1, "tv0_1": tv0_1, "tv1_1": tv1_1,
          "tfe_1": tfe_1, "ti0_2": ti0_2, "tpk_2": tpk, "tvfe_2": tvfe_2, "tv1_2": tv1_2,
          "tfe_2": tfe_2, "i0_1": i0_1, "i_on": i_on, "i0_2": i0_2}
    return TrueShot(dt=dt, t=t, sig=sig, ev=ev, dev=dev, circ=circ, timing=timing, t1=t1)


# --------------------------------------------------------------------------------------
# Ground truth (independent, simple implementation of the definitions on the fine grid)
# --------------------------------------------------------------------------------------


def _cross(t: FloatArray, x: FloatArray, level: float, rising: bool, t_from: float,
           t_to: float, last: bool = False) -> float:
    m = (t >= t_from) & (t <= t_to)
    ii = np.flatnonzero(m)
    if ii.size < 2:
        return math.nan
    xs = x[ii]
    b = xs >= level if rising else xs <= level
    k = np.flatnonzero(~b[:-1] & b[1:]) + 1
    if k.size == 0:
        return math.nan
    j = int(k[-1] if last else k[0])
    x0, x1 = xs[j - 1], xs[j]
    f = (level - x0) / (x1 - x0) if x1 != x0 else 0.0
    return float(t[ii[j - 1]] + f * (t[ii[j]] - t[ii[j - 1]]))


def _trapz_between(t: FloatArray, y: FloatArray, ta: float, tb: float) -> float:
    m = (t > ta) & (t < tb)
    tt = np.concatenate(([ta], t[m], [tb]))
    yy = np.concatenate(([np.interp(ta, t, y)], y[m], [np.interp(tb, t, y)]))
    return float(np.sum(0.5 * (yy[1:] + yy[:-1]) * np.diff(tt)))


def truth(ts: TrueShot, preset: Preset, cfg: AnalysisConfig) -> dict[str, float]:
    """Reference values for `preset` and the definition-related settings in `cfg`."""
    t, s, d, c = ts.t, ts.sig, ts.dev, ts.circ
    out: dict[str, float] = {}
    lvl50 = 0.5 * (d.v_on + d.v_off)
    e2, e3, e4 = (ts.ev[k] for k in ("e2", "e3", "e4"))
    g2 = _cross(t, s["vge"], lvl50, False, e2, e3)
    g3 = _cross(t, s["vge"], lvl50, True, e3, e4)
    g4 = _cross(t, s["vge"], lvl50, False, e4, t[-1])
    vdc = c.v_dc + c.v_f   # off-state DUT voltage while the diode freewheels (what ANA-13 measures)
    out.update(vdc_on=vdc, vdc_off=vdc, vdc_pre=c.v_dc + c.droop, vge_on_plateau=d.v_on,
               vge_off_plateau=d.v_off)
    w_off = (min(e2, g2) - cfg.window_pre_s, g2 + min(cfg.window_post_max_s, g3 - g2))
    w_on = (min(e3, g3) - cfg.window_pre_s, g3 + min(cfg.window_post_max_s, g4 - g3))
    t_v90 = _cross(t, s["vce"], 0.9 * vdc, True, *w_off)
    i_off = float(np.interp(t_v90, t, s["il"]))  # physical DUT/inductor current at t_v90
    i_on = ts.ev["i_on"]
    out.update(i_off=i_off, i_on=i_on, t_v90_off=t_v90)

    def gate(frac: float) -> float:
        return frac * d.v_on if preset.gate_reference == "zero" else d.v_off + frac * (d.v_on - d.v_off)

    keymap = {"vge": "vge", "ic": "ic", "vce": "vce"}
    p_ce = s["vce"] * s["ic"]
    for key, p in preset.params.items():
        w = w_on if p.event == "on" else w_off
        iref = i_on if p.event == "on" else i_off

        def lvl(pt: object, iref: float = iref) -> float:
            sig_ = pt.signal  # type: ignore[attr-defined]
            lv_ = pt.level  # type: ignore[attr-defined]
            return gate(lv_) if sig_ == "vge" else (lv_ * iref if sig_ == "ic" else lv_ * vdc)

        t_s = _cross(t, s[keymap[p.start.signal]], lvl(p.start), p.start.direction == "rising", *w,
                     last=p.start.search == "right")
        t_e = _cross(t, s[keymap[p.end.signal]], lvl(p.end), p.end.direction == "rising", t_s, w[1],
                     last=p.end.search == "right")
        out[key] = t_e - t_s if p.kind == "time" else _trapz_between(t, p_ce, t_s, t_e)

    lo, hi = sorted(cfg.slope_levels)
    ta_ = _cross(t, s["ic"], lo * i_on, True, *w_on)
    tb_ = _cross(t, s["ic"], hi * i_on, True, ta_, w_on[1])
    out["didt_on"] = (hi - lo) * i_on / (tb_ - ta_)
    ta_ = _cross(t, s["vce"], hi * vdc, False, *w_on)
    tb_ = _cross(t, s["vce"], lo * vdc, False, ta_, w_on[1])
    out["dvdt_on"] = -(hi - lo) * vdc / (tb_ - ta_)
    ta_ = _cross(t, s["vce"], lo * vdc, True, *w_off)
    tb_ = _cross(t, s["vce"], hi * vdc, True, ta_, w_off[1])
    out["dvdt_off"] = (hi - lo) * vdc / (tb_ - ta_)
    ta_ = _cross(t, s["ic"], hi * i_off, False, *w_off)
    tb_ = _cross(t, s["ic"], lo * i_off, False, ta_, w_off[1])
    out["didt_off"] = -(hi - lo) * i_off / (tb_ - ta_)

    m_off = (t >= w_off[0]) & (t <= w_off[1])
    m_on = (t >= w_on[0]) & (t <= w_on[1])
    out["vce_peak_off"] = float(np.max(s["vce"][m_off]))
    out["overshoot_dv"] = out["vce_peak_off"] - vdc
    out["ic_peak_on"] = float(np.max(s["ic"][m_on]))

    # reverse recovery from the model's own diode current
    idd, vd = s["i_d"], s["vcomp"]
    t0 = _cross(t, idd, 0.0, False, *w_on)
    mm = m_on & (t >= t0)
    kp = int(np.flatnonzero(mm)[np.argmin(idd[mm])])
    irrm = -float(idd[kp])
    tpk = float(t[kp])
    rh, rl = max(cfg.rr_levels), min(cfg.rr_levels)
    t_h = _cross(t, idd, -rh * irrm, True, tpk, w_on[1])
    t_l = _cross(t, idd, -rl * irrm, True, t_h, w_on[1])
    t_end = t_l + rl * (t_l - t_h) / (rh - rl)
    out.update(t_rr=t_end - t0, i_rrm=irrm, t_a=tpk - t0, t_b=t_end - tpk,
               softness=(t_end - tpk) / (tpk - t0),
               q_rr=_trapz_between(t, np.maximum(-idd, 0.0), t0, t_end),
               e_rec=_trapz_between(t, vd * -idd, t0, t_end))
    out["l_sigma_on"] = c.l_sigma
    out["comp_vce_max_on"] = float(np.max(s["vcomp"][m_on]))
    out["comp_vge_max_on"] = float(np.max(s["vge_c"][m_on]))
    out["comp_vge_min_on"] = float(np.min(s["vge_c"][m_on]))
    out["comp_vge_max_off"] = float(np.max(s["vge_c"][m_off]))
    out["comp_vge_min_off"] = float(np.min(s["vge_c"][m_off]))
    out["comp_i_max_off"] = float(np.max(s["ic_comp"][m_off]))
    return out


# --------------------------------------------------------------------------------------
# Scope view
# --------------------------------------------------------------------------------------

_MAP = {"VGE_DUT": "vge", "VCE_DUT": "vce", "IC_DUT": "ic", "VGE_COMP": "vge_c", "VCE_COMP": "vcomp",
        "IC_COMP": "ic_comp", "IL": "il", "VDC": "vlink"}
DEFAULT_CHANNELS: tuple[str, ...] = ("VGE_DUT", "VCE_DUT", "IC_DUT", "VGE_COMP", "VCE_COMP", "IC_COMP")


@dataclass(frozen=True)
class SynthAcq:
    fs: float = 10e9
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    skew: Mapping[str, float] = field(default_factory=dict)       # + = channel lags
    noise_rms: Mapping[str, float] = field(default_factory=dict)  # physical units
    invert: Mapping[str, bool] = field(default_factory=dict)
    range_override: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    bits: int = 12
    seed: int = 1


def acquire(ts: TrueShot, acq: SynthAcq) -> tuple[list[RawChannel], float, float]:
    """Return (raw channels, dt, t0) as the scope would deliver them."""
    decim = round(1.0 / (acq.fs * ts.dt))
    if decim < 1 or abs(decim * ts.dt * acq.fs - 1.0) > 1e-6:
        raise ValueError("fs must be an integer fraction of the fine-grid rate")
    t_s = ts.t[::decim]
    rng = np.random.default_rng(acq.seed)
    out: list[RawChannel] = []
    for ch in acq.channels:
        x_true = ts.sig[_MAP[ch]]
        tau = acq.skew.get(ch, 0.0)
        x = np.interp(t_s - tau, ts.t, x_true) if tau else x_true[::decim].copy()
        sigma = acq.noise_rms.get(ch, 0.0)
        if sigma:
            x = x + rng.normal(0.0, sigma, size=x.shape)
        inv = acq.invert.get(ch, False)
        if inv:
            x = -x
        if ch in acq.range_override:
            lo, hi = acq.range_override[ch]
            lo, hi = (-hi, -lo) if inv else (lo, hi)
        else:
            lo_t, hi_t = float(np.min(x_true)), float(np.max(x_true))
            if inv:
                lo_t, hi_t = -hi_t, -lo_t
            pad = 0.08 * (hi_t - lo_t) + 6.0 * sigma + 1e-9  # like a sensible V/div setting
            lo, hi = lo_t - pad, hi_t + pad
        levels = 2 ** acq.bits
        step = (hi - lo) / levels
        offset = 0.5 * (hi + lo)
        q = np.clip(np.round((x - offset) / step), -levels // 2, levels // 2 - 1)
        shift = 1 << (16 - acq.bits)  # ADC data left-aligned in 16-bit words
        codes = (q * shift).astype(np.int16)
        out.append(RawChannel(signal=ch, codes=codes, gain=step / shift, offset=offset, invert=inv,
                              code_min=-(levels // 2) * shift, code_max=(levels // 2 - 1) * shift))
    return out, float(ts.dt * decim), 0.0


def meta_for(ts: TrueShot, single_skew: Mapping[str, float] | None = None,
             **kw: object) -> ShotMeta:
    """ShotMeta consistent with a synthetic shot."""
    base = ShotMeta(t_pre=ts.timing.t_pre, t1=ts.t1, t2=ts.timing.t2, t3=ts.timing.t3,
                    l_load=ts.circ.l_load, i_set=ts.circ.i_set, v_set=ts.circ.v_dc,
                    dut_type="IGBT" if ts.dev.dut_type == "IGBT" else "MOSFET",
                    single_skew=dict(single_skew or {}))
    return replace(base, **kw)  # type: ignore[arg-type]
