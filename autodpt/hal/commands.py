"""Instrument command sets (SRS ARC-06, NET-03, Appendix C).

Command strings live in YAML (``autodpt/hal/commandsets``), one file per instrument family, each command
with a ``verified`` flag: *true* = confirmed in the vendor manual, *false* = convention to be confirmed
on the instrument (bench-check reports whether the instrument accepted it).
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROLE_DEFAULT = {"psu": "magna_sl", "afg": "tek_afg31000", "scope": "lecroy_maui", "dcmon": "scpi_dmm"}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cmd: str
    verified: bool = False
    note: str = ""


class IdnMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    vendor: str | list[str] = ""
    model: str | list[str] = ""

    @staticmethod
    def _any(pat: str | list[str], text: str) -> bool:
        pats = [pat] if isinstance(pat, str) else pat
        return any(p.upper() in text.upper() for p in pats) if pats and pats != [""] else True

    def matches(self, vendor: str, model: str) -> bool:
        return self._any(self.vendor, vendor) and self._any(self.model, model)


class CommandSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    role: Literal["psu", "afg", "scope", "dcmon"]
    idn_match: IdnMatch
    min_firmware: str | None = None
    default_socket_port: int | None = None
    error_style: Literal["scpi", "lecroy"]
    commands: dict[str, Command]
    ques_bits: dict[int, str] = Field(default_factory=dict)
    forbidden: list[str] = Field(default_factory=list)
    arb_max_code: int | None = None

    def fmt(self, key: str, **kw: object) -> str:
        return self.commands[key].cmd.format(**kw)

    def is_forbidden(self, command: str) -> bool:
        c = re.sub(r"\s+", " ", command.strip().upper())
        return any(c == f.upper() or c.startswith(f.upper() + " ") for f in self.forbidden)

    def unverified(self) -> list[str]:
        return [k for k, c in self.commands.items() if not c.verified]


def load_command_set(name: str) -> CommandSet:
    p = Path(name)
    if not p.suffix:
        p = Path(str(resources.files("autodpt.hal") / "commandsets" / f"{name}.yaml"))
    with p.open(encoding="utf-8") as fh:
        return CommandSet.model_validate(yaml.safe_load(fh))


def parse_idn(text: str) -> dict[str, str]:
    parts = [x.strip() for x in text.strip().split(",")]
    parts += [""] * (4 - len(parts))
    return {"vendor": parts[0], "model": parts[1], "serial": parts[2], "firmware": ",".join(parts[3:]).strip(",")}


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_number(text: str) -> float:
    """First number in a response ('5.00E-06 S', '"VERTICAL_GAIN : 1.2e-4"', '+1.0E+01')."""
    m = _NUM.search(text)
    if not m:
        raise ValueError(f"no number in response {text!r}")
    return float(m.group(0))
