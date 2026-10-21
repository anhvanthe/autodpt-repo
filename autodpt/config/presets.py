"""Threshold presets for switching-parameter definitions (SRS ANA-15).

A preset defines every timing/energy parameter as *start point → end point*, where a point is
(signal, level as fraction of its reference, edge direction, search direction). Presets are data
(YAML in ``autodpt/config/presets``) so that definitions can be checked against the standards and
changed without code changes. The preset name/version/verified flag travel with every result.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SignalKey = Literal["vge", "ic", "vce"]
Direction = Literal["rising", "falling"]
EventKey = Literal["on", "off"]

#: parameters every preset must define (template columns + tail time)
REQUIRED_PARAMS: tuple[str, ...] = ("td_on", "t_r", "e_on", "td_off", "t_f", "e_off")


class PresetPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    signal: SignalKey
    level: float = Field(gt=0, lt=1)
    direction: Direction
    search: Literal["left", "right"] = "left"


class PresetParam(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event: EventKey
    kind: Literal["time", "energy"]
    start: PresetPoint
    end: PresetPoint


class Preset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    version: str
    standard: str
    device_type: Literal["IGBT", "MOSFET", "any"]
    verified: bool = False
    gate_reference: Literal["zero", "swing"] = Field(
        description="zero: level·V_GE,on (IEC); swing: V_GE,off + level·(V_GE,on − V_GE,off)")
    source_note: str = ""
    params: dict[str, PresetParam]

    @model_validator(mode="after")
    def _complete(self) -> Preset:
        missing = [p for p in REQUIRED_PARAMS if p not in self.params]
        if missing:
            raise ValueError(f"preset {self.name}: missing parameters {missing}")
        for key, p in self.params.items():
            if key.endswith("_on") or key == "t_r":
                if p.event != "on":
                    raise ValueError(f"preset {self.name}: {key} must be a turn-on parameter")
            if key in ("td_off", "t_f", "e_off", "t_z") and p.event != "off":
                raise ValueError(f"preset {self.name}: {key} must be a turn-off parameter")
        return self


_AUTO = {"IGBT": "IEC60747-9", "MOSFET": "IEC60747-8"}


def preset_dir() -> Path:
    return Path(str(resources.files("autodpt.config") / "presets"))


def load_preset(name_or_path: str) -> Preset:
    """Load a shipped preset by name (e.g. 'IEC60747-9') or a preset YAML file by path."""
    p = Path(name_or_path)
    if not p.suffix:
        p = preset_dir() / f"{name_or_path}.yaml"
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Preset.model_validate(data)


def available_presets() -> list[str]:
    return sorted(f.stem for f in preset_dir().glob("*.yaml"))


def auto_preset_name(dut_type: str) -> str:
    """ANA-15: IGBT → IEC 60747-9, MOSFET (Si/SiC) → IEC 60747-8."""
    try:
        return _AUTO[dut_type]
    except KeyError as exc:
        raise ValueError(f"no automatic preset for DUT type {dut_type!r}") from exc


def resolve_preset(preset: str | None, dut_type: str) -> Preset:
    return load_preset(preset if preset else auto_preset_name(dut_type))
