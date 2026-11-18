"""Tests of the argument refill_zones of run_drc, get_drc_violations and export_drc_report.

A fake kicad-cli writes the report: the tests need no KiCad. The DRC of kicad-cli with
--refill-zones is in spike/kicad-versions/drc_refill_check.py.
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from kicad_workbench.tools import validation

BOARD = '(kicad_pcb (version 20260206) (generator "pcbnew") (zone (net 0) (layer "F.Cu")))\n'


def _report(violations: list[dict]) -> str:
    return json.dumps({"violations": violations, "unconnected_items": [], "schematic_parity": []})


CLEARANCE = {
    "type": "clearance",
    "description": "Clearance violation",
    "severity": "error",
    "items": [],
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A board, a fake kicad-cli (its calls), and KiCad 10.0.6."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(BOARD, encoding="utf-8")
    calls: list[list[str]] = []

    async def kicad_cli(args, *rest, **kwargs):
        calls.append(args)
        # With the refill, the old fill of the file gives no violation.
        output = Path(args[args.index("--output") + 1])
        refill = "--refill-zones" in args
        output.write_text(_report([] if refill else [CLEARANCE]), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    async def version():
        return "10.0.6"

    monkeypatch.setattr(validation, "run_kicad_cli", kicad_cli)
    monkeypatch.setattr(validation, "get_kicad_version", version)
    monkeypatch.setattr(validation, "_temp_dir", lambda: tmp_path)
    return pcb, calls


def test_run_drc_passes_the_refill_to_kicad_cli(board):
    pcb, calls = board
    out = asyncio.run(validation.run_drc(str(pcb), refill_zones=True))
    assert "--refill-zones" in calls[-1] and "--save-board" not in calls[-1]
    assert out.startswith("✅ **DRC Check Passed**"), out
    assert "**Zones:** filled again before the DRC (in memory" in out
    out = asyncio.run(validation.run_drc(str(pcb)))
    assert "--refill-zones" not in calls[-1]
    # The answer with violations tells the zone mode too.
    assert out.startswith("❌ **DRC Violations Detected**"), out
    assert "**Zones:** the fills saved in the board file (not filled again; refill_zones" in out


def test_each_zone_mode_has_its_own_report(board):
    pcb, calls = board
    with_fill = asyncio.run(validation.get_drc_violations(str(pcb), refill_zones=True))
    without = asyncio.run(validation.get_drc_violations(str(pcb)))
    assert with_fill.startswith("✅ **No DRC Violations**"), with_fill
    assert "**Zones:** the fills saved in the board file" in without
    assert "clearance" in without
    assert len(calls) == 2
    # The reports of the two modes stay: no new run.
    asyncio.run(validation.get_drc_violations(str(pcb), refill_zones=True))
    assert len(calls) == 2


def test_export_with_the_refill(board, tmp_path):
    pcb, calls = board
    output = tmp_path / "out.json"
    out = asyncio.run(validation.export_drc_report(str(pcb), str(output), refill_zones=True))
    assert "--refill-zones" in calls[-1]
    assert json.loads(output.read_text(encoding="utf-8"))["violations"] == [], out


@pytest.mark.parametrize(
    "call",
    [
        lambda pcb: validation.run_drc(pcb, refill_zones=True),
        lambda pcb: validation.get_drc_violations(pcb, refill_zones=True),
        lambda pcb: validation.export_drc_report(pcb, refill_zones=True),
    ],
)
@pytest.mark.parametrize(
    "installed, text",
    [
        (
            "9.0.9",
            "❌ refill_zones has the rules of KiCad 10.0 only. The installed KiCad is 9.0.9.",
        ),
        (
            "10.99.0",
            "❌ refill_zones has the rules of KiCad 10.0 only. The installed KiCad is 10.99.0.",
        ),
        # No KiCad: the full reason of installed_kicad_problem.
        (None, "❌ KiCad is not installed: kicad-cli was not found."),
    ],
)
def test_the_refill_needs_kicad_10(board, monkeypatch, call, installed, text):
    pcb, calls = board

    async def version():
        return installed

    monkeypatch.setattr(validation, "get_kicad_version", version)
    monkeypatch.setattr("kicad_workbench.utils.kicad_cli.find_kicad_cli", lambda: None)
    out = asyncio.run(call(str(pcb)))
    assert out.startswith(text), out
    assert calls == []


def test_a_board_with_no_zones(board):
    pcb, calls = board
    pcb.write_text('(kicad_pcb (version 20260206) (generator "pcbnew"))\n', encoding="utf-8")
    out = asyncio.run(validation.run_drc(str(pcb)))
    assert "**Zones:** the board has no zones" in out, out


def test_the_default_files_of_the_export(board):
    pcb, calls = board
    asyncio.run(validation.export_drc_report(str(pcb)))
    asyncio.run(validation.export_drc_report(str(pcb), refill_zones=True))
    assert (pcb.parent / "b_drc_report.json").is_file()
    assert (pcb.parent / "b_drc_refill_report.json").is_file()


def test_a_failed_refill_names_the_refill(board, monkeypatch):
    pcb, calls = board

    async def failing(args, *rest, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"", b"zone fill failed")

    monkeypatch.setattr(validation, "run_kicad_cli", failing)
    out = asyncio.run(validation.run_drc(str(pcb), refill_zones=True))
    assert out.startswith("❌ **DRC Check Failed**"), out
    assert "**Zones:** filled again before the DRC" in out
    assert "- The zone fill of --refill-zones: run the DRC again with refill_zones=False" in out


def test_no_refill_hint_for_kicad_9(board, monkeypatch):
    pcb, calls = board

    async def version():
        return "9.0.9"

    monkeypatch.setattr(validation, "get_kicad_version", version)
    out = asyncio.run(validation.run_drc(str(pcb)))
    assert "**Zones:** the fills saved in the board file (not filled again)" in out, out
