"""Tests of the netlist of the pin, device tree, hierarchy and search tools.

The tools read the netlist that kicad-cli writes now (netlist.current_netlist).
They used a netlist file that they found next to the sheet, or in the temporary
folder with the name of the sheet: such a file can be old, or of an other
project with the same sheet name.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.tools import netlist, pin_analysis
from kicad_workbench.utils.kicad_cli import CliExport

SCH = '(kicad_sch (version 20250114) (generator "eeschema"))\n'


def _netlist(nets: str) -> bytes:
    return (
        '<export version="E"><design><tool>Eeschema 10.0.6</tool></design><components>'
        '<comp ref="U1"><value>U</value><libsource lib="L" part="U"/></comp>'
        '<comp ref="U2"><value>U</value><libsource lib="L" part="U"/></comp>'
        f"</components><nets>{nets}</nets></export>"
    ).encode()


# Two output pins on one net: an error of detect_pin_conflicts.
DRIVERS = _netlist(
    '<net code="1" name="N"><node ref="U1" pin="2" pintype="output"/>'
    '<node ref="U2" pin="2" pintype="output"/></net>'
)
# The same design after the edit: one output pin and one input pin.
FIXED = _netlist(
    '<net code="1" name="N"><node ref="U1" pin="2" pintype="output"/>'
    '<node ref="U2" pin="2" pintype="input"/></net>'
)


@pytest.fixture
def sch(tmp_path):
    path = tmp_path / f"current_{tmp_path.name}.kicad_sch"
    path.write_text(SCH, encoding="utf-8")
    old = [
        path.with_suffix(".xml"),  # an export of the schematic editor
        Path(tempfile.gettempdir()) / f"{path.stem}.xml",  # the old name in the temp folder
        netlist.netlist_cache_path(path),  # the file of generate_netlist
    ]
    for file in old:
        file.write_bytes(DRIVERS)
    yield path
    for file in old:
        file.unlink(missing_ok=True)


def test_an_old_netlist_file_is_not_used(monkeypatch, sch):
    calls = []

    def export(command, source, file_name, timeout):
        calls.append(source)
        return CliExport(FIXED, [])

    monkeypatch.setattr(netlist, "export_with_kicad_cli", export)
    result = asyncio.run(pin_analysis.detect_pin_conflicts(str(sch)))
    assert result.startswith("✅ **No Pin Conflicts Detected**"), result
    assert calls == [sch]
    # The file of generate_netlist did not change: the tools do not write it.
    assert netlist.netlist_cache_path(sch).read_bytes() == DRIVERS


def test_each_call_reads_the_schematic_again(monkeypatch, sch):
    data = [DRIVERS, FIXED]
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: CliExport(data.pop(0), []))
    first = asyncio.run(pin_analysis.detect_pin_conflicts(str(sch)))
    second = asyncio.run(pin_analysis.detect_pin_conflicts(str(sch)))
    assert first.startswith("❌ **Pin Conflicts Detected**"), first
    assert second.startswith("✅ **No Pin Conflicts Detected**"), second


def test_a_netlist_of_kicad_cli_that_is_not_xml(monkeypatch, sch):
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: CliExport(b"<export", []))
    parser = asyncio.run(netlist.current_netlist(sch))
    with pytest.raises(InputError, match=f"The netlist of kicad-cli for {sch} is not valid XML"):
        parser.get_nets()


def test_a_sub_sheet_gives_the_netlist_of_its_root(monkeypatch, tmp_path):
    import shutil

    hier = Path(__file__).resolve().parent.parent / "fixtures" / "hier"
    for file in hier.glob("*.kicad_sch"):
        shutil.copy(file, tmp_path / file.name)
    # A root schematic is a schematic with the project file of its name.
    (tmp_path / "root.kicad_pro").write_text("{}\n", encoding="utf-8")
    sources = []

    def export(command, source, file_name, timeout):
        sources.append(source)
        return CliExport(b"<export", [])

    monkeypatch.setattr(netlist, "export_with_kicad_cli", export)
    parser = asyncio.run(netlist.current_netlist(tmp_path / "child.kicad_sch"))
    root = tmp_path / "root.kicad_sch"
    assert sources == [root]
    with pytest.raises(InputError, match=f"The netlist of kicad-cli for {root} is not valid XML"):
        parser.get_nets()
