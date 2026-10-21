"""Configuration models, presets and event log (ARC-05, CFG-01..07, ANA-15, DATA-04)."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from autodpt.config import (
    AnalysisConfig,
    ChannelMap,
    Fixture,
    GateDrive,
    Probe,
    ProbeLibrary,
    Setup,
    Station,
    auto_preset_name,
    available_presets,
    load_model,
    load_preset,
    save_model,
)
from autodpt.data.eventlog import get_logger, setup_logging

EX = Path(__file__).resolve().parents[2] / "config" / "examples"


@pytest.mark.req("ARC-05", "CFG-07", "GUI-06")
def test_examples_load_and_roundtrip(tmp_path: Path) -> None:
    setup = load_model(EX / "setup_fs820_hs_400v.yaml", Setup)
    lib = load_model(EX / "probes.yaml", ProbeLibrary)
    load_model(EX / "station.yaml", Station)
    assert setup.check_probes(lib) == [] or all("expired" in w for w in setup.check_probes(lib))
    for suffix in (".yaml", ".json"):
        f = tmp_path / f"s{suffix}"
        save_model(setup, f)
        assert load_model(f, Setup) == setup


@pytest.mark.req("CFG-02", "ARC-05")
def test_channel_map_rejects_missing_and_duplicates() -> None:
    with pytest.raises(ValidationError, match="required signals missing"):
        ChannelMap(assignments=[{"signal": "VGE_DUT", "scope_channel": 1, "probe": "p"}])  # type: ignore[list-item]
    with pytest.raises(ValidationError, match="scope channel is assigned twice"):
        ChannelMap(assignments=[  # type: ignore[list-item]
            {"signal": "VGE_DUT", "scope_channel": 1, "probe": "p"},
            {"signal": "VCE_DUT", "scope_channel": 1, "probe": "p"},
            {"signal": "IC_DUT", "scope_channel": 2, "probe": "p"}])


@pytest.mark.req("CFG-01")
def test_probe_consistency_and_calibration() -> None:
    with pytest.raises(ValidationError, match="current probe"):
        Probe(name="r", type="rogowski", scale=1.0, scale_unit="V/V", bandwidth_hz=1e6)  # type: ignore[arg-type]
    p = Probe(name="v", type="passive", scale=10, scale_unit="V/V", bandwidth_hz=350e6,  # type: ignore[arg-type]
              cal_due=dt.date(2020, 1, 1))
    assert p.calibration_expired() and p.effective_rise_time_s == pytest.approx(1e-9)
    with pytest.raises(ValidationError, match="duplicate"):
        ProbeLibrary(probes=[p, p])


@pytest.mark.req("CFG-03")
def test_fixture_derived_values_match_appendix_e() -> None:
    f = Fixture(name="x", c_dclink_f=1e-3, r_bleed_ohm=44e3, r_discharge_ohm=470, discharge_relay=True,
                l_load_h=20e-6, max_v=1000, max_i=1500)
    assert f.stored_energy_j(1000) == pytest.approx(500.0)
    assert f.bleed_time_s(1000, 60) == pytest.approx(123.8, abs=0.1)
    assert f.bleed_time_s(1000, 60, relay_closed=True) == pytest.approx(1.31, abs=0.01)
    with pytest.raises(ValidationError, match="requires r_discharge_ohm"):
        Fixture(name="x", c_dclink_f=1e-3, r_bleed_ohm=44e3, discharge_relay=True, l_load_h=1e-6,
                max_v=1, max_i=1)


@pytest.mark.req("CFG-05")
def test_gate_drive_levels() -> None:
    with pytest.raises(ValidationError):
        GateDrive(v_on=-5, v_off=15, rg_on_ohm=1, rg_off_ohm=1)


@pytest.mark.req("ANA-15")
def test_presets_shipped_and_auto_selection() -> None:
    assert {"IEC60747-9", "IEC60747-8", "LEGACY"} <= set(available_presets())
    assert auto_preset_name("IGBT") == "IEC60747-9" and auto_preset_name("MOSFET") == "IEC60747-8"
    p9, p8 = load_preset("IEC60747-9"), load_preset("IEC60747-8")
    assert not p9.verified and not p8.verified and p9.gate_reference == "zero"
    assert p9.params["e_off"].start.signal == "vge"     # IEC: E_off starts at 90 % V_GE
    assert p8.params["t_r"].start.signal == "vce"       # IEC 60747-8: V_DS-based times


@pytest.mark.req("ARC-05")
def test_analysis_config_rejects_bad_pairs() -> None:
    with pytest.raises(ValidationError):
        AnalysisConfig(slope_levels=(0.5, 0.5))


@pytest.mark.req("DATA-04", "NFR-LOG-01")
def test_event_log_iso_ms_and_memory(tmp_path: Path) -> None:
    mem = setup_logging(tmp_path, console_level=logging.CRITICAL)
    get_logger("seq").info("state IDLE -> CONFIGURED")
    for h in logging.getLogger("autodpt").handlers:
        h.flush()
    line = (tmp_path / "autodpt.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    ts, level, source, msg = line.split("\t")
    assert dt.datetime.fromisoformat(ts) and "." in ts and level == "INFO"
    assert source == "autodpt.seq" and msg.startswith("state IDLE")
    assert mem.records[-1][2] == "autodpt.seq"
