"""Validation flags and error handling (ANA-04, ANA-30, ANA-27)."""

from __future__ import annotations

import numpy as np
import pytest

from autodpt.analysis import synth
from autodpt.analysis.analyse import analyse_shot
from autodpt.analysis.preprocess import waveforms_from_raw
from autodpt.config.models import AnalysisConfig, FilterCfg
from autodpt.config.presets import load_preset
from tests.conftest import true_shot

P9 = "IEC60747-9"


def _analyse(acq: synth.SynthAcq, cfg: AnalysisConfig, case: str = "igbt", **meta_kw: object):  # type: ignore[no-untyped-def]
    ts = true_shot(case)
    raw, dt, t0 = synth.acquire(ts, acq)
    wf = waveforms_from_raw(raw, dt, t0)
    preset = load_preset(P9 if case == "igbt" else "IEC60747-8")
    return analyse_shot(wf, synth.meta_for(ts, **meta_kw), cfg, preset)


@pytest.mark.req("SCO-02", "ANA-30")
def test_clipping_makes_shot_invalid(cfg: AnalysisConfig) -> None:
    r = _analyse(synth.SynthAcq(bits=12, range_override={"VCE_DUT": (-20.0, 430.0)}), cfg)
    assert "CLIPPED:VCE_DUT" in r.flags and r.validity == "INVALID"


@pytest.mark.req("ANA-30", "SEQ-03")
def test_current_out_of_tolerance(cfg: AnalysisConfig) -> None:
    r = _analyse(synth.SynthAcq(bits=16), cfg, i_set=250.0)
    assert "CURRENT_OUT_OF_TOL" in r.flags and r.validity == "INVALID"


@pytest.mark.req("ANA-30")
def test_vdc_out_of_tolerance(cfg: AnalysisConfig) -> None:
    r = _analyse(synth.SynthAcq(bits=16), cfg, v_set=450.0)
    assert "VDC_OUT_OF_TOL" in r.flags


@pytest.mark.req("ANA-27")
def test_complementary_gate_warnings(cfg: AnalysisConfig) -> None:
    # synthetic IGBT: comp gate peaks at −5.1 V (on) and dips to −7.2 V (off)
    r = _analyse(synth.SynthAcq(bits=16), cfg, comp_vth_min=-5.5, comp_vge_min_rating=-7.0)
    assert "COMP_GATE_NEAR_VTH" in r.flags and "COMP_GATE_UNDERSHOOT" in r.flags
    assert r.validity == "WARNING"


@pytest.mark.req("ANA-30")
def test_bandwidth_limited_edge(cfg: AnalysisConfig) -> None:
    # SiC current rises 10–90 % in ~30 ns; a 30 MHz Rogowskiclass probe (t_r ≈ 12 ns) is too slow
    r = _analyse(synth.SynthAcq(bits=16), cfg, case="sic", probe_rise_time={"IC_DUT": 12e-9})
    assert "BW_LIMITED_EDGE:IC_DUT" in r.flags


@pytest.mark.req("ANA-09", "ANA-30")
def test_missing_gate_edge_is_reported(cfg: AnalysisConfig) -> None:
    ts = true_shot("igbt")
    raw, dt, t0 = synth.acquire(ts, synth.SynthAcq(bits=16))
    wf = waveforms_from_raw(raw, dt, t0)
    wf.signals["VGE_DUT"] = np.full_like(wf.signals["VGE_DUT"], -6.5)
    r = analyse_shot(wf, synth.meta_for(ts), cfg, load_preset(P9))
    assert r.flags == ["EDGE_MISSING"] and r.validity == "INVALID" and r.values == {}


@pytest.mark.req("ANA-30")
def test_droop_warning() -> None:
    ts = synth.generate_true(synth.igbt_device(), synth.SynthCircuit(droop=20.0), synth.SynthTiming())
    raw, dt, t0 = synth.acquire(ts, synth.SynthAcq(bits=16))
    r = analyse_shot(waveforms_from_raw(raw, dt, t0), synth.meta_for(ts), AnalysisConfig(), load_preset(P9))
    assert "DROOP_HIGH" in r.flags


@pytest.mark.req("ANA-03")
def test_filter_is_recorded_and_mild(cfg: AnalysisConfig) -> None:
    c = cfg.model_copy(update={"filter": FilterCfg(kind="moving_average", n_samples=3)})
    r0 = _analyse(synth.SynthAcq(bits=16), cfg)
    r1 = _analyse(synth.SynthAcq(bits=16), c)
    for k in ("e_on", "e_off"):
        assert abs(r1.get(k) / r0.get(k) - 1) < 0.005


@pytest.mark.req("ANA-15")
def test_auto_preset_selection(cfg: AnalysisConfig) -> None:
    ts = true_shot("sic")
    raw, dt, t0 = synth.acquire(ts, synth.SynthAcq(bits=16))
    r = analyse_shot(waveforms_from_raw(raw, dt, t0), synth.meta_for(ts), cfg)  # preset=None → auto
    assert r.preset["name"] == "IEC60747-8" and r.preset["verified"] == "false"
