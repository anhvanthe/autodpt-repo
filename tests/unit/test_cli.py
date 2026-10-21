"""CLI smoke tests (NET-06 simulation mode, ARC-05 config check)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodpt.app import main

EX = Path(__file__).resolve().parents[2] / "config" / "examples"


@pytest.mark.req("NET-06")
@pytest.mark.parametrize("device", ["igbt", "sic"])
def test_sim_mode_runs(device: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sim", "--device", device, "--skew", "3"]) == 0
    out = capsys.readouterr().out
    assert "validity VALID" in out and "e_on" in out


@pytest.mark.req("ARC-05")
def test_check_config(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    setup = EX / "setup_fs820_hs_400v.yaml"
    assert main(["check-config", str(setup), "--probes", str(EX / "probes.yaml")]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text(setup.read_text().replace("c_dclink_f: 1.0e-3", "c_dclink_f: -1"))
    assert main(["check-config", str(bad)]) == 2
    assert "c_dclink_f" in capsys.readouterr().err
