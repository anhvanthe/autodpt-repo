"""Unit tests for crossings, integration and pre-processing (ANA-05, ANA-08, ANA-14, ANA-16)."""

from __future__ import annotations

import numpy as np
import pytest

from autodpt.analysis.edges import find_crossing, integrate, linear_fit_at
from autodpt.analysis.preprocess import (
    apply_filter,
    remove_current_baseline,
    rogowski_droop_compensation,
    shift_signal,
    waveforms_from_raw,
)
from autodpt.analysis.types import RawChannel, Waveforms
from autodpt.config.models import FilterCfg

DT = 1e-9


@pytest.mark.req("ANA-14")
def test_crossing_interpolates_exactly_on_ramp() -> None:
    x = np.arange(100, dtype=float)  # 1 unit per sample
    t = find_crossing(x, 0.0, DT, 42.3, "rising", 0, 100)
    assert t == pytest.approx(42.3 * DT)
    assert find_crossing(x[::-1].copy(), 0.0, DT, 42.3, "falling", 0, 100) == pytest.approx((99 - 42.3) * DT)


@pytest.mark.req("ANA-14")
def test_debounce_rejects_glitch() -> None:
    x = np.zeros(100)
    x[20] = 1.0  # 1-sample glitch
    x[60:] = 1.0
    assert find_crossing(x, 0.0, DT, 0.5, "rising", 0, 100, debounce=3) == pytest.approx(59.5 * DT)
    assert find_crossing(x, 0.0, DT, 0.5, "rising", 0, 100, debounce=1) == pytest.approx(19.5 * DT)


@pytest.mark.req("ANA-14")
def test_search_from_right() -> None:
    x = np.zeros(100)
    x[20:40] = 1.0
    x[60:80] = 1.0
    assert find_crossing(x, 0.0, DT, 0.5, "rising", 0, 100, search="right") == pytest.approx(59.5 * DT)


@pytest.mark.req("ANA-14")
def test_hysteresis_ignores_noise_on_ramp_towards_level() -> None:
    """A noisy ramp *rising* through a level must not produce a *falling* crossing."""
    rng = np.random.default_rng(0)
    n = 4000
    ramp = np.linspace(170.0, 200.0, n)
    fall = np.linspace(200.0, 0.0, 200)
    x = np.concatenate((ramp, fall)) + rng.normal(0, 1.0, n + 200)
    t_plain = find_crossing(x, 0.0, DT, 180.0, "falling", 0, len(x), debounce=3)
    t_h = find_crossing(x, 0.0, DT, 180.0, "falling", 0, len(x), debounce=3, hysteresis=4.0)
    assert t_plain is not None and t_plain < 3000 * DT          # fooled by noise
    assert t_h is not None and abs(t_h - (n + 20) * DT) < 5 * DT  # true fall (10 % into the ramp)


@pytest.mark.req("ANA-16")
def test_integrate_partial_intervals() -> None:
    x = np.arange(11, dtype=float)  # y = t/DT
    # ∫ y dt from 2.5 to 7.25 samples = (7.25² − 2.5²)/2 · DT
    assert integrate(x, 0.0, DT, 2.5 * DT, 7.25 * DT) == pytest.approx((7.25**2 - 2.5**2) / 2 * DT)
    assert integrate(x, 0.0, DT, 3.2 * DT, 3.7 * DT) == pytest.approx((3.7**2 - 3.2**2) / 2 * DT)


@pytest.mark.req("ANA-11")
def test_linear_fit_extrapolation() -> None:
    x = 5.0 + 2.0 * np.arange(50)
    assert linear_fit_at(x, 0.0, DT, 10, 40, 60 * DT) == pytest.approx(125.0)


@pytest.mark.req("SCO-02", "CFG-02")
def test_raw_scaling_polarity_and_clipping() -> None:
    codes = np.array([-16, 0, 16, 32752], dtype=np.int16)
    ch = RawChannel("IC_DUT", codes, gain=0.5, offset=10.0, invert=True, code_min=-32768, code_max=32752)
    wf = waveforms_from_raw([ch], dt=DT, t0=0.0)
    assert wf.signals["IC_DUT"].tolist() == [-2.0, -10.0, -18.0, -16386.0]
    assert wf.clipped["IC_DUT"] is True


@pytest.mark.req("ANA-08")
def test_baseline_removal_only_on_currents() -> None:
    wf = Waveforms(dt=DT, t0=0.0, signals={"IC_DUT": np.full(100, 3.0), "VCE_DUT": np.full(100, 400.0)})
    removed = remove_current_baseline(wf, slice(0, 50))
    assert removed == {"IC_DUT": 3.0}
    assert np.allclose(wf.signals["IC_DUT"], 0.0) and np.allclose(wf.signals["VCE_DUT"], 400.0)


@pytest.mark.req("ANA-21", "ANA-08")
def test_shift_signal_sign_convention() -> None:
    """A channel lagging by tau is corrected by y(t) = x(t + tau)."""
    t = np.arange(200) * DT
    true = np.clip((t - 100 * DT) / (10 * DT), 0, 1)
    lagging = np.clip((t - 103.5 * DT) / (10 * DT), 0, 1)
    # exact except at the two ramp corners (linear interpolation across a corner)
    assert np.allclose(shift_signal(lagging, DT, 3.5 * DT)[:190], true[:190], atol=0.03)
    assert np.allclose(shift_signal(lagging, DT, 3.5 * DT)[102:109], true[102:109], atol=1e-12)


@pytest.mark.req("ANA-05")
def test_rogowski_droop_compensation_recovers_step() -> None:
    """A first-order high-pass (coil droop) applied to a current step is undone."""
    tau = 50e-6
    dt = 10e-9
    n = 20000
    x = np.zeros(n)
    x[1000:] = 100.0
    # discrete HP consistent with trapezoidal integration: y = x − (1/τ)∫y
    y = np.empty(n)
    acc = 0.0
    y_prev = 0.0
    for i in range(n):
        y_i = (x[i] - (acc + 0.5 * y_prev * dt) / tau) / (1 + 0.5 * dt / tau) if i else x[0]
        if i:
            acc += 0.5 * (y_prev + y_i) * dt
        y[i] = y_i
        y_prev = y_i
    assert y[-1] < 70.0  # visible droop (~30 % after 190 µs)
    rec = rogowski_droop_compensation(y, dt, tau)
    assert np.allclose(rec, x, atol=0.05)


@pytest.mark.req("ANA-03")
def test_filters_preserve_dc_and_length() -> None:
    wf = Waveforms(dt=DT, t0=0.0, signals={"VCE_DUT": np.full(500, 400.0)})
    for f in (FilterCfg(kind="moving_average", n_samples=5), FilterCfg(kind="lowpass", cutoff_hz=50e6)):
        out = apply_filter(wf, f).signals["VCE_DUT"]
        assert out.shape == (500,) and np.allclose(out, 400.0)
    with pytest.raises(ValueError):
        apply_filter(wf, FilterCfg(kind="lowpass", cutoff_hz=1e9))  # above Nyquist for 1 GS/s
