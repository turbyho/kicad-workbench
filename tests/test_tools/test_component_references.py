"""Tests of the references of the schematic tools.

list_schematic_components, get_schematic_info, search_symbols and get_symbol_details
took the reference of a symbol from its Reference field. KiCad gives each sheet
path its own reference and unit (SCH_SYMBOL::GetRef, GetUnitSelection): a sheet
that the hierarchy uses two times has two references, and a symbol with "U?" in
its Reference field has the reference of its instance. Now the tools use the
references and units of placed_instances (view "cli", as the netlist).
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from kicad_workbench.parsers import sheet_list
from kicad_workbench.tools import schematic

from .test_symbol_fields import symbol, write_project

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """board.kicad_sch with U1 (Reference field "U?", reference only in its instance)
    and the sheet sub.kicad_sch two times (A: /a0/b1, B: /a0/b2) with one LM358:
    U5 in A and U6 in B (its Reference field is U5)."""

    async def version():
        return "10.0.6"

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    monkeypatch.setattr(schematic, "netlist_of", _no_netlist)
    root = symbol("U?", 1, paths=(("/a0", "U1"),))
    sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
    write_project(tmp_path, root, sub)
    return tmp_path


def _no_netlist(path):
    from kicad_workbench.tools.netlist import NetlistError

    raise NetlistError("no kicad-cli in the test")


def run(coroutine):
    return asyncio.run(coroutine)


def test_a_sheet_used_two_times_gives_two_references(project):
    result = run(schematic.list_schematic_components(str(project / "sub.kicad_sch")))
    assert "Total: 2 component(s)" in result, result
    assert "| U5 | LM358 | - | A:LM358 | /A/ |" in result
    assert "| U6 | LM358 | - | A:LM358 | /B/ |" in result


def test_the_reference_of_the_instance(project):
    result = run(schematic.list_schematic_components(str(project / "board.kicad_sch")))
    assert "| U1 | LM358 |" in result, result
    assert "U?" not in result


def test_the_info_counts_the_references_of_each_sheet_path(project):
    result = run(schematic.get_schematic_info(str(project / "sub.kicad_sch")))
    assert "**Total Components:** 2 (in 2 sheet paths of the file)" in result, result
    assert "- U: 2" in result


def test_search_by_field(project):
    sub = str(project / "sub.kicad_sch")
    result = run(schematic.search_symbols(sub, "U6", "reference"))
    assert "| U6 | LM358 | A:LM358 |" in result and "U5" not in result, result
    result = run(schematic.search_symbols(sub, "U6", "value"))
    assert result.startswith("No components found matching pattern: U6"), result
    result = run(schematic.search_symbols(sub, "(", "all"))
    assert result.startswith("❌ The pattern '(' is not a regular expression"), result
    result = run(schematic.search_symbols(sub, "U", "pins"))
    assert result.startswith("❌ search_fields 'pins' is not known"), result


def test_the_details_of_a_reference_of_an_other_file(project):
    # The reference is a reference of the project: U6 is in sub.kicad_sch.
    result = run(schematic.get_symbol_details(str(project / "board.kicad_sch"), "U6"))
    assert result.startswith("# Symbol Details: U6"), result
    assert "**Sheet:** /B/ (`sub.kicad_sch`)" in result


def test_no_kicad_version(project, monkeypatch):
    async def version():
        return None

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    result = run(schematic.list_schematic_components(str(project / "sub.kicad_sch")))
    assert result.startswith("❌ KiCad is not installed"), result
    assert "Tell the user: install KiCad 8 or later" in result


def test_kicad_7_is_an_error(project, monkeypatch):
    """The tool has the minimum version of the server (installed_kicad_problem)."""

    async def version():
        return "7.0.11"

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    result = run(schematic.list_schematic_components(str(project / "sub.kicad_sch")))
    assert result.startswith(
        "❌ KiCad 7.0.11 is older than KiCad 8, the minimum of this server. "
        "Tell the user: install KiCad 8 or later"
    ), result


@pytest.fixture
def unannotated(tmp_path, monkeypatch):
    """Two symbols that are not annotated (U?) and the two units of U3."""

    async def version():
        return "10.0.6"

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    monkeypatch.setattr(schematic, "netlist_of", _no_netlist)
    root = (
        symbol("U?", 1, paths=(("/a0", "U?"),))
        + symbol("U?", 1, paths=(("/a0", "U?"),)).replace("(at 100 50 0)", "(at 120 50 0)", 1)
        + symbol("U3", 1, paths=(("/a0", "U3"),)).replace("(at 100 50 0)", "(at 140 50 0)", 1)
        + symbol("U3", 2, paths=(("/a0", "U3"),)).replace("(at 100 50 0)", "(at 160 50 0)", 1)
    )
    return write_project(tmp_path, root)


def test_symbols_that_are_not_annotated_are_not_one_component(unannotated):
    result = run(schematic.get_schematic_info(str(unannotated)))
    # U? two times, and U3 (two units, one component).
    assert "**Total Components:** 3" in result, result
    components = schematic.project_components(unannotated, "10.0.6")
    assert sorted(c.reference for c in components) == ["U3", "U?", "U?"]


def test_the_details_of_a_reference_that_is_not_annotated(unannotated):
    result = run(schematic.get_symbol_details(str(unannotated), "U?"))
    assert result.startswith("❌ 'U?' is not annotated"), result


def test_the_details_of_two_units(unannotated):
    result = run(schematic.get_symbol_details(str(unannotated), "U3"))
    assert "**Placed units:** unit 1 in /, unit 2 in /" in result, result


def test_the_info_lists_the_sheet_symbols_as_kicad_loads_them(tmp_path, monkeypatch):
    # A sheet with no file field (KiCad 10: an empty file name) and a sheet of a file
    # that is not there: KiCad shows an empty sheet for each (measured with kicad-cli
    # 8.0.9 to 10.0.7: the page is in the SVG export). A name of white space only.
    async def version():
        return "10.0.7"

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    monkeypatch.setattr(schematic, "netlist_of", _no_netlist)
    sheets = (
        '\t(sheet (at 1 1) (size 2 2) (uuid "c1")\n'
        '\t\t(property "Sheetname" "Leaf" (at 0 0 0))\n\t)\n'
        '\t(sheet (at 1 1) (size 2 2) (uuid "c2")\n'
        '\t\t(property "Sheetname" " " (at 0 0 0))\n'
        '\t\t(property "Sheetfile" "gone.kicad_sch" (at 0 0 0))\n\t)\n'
    )
    path = write_project(tmp_path, symbol("U1", 1, paths=(("/a0", "U1"),)) + sheets)
    result = run(schematic.get_schematic_info(str(path)))
    assert "**Sheet symbols:** 2" in result, result
    assert "- Leaf: (no file name) (the sheet has no file name: an empty sheet in KiCad)" in result
    assert (
        "- (no name: ' '): gone.kicad_sch (the file is not there: an empty sheet in KiCad)"
        in result
    )


def test_the_info_of_a_loaded_sheet_and_of_an_ancestor(project, tmp_path):
    # The fixture: board.kicad_sch with the sheets A and B of sub.kicad_sch.
    sub = tmp_path / "sub.kicad_sch"
    loop = (
        '\t(sheet (at 1 1) (size 2 2) (uuid "c3")\n'
        '\t\t(property "Sheetname" "Loop" (at 0 0 0))\n'
        '\t\t(property "Sheetfile" "board.kicad_sch" (at 0 0 0))\n\t)\n)\n'
    )
    sub.write_text(sub.read_text(encoding="utf-8").rstrip()[:-1] + loop, encoding="utf-8")
    result = run(schematic.get_schematic_info(str(tmp_path / "board.kicad_sch")))
    assert "- A: sub.kicad_sch\n- B: sub.kicad_sch" in result, result
    result = run(schematic.get_schematic_info(str(sub)))
    assert (
        "- Loop: board.kicad_sch (the file holds its own ancestor: an empty sheet in KiCad)"
        in result
    ), result


def test_the_info_of_a_file_with_two_names(project):
    # KiCad loads sub.kicad_sch under two names (sheet_file_names): two screens.
    root = project / "board.kicad_sch"
    text = root.read_text(encoding="utf-8").replace(
        '"Sheetfile" "sub.kicad_sch"', f'"Sheetfile" "{project.as_posix()}/./sub.kicad_sch"', 1
    )
    root.write_text(text, encoding="utf-8")
    result = run(schematic.get_schematic_info(str(project / "sub.kicad_sch")))
    assert "⚠️ KiCad loads this file under 2 names" in result, result
    assert "**Total Components:** 2" in result


def test_the_info_of_a_file_that_the_load_does_not_hold(project, tmp_path, monkeypatch):
    # A guard: the project of the file (sch_hierarchy) and the load of its components
    # use the same loader (sheet_list). When the load does not hold the file, the
    # tool tells that KiCad does not load it.
    real = schematic.placed_components_and_files

    def no_file(path, kicad, project_wide=False):
        placed, _screens, problems = real(path, kicad, project_wide)
        return placed, {}, problems

    monkeypatch.setattr(schematic, "placed_components_and_files", no_file)
    result = run(schematic.get_schematic_info(str(tmp_path / "sub.kicad_sch")))
    assert "⚠️ The hierarchy of the project does not hold this file" in result, result


def test_the_info_gives_the_expanded_file_and_loads_a_file_one_time(tmp_path, monkeypatch):
    async def version():
        return "10.0.7"

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    monkeypatch.setattr(schematic, "netlist_of", _no_netlist)
    sheets = "".join(
        f'\t(sheet (at 1 1) (size 2 2) (uuid "c{index}")\n'
        f'\t\t(property "Sheetname" "S{index}" (at 0 0 0))\n'
        '\t\t(property "Sheetfile" "${DIR}/gone.kicad_sch" (at 0 0 0))\n\t)\n'
        for index in (1, 2)
    )
    path = write_project(
        tmp_path,
        symbol("U1", 1, paths=(("/a0", "U1"),)) + sheets,
        settings=None,
    )
    project_file = tmp_path / "board.kicad_pro"
    project_file.write_text('{"text_variables": {"DIR": "sheets"}}', encoding="utf-8")
    loads = []
    real = sheet_list._read_screen

    def counted(name, kicad):
        loads.append(Path(name.text).name)
        return real(name, kicad)

    monkeypatch.setattr(sheet_list, "_read_screen", counted)
    result = run(schematic.get_schematic_info(str(path)))
    gone = tmp_path / "sheets" / "gone.kicad_sch"
    assert f"- S1: ${{DIR}}/gone.kicad_sch = {gone} (the file is not there" in result, result
    assert loads.count("gone.kicad_sch") == 1


@pytest.mark.usefixtures("kicad_version")
def test_the_type_filter_has_no_difference_of_case(tmp_path):
    """KiCad allows a reference with lower case letters ("u1"): the filter "U" finds it."""
    sch = tmp_path / "board.kicad_sch"
    shutil.copy(FIXTURES / "example_schematic.kicad_sch", sch)
    text = sch.read_text(encoding="utf-8").replace('"R1"', '"r1"')
    sch.write_text(text, encoding="utf-8")
    result = asyncio.run(schematic.list_schematic_components(str(sch), filter_type="r"))
    assert "| r1 |" in result and "| R2 |" in result, result


def test_a_value_with_a_bar_is_one_cell(tmp_path, monkeypatch):
    """A "|" or a line break of a field does not add a column to the tables."""

    async def version():
        return "10.0.6"

    monkeypatch.setattr(schematic, "get_kicad_version", version)
    sch = tmp_path / "board.kicad_sch"
    shutil.copy(FIXTURES / "example_schematic.kicad_sch", sch)
    text = sch.read_text(encoding="utf-8").replace('"Value" "10k"', '"Value" "10k|1%\\n0805"')
    sch.write_text(text, encoding="utf-8")
    listed = asyncio.run(schematic.list_schematic_components(str(sch)))
    found = asyncio.run(schematic.search_symbols(str(sch), "10k"))
    for result in (listed, found):
        row = next(line for line in result.splitlines() if line.startswith("| R1 "))
        assert "| 10k\\|1% 0805 |" in row, row
