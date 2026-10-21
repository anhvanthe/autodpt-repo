"""Shared fixtures: synthetic shots are expensive to generate, so they are cached per session."""

from __future__ import annotations

from functools import cache

import pytest

from autodpt.analysis import synth
from autodpt.config.models import AnalysisConfig

CASES = {
    "igbt": (synth.igbt_device, synth.SynthCircuit()),
    "sic": (synth.sic_device, synth.SynthCircuit(v_dc=600.0, droop=6.0, l_sigma=15e-9, i_set=300.0)),
}
#: noise (rms) used for the "realistic" acquisition: 0.5 % of the V_CE/I_C swing, 0.1 V on gates
NOISE = {
    "igbt": {"VCE_DUT": 2.0, "IC_DUT": 1.0, "VGE_DUT": 0.1, "VCE_COMP": 2.0, "VGE_COMP": 0.1},
    "sic": {"VCE_DUT": 3.0, "IC_DUT": 1.5, "VGE_DUT": 0.1, "VCE_COMP": 3.0, "VGE_COMP": 0.1},
}


@cache
def true_shot(case: str) -> synth.TrueShot:
    dev_f, circ = CASES[case]
    return synth.generate_true(dev_f(), circ, synth.SynthTiming())


@pytest.fixture(scope="session")
def cfg() -> AnalysisConfig:
    return AnalysisConfig()
