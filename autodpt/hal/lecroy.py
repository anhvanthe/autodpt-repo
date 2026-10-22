"""LeCroy waveform transfer helpers: IEEE-488.2 block parsing and WAVEDESC decoding (SRS SCO-05).

The descriptor layout follows the LeCroy WAVEDESC template (offsets relative to the 'WAVEDESC' tag).
Bench-check verifies the offsets on the real instrument by comparing the decoded VERTICAL_GAIN and
HORIZ_INTERVAL with the scope's own `INSPECT?` text read-back.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from autodpt.analysis.types import RawChannel

# offset, struct format (without byte-order prefix)
_FIELDS: dict[str, tuple[int, str]] = {
    "COMM_TYPE": (32, "h"),          # 0 = byte, 1 = word
    "COMM_ORDER": (34, "h"),         # 0 = big-endian (HIFIRST), 1 = little-endian (LOFIRST)
    "WAVE_DESCRIPTOR": (36, "i"),
    "USER_TEXT": (40, "i"),
    "RES_DESC1": (44, "i"),
    "TRIGTIME_ARRAY": (48, "i"),
    "RIS_TIME_ARRAY": (52, "i"),
    "RES_ARRAY1": (56, "i"),
    "WAVE_ARRAY_1": (60, "i"),
    "WAVE_ARRAY_COUNT": (116, "i"),
    "VERTICAL_GAIN": (156, "f"),
    "VERTICAL_OFFSET": (160, "f"),
    "NOMINAL_BITS": (172, "h"),
    "HORIZ_INTERVAL": (176, "f"),
    "HORIZ_OFFSET": (180, "d"),
}
DESC_LEN = 346


class BlockError(ValueError):
    pass


def parse_ieee_block(raw: bytes) -> bytes:
    """Return the payload of '#<n><len><payload>' (an optional text prefix before '#' is skipped)."""
    i = raw.find(b"#")
    if i < 0 or i + 2 > len(raw):
        raise BlockError("no IEEE-488.2 block header")
    n = int(chr(raw[i + 1]))
    if n == 0:
        return raw[i + 2:].rstrip(b"\n")
    length = int(raw[i + 2:i + 2 + n])
    start = i + 2 + n
    payload = raw[start:start + length]
    if len(payload) != length:
        raise BlockError(f"block truncated: {len(payload)} of {length} bytes")
    return payload


def make_ieee_block(payload: bytes) -> bytes:
    ln = str(len(payload)).encode()
    return b"#" + str(len(ln)).encode() + ln + payload


@dataclass
class LecroyWaveform:
    desc: dict[str, float | int]
    codes: np.ndarray

    @property
    def dt(self) -> float:
        return float(self.desc["HORIZ_INTERVAL"])

    @property
    def t0(self) -> float:
        return float(self.desc["HORIZ_OFFSET"])

    def volts(self) -> np.ndarray:
        """LeCroy convention: value = VERTICAL_GAIN · code − VERTICAL_OFFSET."""
        return self.codes.astype(np.float64) * float(self.desc["VERTICAL_GAIN"]) - float(self.desc["VERTICAL_OFFSET"])

    def to_raw_channel(self, signal: str, invert: bool = False) -> RawChannel:
        word = int(self.desc["COMM_TYPE"]) == 1
        lim = 32767 if word else 127
        return RawChannel(signal=signal, codes=self.codes.astype(np.int16), gain=float(self.desc["VERTICAL_GAIN"]),
                          offset=-float(self.desc["VERTICAL_OFFSET"]), invert=invert, code_min=-lim - 1,
                          code_max=lim)


def parse_wavedesc(payload: bytes) -> LecroyWaveform:
    base = payload.find(b"WAVEDESC")
    if base < 0:
        raise BlockError("WAVEDESC tag not found")
    order = struct.unpack_from("<h", payload, base + 34)[0]
    bo = "<" if order == 1 else ">"
    desc: dict[str, float | int] = {}
    for name, (off, fmt) in _FIELDS.items():
        desc[name] = struct.unpack_from(bo + fmt, payload, base + off)[0]
    start = base + int(desc["WAVE_DESCRIPTOR"]) + int(desc["USER_TEXT"]) + int(desc["TRIGTIME_ARRAY"]) \
        + int(desc["RIS_TIME_ARRAY"]) + int(desc["RES_ARRAY1"])
    n_bytes = int(desc["WAVE_ARRAY_1"])
    dtype = np.dtype(bo + ("i2" if int(desc["COMM_TYPE"]) == 1 else "i1"))
    data = payload[start:start + n_bytes]
    if len(data) != n_bytes:
        raise BlockError(f"waveform data truncated: {len(data)} of {n_bytes} bytes")
    return LecroyWaveform(desc=desc, codes=np.frombuffer(data, dtype=dtype).copy())


def build_wavedesc(codes: np.ndarray, gain: float, offset: float, dt: float, t0: float,
                   little_endian: bool = True) -> bytes:
    """Inverse of parse_wavedesc — used by the scope simulator and tests."""
    bo = "<" if little_endian else ">"
    desc = bytearray(DESC_LEN)
    desc[0:8] = b"WAVEDESC"
    data = np.asarray(codes, dtype=bo + "i2").tobytes()
    values: dict[str, float | int] = {
        "COMM_TYPE": 1, "COMM_ORDER": 1 if little_endian else 0, "WAVE_DESCRIPTOR": DESC_LEN,
        "USER_TEXT": 0, "RES_DESC1": 0, "TRIGTIME_ARRAY": 0, "RIS_TIME_ARRAY": 0, "RES_ARRAY1": 0,
        "WAVE_ARRAY_1": len(data), "WAVE_ARRAY_COUNT": len(codes), "VERTICAL_GAIN": gain,
        "VERTICAL_OFFSET": offset, "NOMINAL_BITS": 12, "HORIZ_INTERVAL": dt, "HORIZ_OFFSET": t0,
    }
    for name, (off, fmt) in _FIELDS.items():
        struct.pack_into(bo + fmt, desc, off, values[name])
    return bytes(desc) + data
