"""Tests for the placed symbol of add_component_from_library, as KiCad places a symbol."""

import asyncio
import json
import uuid as uuidlib
from pathlib import Path

import pytest

from kicad_workbench.parsers.sexpr import Node, parse, property_field
from kicad_workbench.tools import schematic_editor

from .installed_kicad import write_schematic
from .symbol_tables import row, use_global_table

LIBRARY = """(kicad_symbol_lib (version 20251024) (generator "test")
(symbol "R"
	(exclude_from_sim yes)
	(in_bom no)
	(on_board yes)
	(property "Reference" "R" (at 2.032 1.5 90) (effects (font (size 1.27 1.27))))
	(property "Value" "R" (at 0 -2 90) (effects (font (size 1.27 1.27))))
	(property "Footprint" "Resistor_SMD:R_0603" (at -1.778 0 90)
		(effects (font (size 1.27 1.27)) (hide yes)))
	(property "Datasheet" "https://example.com/r.pdf" (at 0 0 0)
		(effects (font (size 1.27 1.27)) (hide yes)))
	(property "Description" "Resistor" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
	(property "MPN" "RC0603" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
	(property "ki_keywords" "R res" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
	(symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27) (number "1"))
		(pin passive line (at 0 -3.81 90) (length 1.27) (number "2")))
	(symbol "R_2_1" (pin passive line (at 0 3.81 270) (length 1.27) (number "3")))
)
(symbol "Plain"
	(property "Reference" "P")
	(property "Value" "Plain")
	(in_pos_files no)
	(symbol "Plain_1_1" (pin passive line (number "1")))
)
)
"""


@pytest.fixture
def lib(tmp_path, monkeypatch):
    path = tmp_path / "Device.kicad_sym"
    path.write_text(LIBRARY, encoding="utf-8")
    use_global_table(tmp_path, monkeypatch, row("Device", path))


def add(path: Path, symbol: str = "R", **kwargs) -> str:
    # The symbol at the position of the call: the field positions are relative to it
    # (test_snap_to_grid.py tests the connection grid).
    values = {"reference": "R1", "value": "1k", "x": 100, "y": 50, "snap_to_grid": False} | kwargs
    return asyncio.run(
        schematic_editor.add_component_from_library(str(path), "Device", symbol, **values)
    )


def placed(path: Path) -> list[Node]:
    return [
        node
        for node in parse(path.read_text(encoding="utf-8")).children("symbol")
        if node.first("lib_id") is not None
    ]


def fields(node: Node) -> dict[str, tuple[str, list]]:
    """Name -> (text, at) of the properties of a placed symbol."""
    result = {}
    for prop in node.children("property"):
        name, text = property_field(prop)
        at = prop.first("at")
        result[name] = (text, [at.value(i) for i in range(3)])
    return result


def flags(node: Node) -> dict[str, str]:
    heads = ("exclude_from_sim", "in_bom", "on_board", "in_pos_files", "dnp")
    return {head: node.first(head).value(0) for head in heads if node.first(head) is not None}


def instances(node: Node) -> list[tuple[str, str, str, str]]:
    result = []
    for project in node.first("instances").children("project"):
        for path in project.children("path"):
            result.append(
                (
                    project.value(0),
                    path.value(0),
                    path.first("reference").value(0),
                    path.first("unit").value(0),
                )
            )
    return result


def file_uuid(path: Path) -> str:
    return parse(path.read_text(encoding="utf-8")).first("uuid").value(0)


class TestFields:
    def test_fields_of_the_library_at_their_library_positions(self, tmp_path, lib):
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch)
        got = fields(placed(sch)[0])
        # The position of the symbol plus the position in the library, with
        # the Y axis of the sheet down.
        assert got == {
            "Reference": ("R1", ["102.032", "48.5", "90"]),
            "Value": ("1k", ["100", "52", "90"]),
            "Footprint": ("Resistor_SMD:R_0603", ["98.222", "50", "90"]),
            "Datasheet": ("https://example.com/r.pdf", ["100", "50", "0"]),
            "Description": ("Resistor", ["100", "50", "0"]),
            "MPN": ("RC0603", ["100", "50", "0"]),
        }

    def test_effects_of_the_library(self, tmp_path, lib):
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch)
        props = {p.value(0): p for p in placed(sch)[0].children("property")}
        assert props["Footprint"].first("effects").first("hide").value(0) == "yes"
        assert props["Reference"].first("effects").first("hide") is None

    def test_footprint_of_the_call(self, tmp_path, lib):
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch, footprint="Resistor_SMD:R_0805")
        assert fields(placed(sch)[0])["Footprint"][0] == "Resistor_SMD:R_0805"

    def test_mandatory_field_that_the_library_does_not_have(self, tmp_path, lib):
        # The default field of LIB_SYMBOL: at the symbol position, Footprint,
        # Datasheet and Description hidden.
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch, "Plain", footprint="Resistor_SMD:R_0603")
        node = placed(sch)[0]
        assert fields(node)["Footprint"] == ("Resistor_SMD:R_0603", ["100", "50", "0"])
        assert fields(node)["Datasheet"] == ("", ["100", "50", "0"])
        props = {p.value(0): p for p in node.children("property")}
        assert props["Footprint"].first("effects").first("hide").value(0) == "yes"
        assert props["Value"].first("effects") is None


class TestFlags:
    def test_flags_of_the_library(self, tmp_path, lib):
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch)
        assert flags(placed(sch)[0]) == {
            "exclude_from_sim": "yes",
            "in_bom": "no",
            "on_board": "yes",
            "dnp": "no",
        }

    def test_defaults_and_in_pos_files(self, tmp_path, lib):
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch, "Plain")
        assert flags(placed(sch)[0]) == {
            "exclude_from_sim": "no",
            "in_bom": "yes",
            "on_board": "yes",
            "in_pos_files": "no",
            "dnp": "no",
        }


class TestUnit:
    def test_unit_of_the_symbol(self, tmp_path, lib):
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add(sch, unit=2)
        node = placed(sch)[0]
        assert node.first("unit").value(0) == "2"
        assert instances(node)[0][3] == "2"
        # KiCad gives a placed symbol the pins of all units.
        assert [pin.value(0) for pin in node.children("pin")] == ["1", "2", "3"]

    @pytest.mark.parametrize("unit", [0, 3, -1])
    def test_unit_that_the_symbol_does_not_have(self, tmp_path, lib, unit):
        sch = write_schematic(tmp_path, 20260306)
        before = sch.read_text(encoding="utf-8")
        result = add(sch, unit=unit)
        assert result == (
            f"❌ Cannot add component Device:R: the symbol has 2 units, thus the unit must be "
            f"from 1 to 2, not {unit}."
        )
        assert sch.read_text(encoding="utf-8") == before


def sheet(file_name: str, name: str, sheet_uuid: str) -> str:
    return (
        f'\t(sheet (at 10 10) (size 20 10) (uuid "{sheet_uuid}")\n'
        f'\t\t(property "Sheetname" "{name}" (at 10 9 0))\n'
        f'\t\t(property "Sheetfile" "{file_name}" (at 10 21 0))\n\t)\n'
    )


def schematic(path: Path, *sheets: str) -> Path:
    text = (
        f'(kicad_sch (version 20260306) (generator "eeschema")\n\t(uuid "{uuidlib.uuid4()}")\n'
        f"\t(lib_symbols)\n{''.join(sheets)})\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestInstances:
    def test_root_schematic_of_a_project(self, tmp_path, lib):
        root = schematic(tmp_path / "board.kicad_sch")
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        assert "✅" in add(root)
        assert instances(placed(root)[0]) == [("board", f"/{file_uuid(root)}", "R1", "1")]

    def test_schematic_with_no_project(self, tmp_path, lib):
        # The schematic editor opens it in a project of its name.
        sch = schematic(tmp_path / "alone.kicad_sch")
        result = add(sch)
        assert "✅" in result and "The project file alone.kicad_pro does not exist" in result, (
            result
        )
        assert instances(placed(sch)[0]) == [("alone", f"/{file_uuid(sch)}", "R1", "1")]

    def test_shared_sheet_of_a_root_with_no_project_file(self, tmp_path, lib):
        # The schematic editor has the root open alone: a lock of its project only.
        schematic(
            tmp_path / "alone.kicad_sch",
            sheet("amp.kicad_sch", "Left", "a1"),
            sheet("amp.kicad_sch", "Right", "a2"),
        )
        (tmp_path / "~alone.kicad_pro.lck").write_text("{}", encoding="utf-8")
        amp = schematic(tmp_path / "amp.kicad_sch")
        result = add(amp, sheet_path="/Left/")
        assert "✅" in result, result
        assert "The project file alone.kicad_pro does not exist" in result
        assert "The hierarchy uses this schematic 2 times" in result

    def test_sub_sheet(self, tmp_path, lib):
        root = schematic(tmp_path / "board.kicad_sch", sheet("sub/power.kicad_sch", "Power", "a1"))
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        sub = schematic(tmp_path / "sub" / "power.kicad_sch")
        result = add(sub)
        assert "✅" in result and "**Sheet:** /Power/" in result, result
        assert instances(placed(sub)[0]) == [("board", f"/{file_uuid(root)}/a1", "R1", "1")]

    @pytest.fixture
    def shared(self, tmp_path):
        root = schematic(
            tmp_path / "board.kicad_sch",
            sheet("amp.kicad_sch", "Left", "a1"),
            sheet("amp.kicad_sch", "Right", "a2"),
        )
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        return root, schematic(tmp_path / "amp.kicad_sch")

    def test_shared_sheet_needs_the_sheet_path(self, lib, shared):
        root, amp = shared
        before = amp.read_text(encoding="utf-8")
        result = add(amp)
        top = file_uuid(root)
        assert result == (
            "❌ Cannot add component Device:R: the hierarchy uses this schematic 2 times. Give "
            f"sheet_path, the sheet that gets the reference: /Left/ (/{top}/a1), "
            f"/Right/ (/{top}/a2)."
        )
        assert amp.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("wanted", ["/Right/", "/Right", "Right", "UUID", "UUID/"])
    def test_shared_sheet_with_the_sheet_path(self, lib, shared, wanted):
        root, amp = shared
        if wanted.startswith("UUID"):
            wanted = wanted.replace("UUID", f"/{file_uuid(root)}/a2")
        result = add(amp, sheet_path=wanted)
        assert "✅" in result, result
        assert "**Position:** (100, 50)" in result
        assert instances(placed(amp)[0]) == [("board", f"/{file_uuid(root)}/a2", "R1", "1")]
        assert "Only /Right/ has the reference R1" in result

    def test_sheet_path_with_a_fixed_path_next_to_a_sheet_with_no_uuid(self, tmp_path, lib):
        root = schematic(
            tmp_path / "board.kicad_sch",
            sheet("amp.kicad_sch", "Left", "a1"),
            sheet("amp.kicad_sch", "Right", "a2").replace('(uuid "a2")', ""),
        )
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        amp = schematic(tmp_path / "amp.kicad_sch")
        result = add(amp)
        assert "/Right/ (no fixed UUID path)" in result, result
        assert "✅" in add(amp, sheet_path="/Left/")
        assert instances(placed(amp)[0]) == [("board", f"/{file_uuid(root)}/a1", "R1", "1")]
        result = add(amp, reference="R2", sheet_path="/Right/")
        assert result.startswith(
            "❌ Cannot add component Device:R: KiCad has no fixed sheet path for the sheet /Right/"
        ), result

    def test_sheet_path_that_is_not_a_path_of_the_schematic(self, lib, shared):
        _, amp = shared
        result = add(amp, sheet_path="/Middle/")
        assert result.startswith(
            "❌ Cannot add component Device:R: '/Middle/' is not a sheet path of this schematic."
        ), result

    def test_top_level_sheets_of_kicad_10(self, tmp_path, lib):
        # The path of a top-level sheet is the UUID of its entry in the project.
        root = schematic(tmp_path / "board.kicad_sch")
        second = schematic(tmp_path / "second.kicad_sch")
        entries = [
            {"filename": "board.kicad_sch", "name": "Root", "uuid": file_uuid(root)},
            {"filename": "second.kicad_sch", "name": "Second", "uuid": "ccc7"},
        ]
        (tmp_path / "board.kicad_pro").write_text(
            json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8"
        )
        result = add(second)
        assert "✅" in result and "**Sheet:** /Second/" in result, result
        assert instances(placed(second)[0]) == [("board", "/ccc7", "R1", "1")]

    def test_schematic_with_no_uuid(self, tmp_path, lib):
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20260306) (generator "eeschema"))\n', encoding="utf-8")
        result = add(sch)
        assert result == (
            "❌ Cannot add component Device:R: KiCad has no fixed sheet path for the sheet / "
            f"of this schematic. {sch} has no UUID."
        )


@pytest.mark.parametrize(
    "value, text",
    [(100, "100"), (2.54, "2.54"), (-0.00001, "0"), (1 / 3, "0.3333"), (-1.5, "-1.5")],
)
def test_lengths_as_kicad_writes_them(value, text):
    assert schematic_editor._mm(value) == text
