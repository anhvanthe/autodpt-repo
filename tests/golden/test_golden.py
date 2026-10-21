"""VER-SW-01: analysis vs synthetic golden waveforms with known ground truth (ANA-41, ANA-42).

Two acquisition classes:
* IDEAL      – no noise, 16-bit quantisation, 10 GS/s: isolates algorithm error. ANA-42 tolerances.
* REALISTIC  – 0.5 % noise on V_CE/I_C, 0.1 V on gates, 12-bit, 10 GS/s, 6 seeds: documents the
  accuracy to expect on the bench (see docs/analysis_accuracy.md). The worst seed must pass.
"""

from __future__ import annotations

import math

import pytest

from autodpt.analysis import synth
from autodpt.analysis.analyse import analyse_shot
from autodpt.analysis.preprocess import waveforms_from_raw
from autodpt.config.models import AnalysisConfig
from autodpt.config.presets import load_preset
from tests.conftest import NOISE, true_shot

SAMPLE = 0.1e-9  # 10 GS/s

# ----- tolerance tables: key -> ("rel", fraction) | ("abs", value in SI) -----------------------
CROSSING_TIMES = ("td_on", "t_r", "td_off", "t_f", "t_z")
RR_TIMES = ("t_rr", "t_a", "t_b")

IDEAL: dict[str, tuple[str, object]] = {
    **{k: ("abs", SAMPLE) for k in CROSSING_TIMES},
    **{k: ("abs|rel", (2 * SAMPLE, 0.005)) for k in RR_TIMES},
    "e_on": ("rel", 0.005), "e_off": ("rel", 0.005), "e_rec": ("rel", 0.005),
    "i_rrm": ("rel", 0.005), "q_rr": ("rel", 0.01), "softness": ("rel", 0.02),
    "didt_on": ("rel", 0.005), "dvdt_on": ("rel", 0.005), "dvdt_off": ("rel", 0.005),
    "didt_off": ("rel", 0.005),
    "vdc_on": ("rel", 0.001), "vdc_off": ("rel", 0.001), "i_off": ("rel", 0.001), "i_on": ("rel", 0.001),
    "vce_peak_off": ("rel", 0.002), "overshoot_dv": ("rel", 0.01), "ic_peak_on": ("rel", 0.002),
    "l_sigma_on": ("rel", 0.02),
    "comp_vce_max_on": ("rel", 0.005), "comp_vge_max_on": ("abs", 0.05), "comp_vge_min_on": ("abs", 0.05),
    "comp_vge_max_off": ("abs", 0.05), "comp_vge_min_off": ("abs", 0.05), "comp_i_max_off": ("abs", 0.5),
}

_REAL_COMMON: dict[str, tuple[str, object]] = {
    "td_on": ("abs", 2e-9), "t_r": ("abs", 2e-9), "td_off": ("abs", 2.5e-9),
    "e_on": ("rel", 0.02), "e_off": ("rel", 0.02),
    "didt_on": ("rel", 0.03), "dvdt_on": ("rel", 0.03), "dvdt_off": ("rel", 0.03),
    "vdc_on": ("rel", 0.002), "vdc_off": ("rel", 0.002), "i_off": ("rel", 0.002), "i_on": ("rel", 0.002),
    "vce_peak_off": ("rel", 0.03), "overshoot_dv": ("rel", 0.25), "ic_peak_on": ("rel", 0.02),
    "l_sigma_on": ("rel", 0.02), "t_rr": ("abs", 4e-9), "t_a": ("abs", 1.5e-9), "t_b": ("abs", 4e-9),
    "comp_vge_max_on": ("abs", 0.6), "comp_vge_min_on": ("abs", 0.6), "comp_vge_max_off": ("abs", 0.6),
    "comp_vge_min_off": ("abs", 0.6), "comp_vce_max_on": ("abs", 12.0),
}
REALISTIC: dict[str, dict[str, tuple[str, object]]] = {
    # IGBT: the 10 %/2 % current levels sit on the slow tail current -> noise moves those crossings
    "igbt": {**_REAL_COMMON, "t_f": ("rel", 0.12), "t_z": ("rel", 0.25), "didt_off": ("rel", 0.15),
             "e_rec": ("rel", 0.03), "i_rrm": ("rel", 0.02), "q_rr": ("rel", 0.02),
             "softness": ("rel", 0.08)},
    # SiC: I_rrm is only 10 % of the current range -> max-of-noise bias on the recovery peak
    "sic": {**_REAL_COMMON, "t_f": ("abs", 1e-9), "didt_off": ("rel", 0.03),
            "e_rec": ("rel", 0.12), "i_rrm": ("rel", 0.12), "q_rr": ("rel", 0.06), "softness": ("rel", 0.3)},
}

PRESETS = [("igbt", "IEC60747-9"), ("igbt", "LEGACY"), ("sic", "IEC60747-8"), ("sic", "LEGACY")]


def _check(meas: dict[str, float], truth: dict[str, float], tol: dict[str, tuple[str, object]],
           label: str) -> list[str]:
    bad: list[str] = []
    for key, (kind, lim) in tol.items():
        if key not in truth:
            continue  # parameter not defined by this preset (e.g. t_z only in IEC60747-9)
        a, b = meas.get(key, math.nan), truth[key]
        if kind == "abs|rel":  # pass if either the absolute or the relative limit holds
            ok = abs(a - b) <= lim[0] or abs(a - b) <= lim[1] * abs(b)  # type: ignore[index]
        else:
            err = abs(a - b) if kind == "abs" else abs(a - b) / abs(b)
            ok = err <= lim  # NaN fails
        if not ok:
            bad.append(f"{label} {key}: meas={a:.6g} truth={b:.6g} limit={lim} ({kind})")
    return bad


Run = tuple[dict[str, float], dict[str, float], list[str], str]


def _run(case: str, preset: str, cfg: AnalysisConfig, acq: synth.SynthAcq,
         meta_kw: dict[str, object] | None = None) -> Run:
    ts = true_shot(case)
    p = load_preset(preset)
    truth = synth.truth(ts, p, cfg)
    raw, dt, t0 = synth.acquire(ts, acq)
    res = analyse_shot(waveforms_from_raw(raw, dt, t0), synth.meta_for(ts, **(meta_kw or {})), cfg, p)
    return res.values.get("none", {}), truth, res.flags, res.validity


@pytest.mark.req("ANA-41", "ANA-42", "ANA-15", "ANA-16", "ANA-17", "ANA-18", "ANA-19", "ANA-27")
@pytest.mark.parametrize(("case", "preset"), PRESETS)
def test_ideal_acquisition(case: str, preset: str, cfg: AnalysisConfig) -> None:
    meas, truth, flags, validity = _run(case, preset, cfg, synth.SynthAcq(bits=16))
    assert validity == "VALID", flags
    bad = _check(meas, truth, IDEAL, f"{case}/{preset}")
    assert not bad, "\n".join(bad)


@pytest.mark.slow
@pytest.mark.req("ANA-42", "ANA-14")
@pytest.mark.parametrize(("case", "preset"), PRESETS)
def test_realistic_acquisition(case: str, preset: str, cfg: AnalysisConfig) -> None:
    bad: list[str] = []
    for seed in range(6):
        acq = synth.SynthAcq(bits=12, noise_rms=NOISE[case], seed=seed)
        meas, truth, flags, validity = _run(case, preset, cfg, acq)
        assert validity in ("VALID", "WARNING"), flags
        bad += _check(meas, truth, REALISTIC[case], f"{case}/{preset}/seed{seed}")
    assert not bad, "\n".join(bad)


@pytest.mark.req("ANA-07", "CFG-02")
@pytest.mark.parametrize("case", ["igbt", "sic"])
def test_high_side_polarity_inversion(case: str, cfg: AnalysisConfig) -> None:
    """HS measurement with inverted current and voltage channels gives the same results (ANA-07)."""
    preset = "IEC60747-9" if case == "igbt" else "IEC60747-8"
    inv = {"IC_DUT": True, "VCE_DUT": True, "VCE_COMP": True}
    meas, truth, flags, validity = _run(case, preset, cfg, synth.SynthAcq(bits=16, invert=inv),
                                        {"side": "HS"})
    assert validity == "VALID", flags
    bad = _check(meas, truth, IDEAL, f"{case}/HS")
    assert not bad, "\n".join(bad)


@pytest.mark.req("ANA-12", "ANA-30")
def test_derived_diode_voltage_without_comp_channel(cfg: AnalysisConfig) -> None:
    """Without VCE_COMP the diode voltage is derived; E_rec stays usable and VD_DERIVED is flagged."""
    chans = ("VGE_DUT", "VCE_DUT", "IC_DUT")
    meas, truth, flags, validity = _run("igbt", "IEC60747-9", cfg, synth.SynthAcq(bits=16, channels=chans))
    assert "VD_DERIVED" in flags and validity == "WARNING"
    assert abs(meas["e_rec"] / truth["e_rec"] - 1) < 0.05
    assert abs(meas["q_rr"] / truth["q_rr"] - 1) < 0.01
    assert math.isnan(meas["comp_vge_max_on"])


@pytest.mark.req("ANA-11")
def test_inductor_current_channel_used_for_i_on(cfg: AnalysisConfig) -> None:
    chans = ("VGE_DUT", "VCE_DUT", "IC_DUT", "VCE_COMP", "IL")
    meas, truth, flags, _ = _run("sic", "IEC60747-8", cfg, synth.SynthAcq(bits=16, channels=chans))
    assert "I_ON_FROM_I_OFF" not in flags
    assert abs(meas["i_on"] / truth["i_on"] - 1) < 1e-3
