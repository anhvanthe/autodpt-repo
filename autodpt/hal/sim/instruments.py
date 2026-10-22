"""Instrument simulators for simulation mode and tests (SRS NET-06, SIM-01/02/03).

They accept exactly the command strings of the command sets (case-insensitive), keep state, return
SCPI-style errors for unknown commands and support fault injection. They are deliberately strict so
that a typo in a command set fails in the tests, not first on the bench.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from autodpt.hal.lecroy import build_wavedesc, make_ieee_block, parse_ieee_block


@dataclass
class Faults:
    unknown_commands: set[str] = field(default_factory=set)   # commands answered with an error
    no_answer: set[str] = field(default_factory=set)          # commands that time out
    idn: str | None = None                                    # override *IDN? (wrong model test)
    readback_offset: float = 0.0                              # setpoint read-back error


class _ScpiSim:
    idn = "SIM,SIM,0,0"

    def __init__(self, faults: Faults | None = None) -> None:
        self.faults = faults or Faults()
        self.errors: list[str] = []
        self.received: list[str] = []

    def _err(self, code: int, msg: str) -> None:
        self.errors.append(f'{code},"{msg}"')

    def _pop_error(self) -> str:
        return self.errors.pop(0) if self.errors else '0,"No error"'

    def handle(self, command: str) -> str | bytes | None:
        self.received.append(command)
        c = command.strip()
        key = c.upper()
        if key in {u.upper() for u in self.faults.no_answer}:
            return None
        if key in {u.upper() for u in self.faults.unknown_commands}:
            self._err(-113, "Undefined header")
            return "" if key.endswith("?") else None
        if key == "*IDN?":
            return self.faults.idn or self.idn
        if key in ("SYST:ERR?", "SYSTEM:ERROR?"):
            return self._pop_error()
        ans = self.dispatch(c, key)
        if ans is NotImplemented:
            self._err(-113, "Undefined header")
            return "" if key.endswith("?") else None
        return ans  # type: ignore[return-value]

    def dispatch(self, c: str, key: str) -> object:
        return NotImplemented


class MagnaSim(_ScpiSim):
    idn = "Magna-Power Electronics Inc.,SL1000-1.5/UI+LXI,SIM0001,FW 3.1"

    def __init__(self, faults: Faults | None = None, output_on: bool = False, ques: int = 0,
                 v_link: float = 0.0) -> None:
        super().__init__(faults)
        self.output_on = output_on
        self.v_set, self.i_set, self.ovt, self.oct = 0.0, 0.0, 1100.0, 1.65
        self.ques = ques
        self.v_link = v_link

    def dispatch(self, c: str, key: str) -> object:
        ro = self.faults.readback_offset
        m = re.fullmatch(r"(VOLT|CURR)\s+([-+0-9.eE]+)", key)
        if m:
            if m.group(1) == "VOLT":
                self.v_set = float(m.group(2))
            else:
                self.i_set = float(m.group(2))
            return None
        table: dict[str, object] = {
            "VOLT?": f"{self.v_set + ro:.4E}", "CURR?": f"{self.i_set + ro:.4E}",
            "VOLT:PROT?": f"{self.ovt:.4E}", "CURR:PROT?": f"{self.oct:.4E}",
            "OUTP?": "1" if self.output_on else "0",
            "MEAS:VOLT?": f"{(self.v_set if self.output_on else self.v_link):.3E}",
            "MEAS:CURR?": "0.000E+00", "STAT:QUES:COND?": str(self.ques), "STAT:OPER:COND?": "64",
        }
        if key in table:
            return table[key]
        if key == "OUTP:STOP":
            self.output_on = False
            return None
        if key == "OUTP:START":
            self.output_on = True
            return None
        return NotImplemented


class AfgSim(_ScpiSim):
    idn = "TEKTRONIX,AFG31022,SIM0002,FV:1.6.3"

    def __init__(self, faults: Faults | None = None, output_on: bool = True) -> None:
        super().__init__(faults)
        self.out = {"1": output_on, "2": output_on}
        self.imp = {"1": "50", "2": "50"}
        self.high = {"1": 1.0, "2": 1.0}
        self.low = {"1": -1.0, "2": -1.0}
        self.func = {"1": "SIN", "2": "SIN"}
        self.burst = {"1": "0", "2": "0"}
        self.ncyc = {"1": "1", "2": "1"}
        self.mode = {"1": "TRIG", "2": "TRIG"}
        self.emem: dict[str, np.ndarray] = {"1": np.zeros(2, np.uint16), "2": np.zeros(2, np.uint16)}

    def dispatch(self, c: str, key: str) -> object:
        ro = self.faults.readback_offset
        if key.startswith("TRAC:DATA EMEM"):
            ch = key[len("TRAC:DATA EMEM")]
            payload = parse_ieee_block(c.encode("latin-1"))
            data = np.frombuffer(payload, dtype=">u2")
            if data.size < 2 or int(data.max()) > 16383:
                self._err(-222, "Data out of range")
                return None
            self.emem[ch] = data.copy()
            return None
        m = re.fullmatch(r"DATA:POIN\? EMEM([12])", key)
        if m:
            return str(self.emem[m.group(1)].size)
        m = re.fullmatch(r"OUTP([12]):STAT (ON|OFF|1|0)", key)
        if m:
            self.out[m.group(1)] = m.group(2) in ("ON", "1")
            return None
        m = re.fullmatch(r"OUTP([12]):STAT\?", key)
        if m:
            return "1" if self.out[m.group(1)] else "0"
        m = re.fullmatch(r"OUTP([12]):IMP (INF|50)", key)
        if m:
            self.imp[m.group(1)] = m.group(2)
            return None
        m = re.fullmatch(r"OUTP([12]):IMP\?", key)
        if m:
            return "9.9E+37" if self.imp[m.group(1)] == "INF" else "5.0E+1"
        m = re.fullmatch(r"SOUR([12]):VOLT:LEV:IMM:(HIGH|LOW)(\?|\s+[-+0-9.eE]+)", key)
        if m:
            ch, which, rest = m.groups()
            lvl = self.high if which == "HIGH" else self.low
            if rest == "?":
                return f"{lvl[ch] + ro:.4E}"
            lvl[ch] = float(rest)
            return None
        m = re.fullmatch(r"SOUR([12]):FUNC(\?| EMEM[12]| SIN| PULS)", key)
        if m:
            if m.group(2) == "?":
                return self.func[m.group(1)]
            self.func[m.group(1)] = m.group(2).strip()
            return None
        m = re.fullmatch(r"SOUR([12]):BURS:(STAT|NCYC|MODE)(\?| \S+)", key)
        if m:
            ch, what, rest = m.groups()
            store = {"STAT": self.burst, "NCYC": self.ncyc, "MODE": self.mode}[what]
            if rest == "?":
                return store[ch]
            v = rest.strip()
            store[ch] = {"ON": "1", "OFF": "0"}.get(v, v)
            return None
        return NotImplemented


class DmmSim(_ScpiSim):
    idn = "SIMDMM,DMM6500,SIM0004,1.7"

    def __init__(self, faults: Faults | None = None, v_link: float = 0.0) -> None:
        super().__init__(faults)
        self.v_link = v_link

    def dispatch(self, c: str, key: str) -> object:
        if key == "MEAS:VOLT:DC?":
            return f"{self.v_link:+.6E}"
        return NotImplemented


class LecroySim:
    """LeCroy MAUI legacy command subset; errors reported through CMR?/EXR? (not SYST:ERR?)."""

    idn = "LECROY,WR8108HD,SIM0003,9.9.0"

    def __init__(self, faults: Faults | None = None, n_points: int = 200_000) -> None:
        self.faults = faults or Faults()
        self.received: list[str] = []
        self.cmr = 0
        self.exr = 0
        self.tdiv = 1e-6
        self.vdiv = {str(i): 1.0 for i in range(1, 9)}
        self.trmd = "AUTO"
        self.chdr = "SHORT"
        self.inr = 0
        rng = np.random.default_rng(0)
        x = np.sin(np.linspace(0, 20 * np.pi, n_points)) * 8000 + rng.normal(0, 50, n_points)
        self.codes = x.astype(np.int16)

    def handle(self, command: str) -> str | bytes | None:
        self.received.append(command)
        key = command.strip().upper()
        if key in {u.upper() for u in self.faults.no_answer}:
            return None
        if key in {u.upper() for u in self.faults.unknown_commands}:
            self.cmr = 1
            return "" if "?" in key else None
        if key == "*IDN?":
            return self.faults.idn or self.idn
        if key == "*OPC?":
            return "1"
        if key == "CMR?":
            v, self.cmr = self.cmr, 0
            return str(v)
        if key == "EXR?":
            v, self.exr = self.exr, 0
            return str(v)
        if key == "CHDR OFF":
            self.chdr = "OFF"
            return None
        if key in ("CFMT DEF9,WORD,BIN", "CORD LO", "ARM"):
            return None
        if key == "FRTR":
            self.inr |= 1
            return None
        if key == "INR?":
            inr, self.inr = self.inr, 0
            return str(inr)
        m = re.fullmatch(r"WAIT( [0-9.]+)?", key)
        if m:
            return None
        if key == "TDIV?":
            return f"{self.tdiv:.2E}" if self.chdr == "OFF" else f"TDIV {self.tdiv:.2E} S"
        m = re.fullmatch(r"TDIV ([-+0-9.E]+)", key)
        if m:
            self.tdiv = float(m.group(1))
            return None
        if key == "TRMD?":
            return self.trmd
        m = re.fullmatch(r"TRMD (AUTO|NORM|SINGLE|STOP)", key)
        if m:
            self.trmd = m.group(1)
            return None
        m = re.fullmatch(r"C([1-8]):VDIV\?", key)
        if m:
            return f"{self.vdiv[m.group(1)]:.3E}"
        m = re.fullmatch(r"C([1-8]):WF\? ALL", key)
        if m:
            gain = self.vdiv[m.group(1)] * 10 / 65536
            return make_ieee_block(build_wavedesc(self.codes, gain, 0.0, self.tdiv * 10 / len(self.codes),
                                                  -5 * self.tdiv)) + b"\n"
        m = re.fullmatch(r'C([1-8]):INSPECT\? "(VERTICAL_GAIN|HORIZ_INTERVAL)"', key)
        if m:
            ch, name = m.groups()
            val = self.vdiv[ch] * 10 / 65536 if name == "VERTICAL_GAIN" else self.tdiv * 10 / len(self.codes)
            return f'"{name:<20s}: {val:.6e}"'
        self.cmr = 1
        return "" if "?" in key else None
