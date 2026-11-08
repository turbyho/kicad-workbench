"""Tests of the report of detect_pin_conflicts."""

import asyncio

import pytest

from kicad_workbench.tools import netlist as netlist_tools
from kicad_workbench.tools import pin_analysis
from kicad_workbench.utils.kicad_cli import CliExport

# The tests give the tool the netlist and the version of KiCad: they need no KiCad.
pytestmark = pytest.mark.usefixtures("kicad_version")

SCH = '(kicad_sch (version 20250114) (generator "eeschema"))\n'

def _conflicts(tmp_path, monkeypatch, nets: str) -> str:
    netlist = tmp_path / "c.xml"
    netlist.write_text(
        '<export version="E"><design><tool>Eeschema 10.0.6</tool></design><components>'
        '<comp ref="U1"><value>U</value><libsource lib="L" part="U"/></comp>'
        '<comp ref="U2"><value>U</value><libsource lib="L" part="U"/></comp>'
        f"</components><nets>{nets}</nets></export>",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        netlist_tools, "export_with_kicad_cli", lambda *args: CliExport(netlist.read_bytes(), [])
    )
    sch = tmp_path / "c.kicad_sch"
    sch.write_text(SCH, encoding="utf-8")
    return asyncio.run(pin_analysis.detect_pin_conflicts(str(sch)))


UNCONNECTED = (
    '<net code="1" name="unconnected-(U1-A-Pad1)">'
    '<node ref="U1" pin="1" pintype="input+no_connect"/></net>'
)
DRIVERS = (
    '<net code="2" name="N"><node ref="U1" pin="2" pintype="output"/>'
    '<node ref="U2" pin="2" pintype="output"/></net>'
)


def test_only_unconnected_pins_are_no_conflict(tmp_path, monkeypatch):
    # An unconnected pin is information: no "❌" (CLAUDE.md, honest output).
    result = _conflicts(tmp_path, monkeypatch, UNCONNECTED)
    assert result.startswith("✅ **No Pin Conflicts Detected**"), result
    assert "- ℹ️ 1 unconnected pin(s), listed below" in result
    assert "| ℹ️ info | unconnected_pin |" in result


def test_two_drivers_are_an_error(tmp_path, monkeypatch):
    result = _conflicts(tmp_path, monkeypatch, UNCONNECTED + DRIVERS)
    assert result.startswith("❌ **Pin Conflicts Detected**"), result
    assert "- Errors: 1\n- Warnings: 0\n- Unconnected pins (info): 1" in result
    assert "| ❌ error | multiple_outputs |" in result
