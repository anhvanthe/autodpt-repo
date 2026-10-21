"""Data types shared by the analysis package (SRS §7). All values SI."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
DeskewMethod = Literal["none", "single", "statistic"]
METHODS: tuple[DeskewMethod, ...] = ("none", "single", "statistic")


class AnalysisError(Exception):
    """Raised when a shot cannot be analysed at all (e.g. EDGE_MISSING)."""

    def __init__(self, flag: str, message: str) -> None:
        super().__init__(f"{flag}: {message}")
        self.flag = flag


@dataclass(frozen=True)
class RawChannel:
    """One scope channel as transferred (SCO-05): integer codes + scaling.

    physical value = codes * gain + offset, then negated if `invert` (channel polarity, CFG-02).
    `code_min`/`code_max` are the ADC limits used for clipping detection (SCO-02)."""

    signal: str
    codes: npt.NDArray[np.int16]
    gain: float
    offset: float
    invert: bool = False
    code_min: int = -32768
    code_max: int = 32767


@dataclass
class Waveforms:
    """Scaled, polarity-corrected signals on a common time base. t = t0 + i*dt (t = 0 at trigger)."""

    dt: float
    t0: float
    signals: dict[str, FloatArray]
    clipped: dict[str, bool] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(next(iter(self.signals.values())))

    def time(self) -> FloatArray:
        return self.t0 + self.dt * np.arange(self.n, dtype=np.float64)

    def index(self, t: float) -> int:
        """Nearest sample index for time t, clipped to the record."""
        i = round((t - self.t0) / self.dt)
        return min(max(i, 0), self.n - 1)

    def has(self, signal: str) -> bool:
        return signal in self.signals


@dataclass(frozen=True)
class ShotMeta:
    """Per-shot context needed by the analysis (from setup, pulse plan and acquisition)."""

    t_pre: float
    t1: float
    t2: float
    t3: float
    l_load: float
    i_set: float
    v_set: float
    dut_type: Literal["IGBT", "MOSFET"]
    side: Literal["HS", "LS"] = "LS"
    single_skew: dict[str, float] = field(default_factory=dict)
    probe_rise_time: dict[str, float] = field(default_factory=dict)
    rogowski_tau: dict[str, float] = field(default_factory=dict)
    comp_vth_min: float | None = None
    comp_vge_min_rating: float | None = None
    v_rated: float | None = None

    @property
    def edges_commanded(self) -> tuple[float, float, float, float]:
        e1 = self.t_pre
        e2 = e1 + self.t1
        e3 = e2 + self.t2
        e4 = e3 + self.t3
        return e1, e2, e3, e4


@dataclass
class SkewEstimate:
    """ANA-22 per-shot statistic de-skew result for IC_DUT relative to VCE_DUT."""

    tau: float
    l_sigma: float
    snr: float
    contrast: float
    ok: bool
    reason: str = ""
    fit_error: float = math.nan


#: flag → severity (ANA-30). "info" flags do not change validity.
FLAG_SEVERITY: dict[str, str] = {
    "CLIPPED": "invalid",
    "EDGE_MISSING": "invalid",
    "CURRENT_OUT_OF_TOL": "invalid",
    "VDC_OUT_OF_TOL": "invalid",
    "NEGATIVE_ENERGY": "invalid",
    "THRESHOLD_NOT_REACHED": "invalid",
    "TRANSFER_INCOMPLETE": "invalid",
    "BW_LIMITED_EDGE": "warning",
    "LOW_SAMPLES_ON_EDGE": "warning",
    "VD_DERIVED": "warning",
    "DESKEW_ILL_CONDITIONED": "warning",
    "DROOP_HIGH": "warning",
    "RINGING_NOT_SETTLED": "warning",
    "COMP_GATE_NEAR_VTH": "warning",
    "COMP_GATE_UNDERSHOOT": "warning",
    "NO_RECOVERY": "warning",
    "I_ON_FROM_I_OFF": "info",
    "TV90_NOT_FOUND": "warning",
}


@dataclass
class ShotResult:
    """Result of one shot. `values[method]` holds every parameter for that de-skew method
    (keys as in template_map.yaml); method-independent values are repeated in each method."""

    values: dict[str, dict[str, float]]
    skews: dict[str, float | None]
    flags: list[str]
    preset: dict[str, str]
    skew_estimate: SkewEstimate | None = None

    @property
    def validity(self) -> str:
        sev = {FLAG_SEVERITY.get(f.split(":")[0], "warning") for f in self.flags}
        if "invalid" in sev:
            return "INVALID"
        if "warning" in sev:
            return "WARNING"
        return "VALID"

    def get(self, key: str, method: DeskewMethod = "none") -> float:
        v = self.values.get(method, {})
        return v.get(key, math.nan)

    def flat(self) -> dict[str, float | str]:
        """'key' for method none, 'key@single', 'key@statistic' for the others."""
        out: dict[str, float | str] = {}
        for m, vals in self.values.items():
            for k, v in vals.items():
                out[k if m == "none" else f"{k}@{m}"] = v
        out["validity"] = self.validity
        out["flags"] = ";".join(self.flags)
        return out
