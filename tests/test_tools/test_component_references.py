"""Tests of the references of the schematic tools.

list_schematic_components, get_schematic_info, search_symbols and get_symbol_details
took the reference of a symbol from its Reference field. KiCad gives each sheet
path its own reference and unit (SCH_SYMBOL::GetRef, GetUnitSelection): a sheet
that the hierarchy uses two times has two references, and a symbol with "U?" in
its Reference field has the reference of its instance. Now the tools use the
references and units of placed_instances (view "cli", as the netlist).
"""

import asyncio

import pytest

from kicad_workbench.tools import schematic

from .test_symbol_fields import symbol, write_project


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
    assert "| U5 | LM358 | - | LM358 | /A/ |" in result
    assert "| U6 | LM358 | - | LM358 | /B/ |" in result


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
    assert "| U6 | LM358 | LM358 |" in result and "U5" not in result, result
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
    assert result.startswith("❌ The version of KiCad is not known"), result


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
