"""Tests for add_component_from_library: the library tables and the placed pins."""

import asyncio
from pathlib import Path

import pytest

from kicad_workbench.parsers.sexpr import parse, quote
from kicad_workbench.tools import schematic_editor

from .installed_kicad import write_schematic
from .symbol_tables import row, table, use_global_table
from .test_sch_hierarchy import project


def library(*symbols: str) -> str:
    return (
        '(kicad_symbol_lib (version 20251024) (generator "test")\n' + "\n".join(symbols) + "\n)\n"
    )


def resistor(description: str, pins: int = 2) -> str:
    pin_list = " ".join(f'(pin passive line (number "{n}"))' for n in range(1, pins + 1))
    return (
        f'(symbol "R" (property "Reference" "R") (property "Description" "{description}")\n'
        f'(symbol "R_1_1" {pin_list}))'
    )


@pytest.fixture
def libs(tmp_path, monkeypatch):
    """A global library "Device" and a schematic of KiCad 10 in a project."""
    directory = tmp_path / "libs"
    directory.mkdir()
    (directory / "Device.kicad_sym").write_text(library(resistor("global")), encoding="utf-8")
    global_table = use_global_table(
        tmp_path, monkeypatch, row("Device", directory / "Device.kicad_sym")
    )
    board = tmp_path / "board"
    board.mkdir()
    return {"dir": directory, "table": global_table, "board": board}


def add(path: Path, symbol: str = "R", library_name: str = "Device") -> str:
    return asyncio.run(
        schematic_editor.add_component_from_library(str(path), library_name, symbol, "R1", "1k")
    )


def description(path: Path) -> str:
    """The Description field of the symbol in the lib_symbols section."""
    lib = parse(path.read_text(encoding="utf-8")).first("lib_symbols")
    for prop in lib.first("symbol").children("property"):
        if prop.value(0) == "Description":
            return prop.value(1)
    raise AssertionError("no Description")


class TestLibraryTables:
    def test_global_library(self, libs):
        sch = write_schematic(libs["board"], 20260306)
        result = add(sch)
        assert "✅" in result, result
        assert f"inserted into lib_symbols from {libs['dir'] / 'Device.kicad_sym'}" in result
        assert description(sch) == "global"

    def test_project_library_replaces_the_global_library(self, libs):
        sch = write_schematic(libs["board"], 20260306)
        (libs["board"] / "board.kicad_pro").write_text("{}", encoding="utf-8")
        (libs["board"] / "Device.kicad_sym").write_text(
            library(resistor("project")), encoding="utf-8"
        )
        (libs["board"] / "sym-lib-table").write_text(
            table(row("Device", "${KIPRJMOD}/Device.kicad_sym")), encoding="utf-8"
        )
        assert "✅" in add(sch)
        assert description(sch) == "project"

    def test_schematic_with_no_project_file_uses_the_table_next_to_it(self, libs):
        # The schematic editor opens such a schematic in a project of its name.
        sch = write_schematic(libs["board"], 20260306)
        (libs["board"] / "Local.kicad_sym").write_text(library(resistor("local")), encoding="utf-8")
        (libs["board"] / "sym-lib-table").write_text(
            table(row("Local", "${KIPRJMOD}/Local.kicad_sym")), encoding="utf-8"
        )
        assert "✅" in add(sch, library_name="Local")
        assert description(sch) == "local"

    def test_sub_sheet_uses_the_table_of_the_root_project(self, libs):
        project(libs["board"], "board", "sheets/power.kicad_sch")
        sheets = libs["board"] / "sheets"
        sheets.mkdir()
        sub = write_schematic(sheets, 20260306).rename(sheets / "power.kicad_sch")
        (libs["board"] / "Local.kicad_sym").write_text(library(resistor("root")), encoding="utf-8")
        (libs["board"] / "sym-lib-table").write_text(
            table(row("Local", "${KIPRJMOD}/Local.kicad_sym")), encoding="utf-8"
        )
        result = add(sub, library_name="Local")
        assert "✅" in result, result
        assert description(sub) == "root"

    def test_project_next_to_the_target_of_a_link(self, libs, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        target = write_schematic(real, 20260306)
        (real / "board.kicad_pro").write_text("{}", encoding="utf-8")
        (real / "Local.kicad_sym").write_text(library(resistor("linked")), encoding="utf-8")
        (real / "sym-lib-table").write_text(
            table(row("Local", "${KIPRJMOD}/Local.kicad_sym")), encoding="utf-8"
        )
        link = libs["board"] / "board.kicad_sch"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("no permission to make a symbolic link")
        assert "✅" in add(link, library_name="Local")
        assert description(target) == "linked"

    def test_directory_library(self, libs):
        symdir = libs["dir"] / "Dir.kicad_symdir"
        symdir.mkdir()
        (symdir / "R.kicad_sym").write_text(library(resistor("directory")), encoding="utf-8")
        libs["table"].write_text(table(row("Dir", symdir)), encoding="utf-8")
        sch = write_schematic(libs["board"], 20260306)
        assert "✅" in add(sch, library_name="Dir")
        assert description(sch) == "directory"


class TestTableProblems:
    def test_table_that_cannot_be_read(self, libs):
        # KiCad uses no row of the table, as the tool, and shows the problem.
        sch = write_schematic(libs["board"], 20260306)
        (libs["board"] / "sym-lib-table").write_text("(sym_lib_table (lib", encoding="utf-8")
        result = add(sch)
        assert "✅" in result, result
        assert "**Library table problems (KiCad shows them too):** The library table" in result
        assert description(sch) == "global"


class TestRefusal:
    @pytest.mark.parametrize(
        "rows, message",
        [
            ((), 'no symbol library "Device" is in the global library table of KiCad 10.0.6'),
            ((row("Device", "{dir}/Device.kicad_sym", "(disabled)"),), ": disabled."),
            ((row("Device", "{dir}/Device.kicad_sym", kind="Legacy"),), 'has the type "Legacy"'),
            # A disabled row gives its state first, as in list_libraries.
            ((row("Device", "{dir}/old.lib", "(disabled)", kind="Legacy"),), ": disabled."),
            ((row("Device", "{dir}/Gone.kicad_sym"),), ": missing."),
            ((row("Device", "${NO_SUCH_VAR}/x.kicad_sym"),), "variable with no value"),
        ],
    )
    def test_library_that_the_tool_cannot_use(self, libs, rows, message):
        # The folder as KiCad writes it in the quoted URI (a Windows backslash escaped).
        rows = tuple(r.replace("{dir}", quote(str(libs["dir"]))[1:-1]) for r in rows)
        libs["table"].write_text(table(*rows), encoding="utf-8")
        sch = write_schematic(libs["board"], 20260306)
        before = sch.read_text(encoding="utf-8")
        result = add(sch)
        assert result.startswith("❌ Cannot add component Device:R: "), result
        assert message in result
        assert sch.read_text(encoding="utf-8") == before

    def test_no_global_table(self, libs):
        libs["table"].unlink()
        result = add(write_schematic(libs["board"], 20260306))
        assert result.startswith('❌ Cannot add component Device:R: no symbol library "Device"')
        assert "The global library table" in result and "does not exist" in result

    def test_symbol_not_in_the_library(self, libs):
        sch = write_schematic(libs["board"], 20260306)
        result = add(sch, symbol="C")
        assert result == (
            f'❌ Cannot add component Device:C: the library "Device" '
            f'({libs["dir"] / "Device.kicad_sym"}) has no symbol "C".'
        )

    def test_no_kicad_cli(self, libs, monkeypatch):
        def no_version():
            return None

        monkeypatch.setattr(schematic_editor, "kicad_cli_version_sync", no_version)
        result = add(write_schematic(libs["board"], 20260306))
        assert result.startswith("❌ Cannot add component Device:R: KiCad is not installed")
        assert "Tell the user: install KiCad 8 or later" in result


class TestPins:
    def test_pins_of_the_symbol(self, libs):
        (libs["dir"] / "Device.kicad_sym").write_text(
            library(resistor("three", pins=3)), encoding="utf-8"
        )
        sch = write_schematic(libs["board"], 20260306)
        result = add(sch)
        assert "**Pins:** 3 pins detected" in result
        placed = [
            node
            for node in parse(sch.read_text(encoding="utf-8")).children("symbol")
            if node.first("lib_id") is not None
        ]
        assert [pin.value(0) for pin in placed[0].children("pin")] == ["1", "2", "3"]

    def test_symbol_with_no_pins_gets_no_pins(self, libs):
        (libs["dir"] / "Device.kicad_sym").write_text(
            library('(symbol "Logo" (property "Reference" "#G") (symbol "Logo_0_1" (circle)))'),
            encoding="utf-8",
        )
        sch = write_schematic(libs["board"], 20260306)
        result = add(sch, symbol="Logo")
        assert "**Pins:** 0 pins detected" in result
        assert "(pin " not in sch.read_text(encoding="utf-8")
