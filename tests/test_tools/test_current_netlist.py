"""Tests of the netlist of the pin conflict, hierarchy and search tools.

The tools read the netlist that kicad-cli writes now (netlist.current_netlist).
They used a netlist file that they found next to the sheet, or in the temporary
folder with the name of the sheet: such a file can be old, or of an other
project with the same sheet name.
"""

import asyncio
import re
import tempfile
from pathlib import Path

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.tools import netlist, pin_analysis
from kicad_workbench.utils import kicad_cli
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
    # The thread of current_netlist parses the XML (NetlistParser.load).
    with pytest.raises(
        InputError, match=f"The netlist of kicad-cli for {re.escape(str(sch))} is not valid XML"
    ):
        asyncio.run(netlist.current_netlist(sch))


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
    root = tmp_path / "root.kicad_sch"
    with pytest.raises(
        InputError, match=f"The netlist of kicad-cli for {re.escape(str(root))} is not valid XML"
    ):
        asyncio.run(netlist.current_netlist(tmp_path / "child.kicad_sch"))
    assert sources == [root]


def _two_top_level_sheets(tmp_path):
    for name in ("board", "extra"):
        (tmp_path / f"{name}.kicad_sch").write_text(SCH, encoding="utf-8")
    (tmp_path / "board.kicad_pro").write_text(
        '{"schematic": {"top_level_sheets": ['
        '{"filename": "board.kicad_sch", "name": "board", "uuid": "'
        '11111111-1111-1111-1111-111111111111"}, '
        '{"filename": "extra.kicad_sch", "name": "Extra", "uuid": "'
        '22222222-2222-2222-2222-222222222222"}]}}',
        encoding="utf-8",
    )
    return tmp_path / "board.kicad_sch"


def test_two_top_level_sheets_with_kicad_10_0_6_are_an_error(monkeypatch, tmp_path):
    # kicad-cli 10.0.6 loads only the schematic that it gets
    # (EESCHEMA_HELPERS::LoadSchematic): the netlist would not have the other
    # top-level sheet. Before, the netlist tools used it with no message.
    root = _two_top_level_sheets(tmp_path)
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.6")
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: pytest.fail("export"))
    for sch in (root, tmp_path / "extra.kicad_sch"):
        with pytest.raises(
            netlist.NetlistError,
            match="has 2 top-level sheets. kicad-cli of "
            "KiCad 10.0.6 writes the netlist of one top-level sheet only",
        ):
            netlist.netlist_of(sch)


def test_two_top_level_sheets_with_kicad_10_0_7(monkeypatch, tmp_path):
    root = _two_top_level_sheets(tmp_path)
    sources = []

    def export(command, source, file_name, timeout):
        sources.append(source)
        return CliExport(FIXED, [])

    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.7")
    monkeypatch.setattr(netlist, "export_with_kicad_cli", export)
    netlist.netlist_of(tmp_path / "extra.kicad_sch")
    assert sources == [root]


def test_one_top_level_sheet_needs_no_version(monkeypatch, sch):
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: pytest.fail("version"))
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: CliExport(FIXED, []))
    assert netlist.netlist_of(sch).get_components()


def test_erc_of_two_top_level_sheets_with_kicad_10_0_6(monkeypatch, tmp_path):
    from kicad_workbench.tools import validation

    root = _two_top_level_sheets(tmp_path)
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.6")
    result = asyncio.run(validation.run_erc(str(root)))
    assert result.startswith(
        f"❌ The project {tmp_path / 'board.kicad_pro'} has 2 top-level "
        "sheets. kicad-cli of KiCad 10.0.6 writes the ERC of one"
    ), result


def test_two_top_level_sheets_with_no_kicad_cli(monkeypatch, tmp_path):
    root = _two_top_level_sheets(tmp_path)
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: None)
    with pytest.raises(
        netlist.NetlistError,
        match="the tool needs the version of kicad-cli, but kicad-cli does not give it",
    ):
        netlist.netlist_of(root)


def test_the_erc_report_is_stale_after_a_change_of_an_other_top_level_sheet(tmp_path):
    # kicad-cli 10.0.7 and later check all top-level sheets of the project.
    from kicad_workbench.tools import validation

    root = _two_top_level_sheets(tmp_path)
    assert tmp_path / "extra.kicad_sch" in validation._erc_sources(root)


def test_the_note_of_a_top_level_sheet_that_is_not_the_root(monkeypatch, tmp_path):
    root = _two_top_level_sheets(tmp_path)
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.7")
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: CliExport(FIXED, []))
    result = asyncio.run(netlist.generate_netlist(str(tmp_path / "extra.kicad_sch")))
    assert (
        "`extra.kicad_sch` is not the root schematic of its project. The netlist is the "
        f"netlist of `{root.name}`" in result
    ), result
    assert "sub-sheet. Switched" not in result


@pytest.mark.parametrize("tool", ["get_erc_violations", "export_erc_report"])
def test_the_erc_tools_of_two_top_level_sheets_with_kicad_10_0_6(monkeypatch, tmp_path, tool):
    from kicad_workbench.tools import validation

    root = _two_top_level_sheets(tmp_path)
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.6")
    result = asyncio.run(getattr(validation, tool)(str(tmp_path / "extra.kicad_sch")))
    assert result.startswith(f"❌ The project {root.with_suffix('.kicad_pro')} has 2 "), result


def test_the_drc_report_is_stale_after_a_change_of_an_other_top_level_sheet(tmp_path):
    from kicad_workbench.tools import validation

    _two_top_level_sheets(tmp_path)
    assert tmp_path / "extra.kicad_sch" in validation._drc_sources(tmp_path / "board.kicad_pcb")


def test_the_nets_note_of_the_root_runs_no_kicad_cli(monkeypatch, tmp_path, kicad_version):
    from kicad_workbench.tools import schematic

    root = _two_top_level_sheets(tmp_path)
    # The tool needs only the version of the call (the field rules): with_schematic_versions
    # sets it, thus no more kicad-cli runs (the fixture kicad_version).
    monkeypatch.setattr(kicad_cli, "run_kicad_cli_sync", lambda *a, **k: pytest.fail("kicad-cli"))
    result = asyncio.run(schematic.list_schematic_nets(str(root)))
    assert "Note" not in result, result


def test_the_nets_note_of_an_other_top_level_sheet(monkeypatch, tmp_path, kicad_version):
    # The note needs no kicad-cli: it names the schematic of the project name.
    from kicad_workbench.tools import schematic

    root = _two_top_level_sheets(tmp_path)
    monkeypatch.setattr(kicad_cli, "run_kicad_cli_sync", lambda *a, **k: pytest.fail("kicad-cli"))
    result = asyncio.run(schematic.list_schematic_nets(str(tmp_path / "extra.kicad_sch")))
    assert (
        "`extra.kicad_sch` is not the root schematic of its project. The nets with no "
        "label need the full hierarchy: they are not here. Use `generate_netlist` on "
        f"`{root.name}`"
    ) in result, result


def test_the_sheet_paths_of_kicad_cli(tmp_path):
    # kicad-cli 10.0.7 and later load all top-level sheets (checked with the Docker
    # image: spike/kicad-versions/netlist_sheets_check.py); 10.0.6 only the root.
    root = _two_top_level_sheets(tmp_path)
    paths, _problems = kicad_cli.cli_sheet_paths(root, "10.0.7")
    assert sorted(p.shown for p in paths) == ["/Extra/", "/board/"]
    assert {p.netlist_path for p in paths} == {"/"}
    paths, _problems = kicad_cli.cli_sheet_paths(root, "10.0.6")
    assert [p.shown for p in paths] == ["/"]


def test_the_components_of_each_top_level_sheet_with_kicad_10_0_7(monkeypatch, tmp_path):
    _two_top_level_sheets(tmp_path)
    xml = (
        b'<export version="E"><design><tool>Eeschema 10.0.7</tool>'
        b'<sheet number="1" name="/board/" tstamps="/"><title_block><source>board.kicad_sch'
        b"</source></title_block></sheet>"
        b'<sheet number="2" name="/Extra/" tstamps="/"><title_block><source>extra.kicad_sch'
        b"</source></title_block></sheet></design><components>"
        b'<comp ref="R1"><value>1k</value><sheetpath names="/board/" tstamps="/"/></comp>'
        b'<comp ref="R2"><value>2k</value><sheetpath names="/Extra/" tstamps="/"/></comp>'
        b"</components><nets/></export>"
    )
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.7")
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: CliExport(xml, []))
    for name, ref in (("board", "R1"), ("extra", "R2")):
        _nl, found, warnings = netlist.components_of_file(tmp_path / f"{name}.kicad_sch", True)
        assert [c.reference for c in found] == [ref], (name, found, warnings)
        assert warnings == []


def test_a_sheet_below_a_top_level_sheet_with_a_text_variable(monkeypatch, tmp_path):
    # The sheets below a top-level sheet of kicad-cli 10.0.7 are found by their
    # "tstamps", not by "names" (KiCad expands the text variables of the names).
    _two_top_level_sheets(tmp_path)
    child = "33333333-3333-3333-3333-333333333333"
    extra = tmp_path / "extra.kicad_sch"
    extra.write_text(
        SCH.replace(
            "))\n",
            f') (sheet (at 1 1) (size 5 5) (uuid "{child}") (property "Sheetname" "${{V}}_io" '
            '(at 1 1 0)) (property "Sheetfile" "sub.kicad_sch" (at 1 6 0))))\n',
        ),
        encoding="utf-8",
    )
    (tmp_path / "sub.kicad_sch").write_text(SCH, encoding="utf-8")
    xml = (
        '<export version="E"><design><tool>Eeschema 10.0.7</tool>'
        '<sheet number="1" name="/board/" tstamps="/"/><sheet number="2" name="/Extra/" '
        f'tstamps="/"/><sheet number="3" name="/Extra/main_io/" tstamps="/{child}/"/>'
        '</design><components><comp ref="R3"><value>3k</value><sheetpath '
        f'names="/Extra/main_io/" tstamps="/{child}/"/></comp></components><nets/></export>'
    ).encode()
    monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.7")
    monkeypatch.setattr(netlist, "export_with_kicad_cli", lambda *args: CliExport(xml, []))
    _nl, found, warnings = netlist.components_of_file(extra, True)
    assert [c.reference for c in found] == ["R3"], warnings
