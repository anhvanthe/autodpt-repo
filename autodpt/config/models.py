"""Typed configuration models (SRS ARC-05, CFG-01..07, §8.2).

All quantities are SI (V, A, s, Ω, F, H, Hz, J) unless the field name says otherwise.
Every model is validated at load time; invalid configurations are rejected with the
offending field named by pydantic's ValidationError.
"""

from __future__ import annotations

import datetime as _dt
import math
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------------------
# Common
# --------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


SignalName = Literal[
    "VGE_DUT", "VCE_DUT", "IC_DUT", "VGE_COMP", "VCE_COMP", "IC_COMP", "IL", "VDC",
    "AUX1", "AUX2", "AUX3",
]
REQUIRED_SIGNALS: tuple[str, ...] = ("VGE_DUT", "VCE_DUT", "IC_DUT")
CURRENT_SIGNALS: tuple[str, ...] = ("IC_DUT", "IC_COMP", "IL")


# --------------------------------------------------------------------------------------
# CFG-01 Probe library
# --------------------------------------------------------------------------------------


class ProbeType(StrEnum):
    PASSIVE = "passive"
    HV_DIFFERENTIAL = "hv_differential"
    OPTICALLY_ISOLATED = "optically_isolated"
    ROGOWSKI = "rogowski"
    COAXIAL_SHUNT = "coaxial_shunt"
    CURRENT_TRANSFORMER = "current_transformer"
    HALL = "hall"


class Probe(_Model):
    """CFG-01. `scale` meaning depends on `scale_unit`:
    V/V = attenuation (10 → 10:1), V/A = sensitivity of a current probe, Ohm = shunt resistance."""

    name: str
    type: ProbeType
    scale: float = Field(gt=0)
    scale_unit: Literal["V/V", "V/A", "Ohm"]
    bandwidth_hz: float = Field(gt=0)
    rise_time_s: float | None = Field(default=None, gt=0)
    propagation_delay_s: float = 0.0
    max_voltage_v: float | None = Field(default=None, gt=0)
    max_current_a: float | None = Field(default=None, gt=0)
    max_didt_a_per_s: float | None = Field(default=None, gt=0)
    rogowski_tau_s: float | None = Field(default=None, gt=0, description="LF time constant for droop comp.")
    shunt_pulse_energy_j: float | None = Field(default=None, gt=0)
    serial: str = ""
    cal_due: _dt.date | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Probe:
        current_types = {ProbeType.ROGOWSKI, ProbeType.COAXIAL_SHUNT, ProbeType.CURRENT_TRANSFORMER,
                         ProbeType.HALL}
        if self.type in current_types and self.scale_unit == "V/V":
            raise ValueError(f"probe '{self.name}': current probe needs scale_unit V/A or Ohm")
        if self.type not in current_types and self.scale_unit != "V/V":
            raise ValueError(f"probe '{self.name}': voltage probe needs scale_unit V/V")
        if self.type == ProbeType.COAXIAL_SHUNT and self.scale_unit != "Ohm":
            raise ValueError(f"probe '{self.name}': coaxial shunt needs scale_unit Ohm")
        return self

    @property
    def effective_rise_time_s(self) -> float:
        """10–90 % rise time; 0.35/BW if not given (single-pole estimate)."""
        return self.rise_time_s if self.rise_time_s is not None else 0.35 / self.bandwidth_hz

    @property
    def is_current_probe(self) -> bool:
        return self.scale_unit in ("V/A", "Ohm")

    def calibration_expired(self, today: _dt.date | None = None) -> bool:
        if self.cal_due is None:
            return False
        return (today or _dt.date.today()) > self.cal_due


class ProbeLibrary(_Model):
    probes: list[Probe]

    @field_validator("probes")
    @classmethod
    def _unique(cls, v: list[Probe]) -> list[Probe]:
        names = [p.name for p in v]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate probe names: {sorted(dup)}")
        return v

    def get(self, name: str) -> Probe:
        for p in self.probes:
            if p.name == name:
                return p
        raise KeyError(f"probe '{name}' not in library")


# --------------------------------------------------------------------------------------
# CFG-02 Channel map
# --------------------------------------------------------------------------------------


class ChannelAssignment(_Model):
    signal: SignalName
    scope_channel: int = Field(ge=1, le=8)
    probe: str
    invert: bool = False
    offset_v: float = 0.0
    bw_limit_hz: float | None = Field(default=None, gt=0)
    deskew_s: float = Field(default=0.0, description="single de-skew value; + = channel lags (ANA-21)")
    dclink_monitor: bool = False


class ChannelMap(_Model):
    assignments: list[ChannelAssignment]

    @model_validator(mode="after")
    def _check(self) -> ChannelMap:
        sig = [a.signal for a in self.assignments]
        ch = [a.scope_channel for a in self.assignments]
        if len(set(sig)) != len(sig):
            raise ValueError("a signal is assigned twice")
        if len(set(ch)) != len(ch):
            raise ValueError("a scope channel is assigned twice")
        missing = [s for s in REQUIRED_SIGNALS if s not in sig]
        if missing:
            raise ValueError(f"required signals missing: {missing}")
        if sum(a.dclink_monitor for a in self.assignments) > 1:
            raise ValueError("only one channel may be marked as DC-link monitor")
        return self

    def by_signal(self) -> dict[str, ChannelAssignment]:
        return {a.signal: a for a in self.assignments}


# --------------------------------------------------------------------------------------
# CFG-03 Fixture (incl. Appendix E/F parameters)
# --------------------------------------------------------------------------------------


class DcLinkMonitorCfg(_Model):
    type: Literal["dmm", "scope_channel", "psu_readback", "mock"] = "dmm"
    gain_correction: float = Field(default=1.0, gt=0)
    accuracy_v: float = Field(default=2.0, ge=0)


class Fixture(_Model):
    name: str
    c_dclink_f: float = Field(gt=0)
    c_tolerance: float = Field(default=0.1, ge=0, lt=1)
    r_bleed_ohm: float = Field(gt=0, description="total bleeder resistance (all strings in parallel)")
    bleed_strings: int = Field(default=2, ge=1)
    r_discharge_ohm: float | None = Field(default=None, gt=0)
    discharge_relay: bool = False
    monitor: DcLinkMonitorCfg = DcLinkMonitorCfg()
    r_series_ohm: float = Field(default=0.0, ge=0, description="PSU → DC-link series resistance")
    v_series_diode_v: float = Field(default=0.0, ge=0)
    l_load_h: float = Field(gt=0)
    l_meas_freq_hz: float | None = Field(default=None, gt=0)
    r_load_ohm: float = Field(default=0.0, ge=0)
    max_v: float = Field(gt=0)
    max_i: float = Field(gt=0)

    @model_validator(mode="after")
    def _relay(self) -> Fixture:
        if self.discharge_relay and self.r_discharge_ohm is None:
            raise ValueError("discharge_relay=true requires r_discharge_ohm")
        return self

    # Derived values shown live in the GUI (CFG-03)
    def stored_energy_j(self, v: float) -> float:
        return 0.5 * self.c_dclink_f * v * v

    def tau_bleed_s(self, relay_closed: bool = False) -> float:
        r = self.r_bleed_ohm
        if relay_closed and self.r_discharge_ohm is not None:
            r = r * self.r_discharge_ohm / (r + self.r_discharge_ohm)
        return r * self.c_dclink_f

    def bleed_time_s(self, v_start: float, v_safe: float, relay_closed: bool = False) -> float:
        if v_start <= v_safe:
            return 0.0
        return self.tau_bleed_s(relay_closed) * math.log(v_start / v_safe)

    def inductor_energy_j(self, i: float) -> float:
        return 0.5 * self.l_load_h * i * i


# --------------------------------------------------------------------------------------
# CFG-04/05 DUT and gate drive
# --------------------------------------------------------------------------------------


class Dut(_Model):
    part_number: str
    dut_type: Literal["IGBT", "MOSFET"]
    technology: Literal["Si", "SiC", "GaN"] = "Si"
    v_rated_v: float = Field(gt=0)
    i_nom_a: float = Field(gt=0)
    tvj_max_c: float = 175.0
    sample_id: str = ""
    config_label: str = Field(default="", description="e.g. 6IN1 (used in file names, DATA-05)")
    phase: Literal["U", "V", "W", "-"] = "-"
    side: Literal["HS", "LS"] = "LS"
    comp_vth_min_v: float | None = Field(default=None, description="ANA-27 parasitic turn-on warning")
    comp_vge_min_rating_v: float | None = Field(default=None, description="ANA-27 undershoot warning")
    shot_count: int = Field(default=0, ge=0)
    notes: str = ""


class GateDrive(_Model):
    v_on: float
    v_off: float
    rg_on_ohm: float = Field(gt=0)
    rg_off_ohm: float = Field(gt=0)
    driver_type: str = ""
    prop_delay_on_s: float | None = None
    prop_delay_off_s: float | None = None

    @model_validator(mode="after")
    def _levels(self) -> GateDrive:
        if self.v_on <= self.v_off:
            raise ValueError("v_on must be greater than v_off")
        return self


# --------------------------------------------------------------------------------------
# CFG-06 Limits
# --------------------------------------------------------------------------------------


class Limits(_Model):
    max_vdc: float = Field(gt=0, le=1000.0)
    vdc_derating: float = Field(default=0.8, gt=0, le=1.0, description="× DUT V rating")
    max_ipeak: float = Field(gt=0)
    max_pulse_width_s: float = Field(default=500e-6, gt=0)
    max_total_on_s: float = Field(default=600e-6, gt=0)
    logic_level_v: float = Field(default=5.0, gt=0)
    max_logic_level_v: float = Field(default=5.5, gt=0)
    min_t2_t3_s: float = Field(default=1e-6, gt=0)
    overshoot_stop_k: float = Field(default=0.85, gt=0, le=1.0)
    v_safe: float = Field(default=50.0, gt=0, le=60.0)

    @model_validator(mode="after")
    def _logic(self) -> Limits:
        if self.logic_level_v > self.max_logic_level_v:
            raise ValueError("logic_level_v exceeds max_logic_level_v")
        return self


# --------------------------------------------------------------------------------------
# SEQ-01 Test matrix
# --------------------------------------------------------------------------------------


class GatePair(_Model):
    rg_on_ohm: float = Field(gt=0)
    rg_off_ohm: float = Field(gt=0)


class GateVoltagePair(_Model):
    v_on: float
    v_off: float


class TestMatrix(_Model):
    vdc_list: list[float] = Field(min_length=1)
    itest_list: list[float] = Field(min_length=1)
    rg_list: list[GatePair] = Field(min_length=1)
    vg_list: list[GateVoltagePair] = Field(min_length=1)
    temp_list_c: list[float] = Field(default_factory=lambda: [25.0], min_length=1)
    repetitions: int = Field(default=3, ge=1)
    t_pre_s: float = Field(default=5e-6, gt=0)
    t2_s: float = Field(default=3e-6, gt=0)
    t3_s: float = Field(default=3e-6, gt=0)

    @field_validator("vdc_list", "itest_list")
    @classmethod
    def _positive(cls, v: list[float]) -> list[float]:
        if any(x <= 0 for x in v):
            raise ValueError("values must be > 0")
        return v

    def n_points(self) -> int:
        return (len(self.vdc_list) * len(self.itest_list) * len(self.rg_list) * len(self.vg_list)
                * len(self.temp_list_c))


# --------------------------------------------------------------------------------------
# Analysis configuration (ANA-03, §7)
# --------------------------------------------------------------------------------------


class FilterCfg(_Model):
    kind: Literal["none", "moving_average", "lowpass"] = "none"
    n_samples: int = Field(default=5, ge=1)
    cutoff_hz: float = Field(default=200e6, gt=0)
    order: int = Field(default=2, ge=1, le=8)


class DeskewCfg(_Model):
    tau_max_s: float = Field(default=20e-9, gt=0)
    step_s: float = Field(default=0.1e-9, gt=0)
    sg_window_s: float = Field(default=5e-9, gt=0)
    min_snr: float = Field(default=10.0, gt=0)
    min_contrast: float = Field(default=1.2, gt=1.0)
    contrast_offset_s: float = Field(default=2e-9, gt=0)
    max_fit_error: float = Field(default=0.06, gt=0, description="model error rms / dip amplitude")
    pool_over_currents: bool = False


class AnalysisConfig(_Model):
    preset: str | None = Field(default=None, description="None = auto from DUT type (ANA-15)")
    filter: FilterCfg = FilterCfg()
    deskew: DeskewCfg = DeskewCfg()
    debounce_samples: int = Field(default=3, ge=1)
    hysteresis_sigma: float = Field(default=4.0, ge=0, description="crossing hysteresis × channel noise rms")
    window_pre_s: float = Field(default=0.3e-6, ge=0)
    window_post_max_s: float = Field(default=3e-6, gt=0)
    gate_search_s: float = Field(default=1.5e-6, gt=0)
    baseline_window_frac: tuple[float, float] = (0.1, 0.8)
    ioff_fit_window_frac: tuple[float, float] = (0.6, 1.0)
    ioff_fit_guard_s: float = Field(default=0.2e-6, ge=0)
    v_f_est: float = Field(default=1.5, ge=0, description="freewheel diode drop for I_on estimate (ANA-11)")
    slope_levels: tuple[float, float] = (0.1, 0.9)
    rr_levels: tuple[float, float] = (0.9, 0.25)
    qrr_end: Literal["trr", "2pct"] = "trr"
    lsigma_levels: tuple[float, float] = (0.2, 0.8)
    current_tolerance: float = Field(default=0.02, gt=0)
    vdc_tolerance: float = Field(default=0.05, gt=0)
    droop_warn_frac: float = Field(default=0.02, gt=0)
    bw_factor: float = Field(default=3.0, gt=0)
    min_edge_samples: int = Field(default=10, ge=1)
    ringing_settle_frac: float = Field(default=0.05, gt=0)
    scope_rise_time_s: float = Field(default=0.35 / 1e9, gt=0, description="WaveRunner 8108HD: 1 GHz")

    @field_validator("baseline_window_frac", "ioff_fit_window_frac", "slope_levels", "rr_levels",
                     "lsigma_levels")
    @classmethod
    def _ordered_pair(cls, v: tuple[float, float]) -> tuple[float, float]:
        a, b = v
        if not (0 <= min(a, b) and max(a, b) <= 1 and a != b):
            raise ValueError("pair must be two distinct fractions in [0, 1]")
        return v


# --------------------------------------------------------------------------------------
# Station (instruments) — used from Phase 2
# --------------------------------------------------------------------------------------


class InstrumentCfg(_Model):
    model: str
    resource: str = Field(description="VISA resource string, e.g. TCPIP0::192.168.0.10::inst0::INSTR")
    transport: Literal["vxi11", "vicp", "socket", "usb", "mock"] = "vxi11"
    timeout_ms: int = Field(default=5000, gt=0)
    read_termination: str | None = "\n"
    write_termination: str | None = "\n"
    command_set: str | None = Field(default=None, description="command-set name; None = by role")
    test_channel: str = Field(default="1", description="scope trace (C1…) / AFG channel used by bench-check")


class Station(_Model):
    name: str
    visa_library: str = Field(default="@py", description="'@py' = pyvisa-py; '' = NI-VISA (needed for VICP)")
    afg_load: Literal["INF", "50"] = Field(default="INF", description="gate-driver logic input: high-Z or 50 Ω")
    instruments: dict[Literal["scope", "afg", "psu", "dcmon", "dio", "temp"], InstrumentCfg]

    @model_validator(mode="after")
    def _required(self) -> Station:
        missing = [k for k in ("scope", "afg", "psu") if k not in self.instruments]
        if missing:
            raise ValueError(f"station is missing instruments: {missing}")
        return self


# --------------------------------------------------------------------------------------
# Setup = everything needed for one run (GUI-06)
# --------------------------------------------------------------------------------------


class Setup(_Model):
    name: str
    operator: str = ""
    matrix: TestMatrix
    channel_map: ChannelMap
    analysis: AnalysisConfig = AnalysisConfig()
    fixture: Fixture
    dut: Dut
    gate_drive: GateDrive
    limits: Limits

    def check_probes(self, library: ProbeLibrary) -> list[str]:
        """Return warnings; raise KeyError if a referenced probe does not exist."""
        warnings: list[str] = []
        for a in self.channel_map.assignments:
            p = library.get(a.probe)
            if p.calibration_expired():
                warnings.append(f"probe '{p.name}' calibration expired ({p.cal_due})")
            is_current = a.signal in CURRENT_SIGNALS
            if is_current != p.is_current_probe:
                raise ValueError(f"signal {a.signal} assigned to unsuitable probe '{p.name}'")
        return warnings
