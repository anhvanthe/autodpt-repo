"""De-skew methods (ANA-20 … ANA-24, ANA-42 skew tolerance)."""

from __future__ import annotations

import pytest

from autodpt.analysis import synth
from autodpt.analysis.analyse import analyse_point, analyse_shot
from autodpt.analysis.preprocess import waveforms_from_raw
from autodpt.config.models import AnalysisConfig
from autodpt.config.presets import load_preset
from tests.conftest import NOISE, true_shot

PRESET = {"igbt": "IEC60747-9", "sic": "IEC60747-8"}


def _shot(case: str, tau: float, noise: bool, seed: int = 1, single: float | None = None):  # type: ignore[no-untyped-def]
    ts = true_shot(case)
    acq = synth.SynthAcq(bits=16 if not noise else 12, skew={"IC_DUT": tau},
                         noise_rms=NOISE[case] if noise else {}, seed=seed)
    raw, dt, t0 = synth.acquire(ts, acq)
    meta = synth.meta_for(ts, single_skew={"IC_DUT": tau if single is None else single})
    return waveforms_from_raw(raw, dt, t0), meta, ts


@pytest.mark.req("ANA-22", "ANA-42")
@pytest.mark.parametrize("case", ["igbt", "sic"])
@pytest.mark.parametrize("tau", [-6.3e-9, 0.0, 4.7e-9])
def test_statistic_skew_ideal(case: str, tau: float, cfg: AnalysisConfig) -> None:
    wf, meta, _ = _shot(case, tau, noise=False)
    est = analyse_shot(wf, meta, cfg, load_preset(PRESET[case])).skew_estimate
    assert est is not None and est.ok, est
    assert abs(est.tau - tau) <= 0.2e-9


@pytest.mark.slow
@pytest.mark.req("ANA-22", "ANA-42")
@pytest.mark.parametrize("case", ["igbt", "sic"])
@pytest.mark.parametrize("tau", [-6.3e-9, 4.7e-9])
def test_statistic_skew_noisy(case: str, tau: float, cfg: AnalysisConfig) -> None:
    for seed in range(4):
        wf, meta, _ = _shot(case, tau, noise=True, seed=seed)
        est = analyse_shot(wf, meta, cfg, load_preset(PRESET[case])).skew_estimate
        assert est is not None and est.ok, est
        assert abs(est.tau - tau) <= 0.5e-9, (seed, est)


@pytest.mark.req("ANA-21", "ANA-24", "ANA-02")
@pytest.mark.parametrize("case", ["igbt", "sic"])
def test_single_deskew_restores_energies(case: str, cfg: AnalysisConfig) -> None:
    """With a known skew, 'single' matches the zero-skew truth; 'none' is visibly wrong."""
    tau = 4.0e-9
    wf, meta, ts = _shot(case, tau, noise=False)
    p = load_preset(PRESET[case])
    truth = synth.truth(ts, p, cfg)
    r = analyse_shot(wf, meta, cfg, p)
    for key in ("e_on", "e_off"):
        assert abs(r.values["single"][key] / truth[key] - 1) < 0.005
        assert abs(r.values["statistic"][key] / truth[key] - 1) < 0.005
        assert abs(r.values["none"][key] / truth[key] - 1) > 0.02  # skew matters
    assert r.values["single"]["skew"] == pytest.approx(tau)


@pytest.mark.slow
@pytest.mark.req("ANA-23")
def test_point_pooling(cfg: AnalysisConfig) -> None:
    tau = -3.1e-9
    shots = [_shot("sic", tau, noise=True, seed=s)[:2] for s in range(5)]
    results, summary = analyse_point(shots, cfg, load_preset("IEC60747-8"))
    assert summary["n_valid_skew"] == 5
    assert abs(summary["tau_stat"] - tau) <= 0.3e-9
    assert all(r.skews["statistic"] == pytest.approx(summary["tau_stat"]) for r in results)


@pytest.mark.req("ANA-22", "ANA-30")
def test_skew_outside_search_range_is_flagged(cfg: AnalysisConfig) -> None:
    """30 ns skew with no prior (single = 0): outside ±20 ns → must not be trusted."""
    wf, meta, _ = _shot("sic", 30e-9, noise=False, single=0.0)
    r = analyse_shot(wf, meta, cfg, load_preset("IEC60747-8"))
    assert r.skew_estimate is not None and not r.skew_estimate.ok
    assert "DESKEW_ILL_CONDITIONED" in r.flags
    assert "statistic" not in r.values


@pytest.mark.req("ANA-22")
def test_search_centred_on_single_deskew(cfg: AnalysisConfig) -> None:
    """A large fixed probe delay is handled when the single de-skew value is the search centre."""
    wf, meta, _ = _shot("sic", 33e-9, noise=True, single=28e-9)
    est = analyse_shot(wf, meta, cfg, load_preset("IEC60747-8")).skew_estimate
    assert est is not None and est.ok and abs(est.tau - 33e-9) <= 0.5e-9
