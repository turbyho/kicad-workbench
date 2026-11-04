"""Tests of snap_to_grid: the editing tools put pins, wire ends and labels on the connection grid."""

import asyncio
import json
from pathlib import Path

import pytest

from kicad_workbench.parsers.sch_geometry import (
    definitions,
    iu,
    on_grid,
    placed_pins,
    placed_symbols,
)
from kicad_workbench.parsers.sch_junctions import read_sheet
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import schematic_editor
from kicad_workbench.utils.sch_hierarchy import AmbiguousRootError

from .installed_kicad import write_schematic
from .symbol_tables import row, use_global_table

LIBRARY = """(kicad_symbol_lib (version 20251024) (generator "test")
(symbol "R"
\t(property "Reference" "R") (property "Value" "R")
\t(symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27) (number "1"))
\t\t(pin passive line (at 0 -3.81 90) (length 1.27) (number "2")))
)
(symbol "Shift"
\t(property "Reference" "S") (property "Value" "Shift")
\t(symbol "Shift_1_1" (pin passive line (at 0.5 3.81 270) (length 1.27) (number "1"))
\t\t(pin passive line (at 0.5 -3.81 90) (length 1.27) (number "2")))
)
(symbol "Odd"
\t(property "Reference" "O") (property "Value" "Odd")
\t(symbol "Odd_1_1" (pin passive line (at 0 1 270) (length 1) (number "1"))
\t\t(pin passive line (at 0 -1 90) (length 1) (number "2")))
)
(symbol "NC"
\t(property "Reference" "N") (property "Value" "NC")
\t(symbol "NC_1_1" (pin passive line (at 0 3.81 270) (length 1.27) (number "1"))
\t\t(pin no_connect line (at 0.3 0 0) (length 1) (number "2")))
)
(symbol "Skew"
\t(property "Reference" "K") (property "Value" "Skew")
\t(symbol "Skew_1_1" (pin passive line (at 0.5 1 270) (length 1) (number "1"))
\t\t(pin passive line (at 0.5 -1 90) (length 1) (number "2")))
)
(symbol "GND"
\t(power) (property "Reference" "#PWR") (property "Value" "GND")
\t(symbol "GND_0_1" (polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54))))
\t(symbol "GND_1_1" (pin power_in line (at 0 0 270) (length 0) hide (number "1")))
)
(symbol "Logo"
\t(property "Reference" "G") (property "Value" "Logo")
)
)
"""


def make_schematic(tmp_path: Path, monkeypatch) -> Path:
    """A schematic of KiCad 10 and the test library Device in the global table."""
    path = tmp_path / "Device.kicad_sym"
    path.write_text(LIBRARY, encoding="utf-8")
    use_global_table(tmp_path, monkeypatch, row("Device", path))
    return write_schematic(tmp_path, 20260306)


@pytest.fixture
def sch(tmp_path, monkeypatch) -> Path:
    return make_schematic(tmp_path, monkeypatch)


def set_project(sch: Path, data) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    sch.with_suffix(".kicad_pro").write_text(text, encoding="utf-8")


def add(sch: Path, symbol: str = "R", x: float = 100, y: float = 50, **kwargs) -> str:
    return asyncio.run(
        schematic_editor.add_component_from_library(
            str(sch), "Device", symbol, kwargs.pop("reference", "R1"), symbol, "", x, y, **kwargs
        )
    )


def symbol_at(sch: Path) -> list[str]:
    """The (at ...) values of the last placed symbol."""
    symbols = list(placed_symbols(parse(sch.read_text(encoding="utf-8"))))
    at = symbols[-1].first("at")
    return [at.value(i) for i in range(3)]


def off_grid_pins(sch: Path, grid: int = 12700) -> list[str]:
    root = parse(sch.read_text(encoding="utf-8"))
    defs = definitions(root)
    pins = []
    for symbol in placed_symbols(root):
        for pin, position in placed_pins(symbol, defs[symbol.first("lib_id").value(0)]):
            if not on_grid(position, grid):
                pins.append(pin.number)
    return pins


class TestSymbol:
    def test_symbol_moves_to_the_grid(self, sch):
        result = add(sch)
        assert (
            "**Position:** (100.33, 49.53), moved to the connection grid of 1.27 mm from (100, 50)"
            in result
        ), result
        assert "Warning" not in result
        assert symbol_at(sch) == ["100.33", "49.53", "0"]
        assert off_grid_pins(sch) == []

    def test_position_on_the_grid_does_not_change(self, sch):
        result = add(sch, x=101.6, y=50.8)
        assert "**Position:** (101.6, 50.8)\n" in result, result

    def test_pins_off_the_symbol_position_go_on_the_grid(self, sch):
        result = add(sch, "Shift")
        assert "**Position:** (99.83, 49.53), moved to the connection grid" in result, result
        assert off_grid_pins(sch) == []

    def test_pins_that_cannot_all_be_on_the_grid(self, sch):
        result = add(sch, "Odd")
        assert (
            "**Position:** (100.33, 49.53), moved to the connection grid of 1.27 mm from (100, 50), "
            "the pins of the symbol cannot all be on the connection grid" in result
        ), result
        assert (
            "**Warning:** these points are not on the connection grid of 1.27 mm: pin 1 at "
            "(100.33, 48.53), pin 2 at (100.33, 50.53). ERC of the project gives "
            "endpoint_off_grid (warning)." in result
        ), result

    def test_no_connect_pins_do_not_decide(self, sch):
        result = add(sch, "NC")
        assert "**Position:** (100.33, 49.53)" in result, result
        assert (
            "**Warning:** these pins of the type no_connect are not on the connection grid of "
            "1.27 mm: pin 2 at (100.63, 49.53). ERC of KiCad 8 gives endpoint_off_grid (warning). "
            "KiCad 9 and later do not check these pins." in result
        ), result
        assert "these points are not on the connection grid" not in result
        assert off_grid_pins(sch) == ["2"]

    def test_symbol_with_no_pins(self, sch):
        result = add(sch, "Logo")
        assert "**Position:** (100.33, 49.53), moved to the connection grid" in result, result

    def test_no_snap(self, sch):
        result = add(sch, snap_to_grid=False)
        assert "**Position:** (100, 50)\n" in result, result
        assert (
            "**Warning:** these points are not on the connection grid of 1.27 mm: pin 1 at "
            "(100, 46.19), pin 2 at (100, 53.81)." in result
        )
        assert symbol_at(sch) == ["100", "50", "0"]

    def test_no_warning_when_the_project_ignores_the_check(self, sch):
        set_project(sch, {"erc": {"rule_severities": {"endpoint_off_grid": "ignore"}}})
        result = add(sch, snap_to_grid=False)
        assert result.startswith("✅"), result
        assert "Warning" not in result

    def test_severity_of_the_project(self, sch):
        set_project(sch, {"erc": {"rule_severities": {"endpoint_off_grid": "error"}}})
        assert "endpoint_off_grid (error)" in add(sch, snap_to_grid=False)

    def test_connection_grid_of_the_project(self, sch):
        set_project(sch, {"schematic": {"connection_grid_size": 100.0}})
        result = add(sch)
        assert "(99.06, 49.53), moved to the connection grid of 2.54 mm" in result, result
        assert off_grid_pins(sch, 25400) == []

    def test_pin_on_a_connection_point_stays(self, sch):
        wire = asyncio.run(
            schematic_editor.add_wire(str(sch), [(100, 46.19), (90, 46.19)], snap_to_grid=False)
        )
        assert wire.startswith("✅"), wire
        result = add(sch)
        assert (
            "**Position:** (100, 50), not moved: the move to the connection grid takes a pin "
            "away from a connection point of the sheet" in result
        ), result
        assert "pin 1 at (100, 46.19), pin 2 at (100, 53.81)" in result

    def test_axes_are_independent(self, sch, monkeypatch):
        # All pins have the X offset 0.5 mm, the Y offsets have different remainders.
        result = add(sch, "Skew")
        assert "**Position:** (99.83, 49.53)" in result, result
        assert off_grid_pins(sch) == ["1", "2"]
        assert "pin 1 at (100.33, 48.53), pin 2 at (100.33, 50.53)" in result

    def test_position_that_is_not_a_number(self, sch):
        assert add(sch, x=float("nan")) == "❌ The position (nan, 50) is not a position in mm."


def wire(sch: Path, points, **kwargs) -> str:
    return asyncio.run(schematic_editor.add_wire(str(sch), points, **kwargs))


def wire_points(sch: Path) -> list[list[str]]:
    root = parse(sch.read_text(encoding="utf-8"))
    return [
        [f"{xy.value(0)} {xy.value(1)}" for xy in line.first("pts").children("xy")]
        for line in root.children("wire")
    ]


class TestWire:
    def test_points_move_to_the_grid(self, sch):
        result = wire(sch, [(10, 10), (20, 10), (20, 20.3)])
        assert wire_points(sch) == [["10.16 10.16", "20.32 10.16"], ["20.32 10.16", "20.32 20.32"]]
        assert (
            "Points moved to the connection grid of 1.27 mm: (10, 10) to (10.16, 10.16), "
            "(20, 10) to (20.32, 10.16), (20, 20.3) to (20.32, 20.32)." in result
        ), result
        assert "Warning" not in result

    def test_point_on_a_pin_stays(self, sch):
        assert add(sch, snap_to_grid=False).startswith("✅")
        # The vertical segment stays vertical: the second point gets the X of the pin.
        result = wire(sch, [(100, 46.19), (100, 40), (110, 40)])
        assert wire_points(sch) == [["100 46.19", "100 39.37"], ["100 39.37", "110.49 39.37"]]
        assert "(100, 40) to (100, 39.37), (110, 40) to (110.49, 39.37)" in result, result
        assert (
            "these points are not on the connection grid of 1.27 mm: (100, 46.19), (100, 39.37)."
            in result
        ), result

    def test_point_in_the_middle_of_a_wire_moves_along_the_wire(self, sch):
        # A wire end in the middle of a wire connects through a junction, which the
        # tool adds. The point moves along the horizontal wire: Y 10 stays.
        assert wire(sch, [(0, 10), (20, 10)], snap_to_grid=False).startswith("✅")
        result = wire(sch, [(5, 10), (5, 20)])
        assert ["5.08 10", "5.08 20.32"] in wire_points(sch)
        assert "**Junctions added** (as the schematic editor of KiCad adds them): (5.08, 10)." in (
            result
        ), result

    def test_point_on_a_junction_stays(self, sch):
        content = sch.read_text(encoding="utf-8").rstrip()[:-1]
        junction = '\t(junction (at 5 10) (diameter 0) (color 0 0 0 0) (uuid "j"))\n)\n'
        sch.write_text(content + junction, encoding="utf-8")
        wire(sch, [(5, 10), (5, 20)])
        assert wire_points(sch)[-1] == ["5 10", "5 20.32"]

    def test_segment_that_the_move_puts_on_a_label(self, sch):
        assert asyncio.run(
            schematic_editor.add_label(str(sch), "A", 15, 10.16, snap_to_grid=False)
        ).startswith("✅")
        result = wire(sch, [(10, 10), (20, 10)])
        assert wire_points(sch)[-1] == ["10.16 10.16", "20.32 10.16"]
        assert "thus the wire connects there: (15, 10.16)." in result, result

    def test_point_that_the_move_connects(self, sch):
        assert add(sch, x=101.6, y=101.6).startswith("✅")  # pin 1 at (101.6, 97.79)
        result = wire(sch, [(101.9, 97.5), (110, 97.5)])
        assert wire_points(sch)[-1] == ["101.6 97.79", "110.49 97.79"]
        assert (
            "**Note:** after the move to the connection grid these points are on a connection "
            "point of the sheet, thus the wire connects there: (101.6, 97.79)." in result
        ), result

    def test_wire_end_on_a_bus_moves(self, sch):
        # A wire connects to a bus only through a bus entry.
        content = sch.read_text(encoding="utf-8").rstrip()[:-1]
        bus = '\t(bus (pts (xy 0 10) (xy 20 10)) (uuid "b"))\n)\n'
        sch.write_text(content + bus, encoding="utf-8")
        wire(sch, [(5, 10), (5, 20)])
        assert wire_points(sch)[-1] == ["5.08 10.16", "5.08 20.32"]

    def test_segment_of_length_0_after_the_move(self, sch):
        result = wire(sch, [(10, 10), (10.3, 10.3)])
        assert result == (
            "❌ The wire has a segment of length 0 at (10.16, 10.16) after the move to the "
            "connection grid. Remove one of the two points."
        )
        assert wire_points(sch) == []

    def test_no_snap(self, sch):
        result = wire(sch, [(10, 10), (20, 10)], snap_to_grid=False)
        assert wire_points(sch) == [["10 10", "20 10"]]
        assert (
            "**Warning:** these points are not on the connection grid of 1.27 mm: (10, 10), "
            "(20, 10). ERC of the project gives endpoint_off_grid (warning)." in result
        ), result

    def test_no_warning_when_the_project_ignores_the_check(self, sch):
        set_project(sch, {"erc": {"rule_severities": {"endpoint_off_grid": "ignore"}}})
        assert "Warning" not in wire(sch, [(10, 10), (20, 10)], snap_to_grid=False)

    def test_pin_on_a_connection_point_does_not_move_to_an_other_point(self, sch):
        # Pin 1 at (100, 46.19) is on a wire end. The grid position of the symbol puts
        # pin 1 on the other end of a wire, (100.33, 45.72): the symbol must not move.
        assert wire(sch, [(100, 46.19), (90, 46.19)], snap_to_grid=False).startswith("✅")
        assert wire(sch, [(100.33, 45.72), (110.49, 45.72)]).startswith("✅")
        result = add(sch)
        assert "**Position:** (100, 50), not moved" in result, result

    def test_symbol_pin_that_the_move_connects(self, sch):
        assert wire(sch, [(100.33, 45.72), (90.17, 45.72)]).startswith("✅")
        result = add(sch)  # R1 moves to (100.33, 49.53): pin 1 at (100.33, 45.72)
        assert (
            "after the move to the connection grid these pins are on a connection point of the "
            "sheet, thus they connect there: pin 1 at (100.33, 45.72)." in result
        ), result

    def test_sheet_that_kicad_cannot_read(self, sch):
        assert add(sch, x=101.6, y=101.6).startswith("✅")
        text = sch.read_text(encoding="utf-8")
        sch.write_text(text.replace("(at 101.6 101.6 0)", "(at 101.6 101.6 45)"), encoding="utf-8")
        assert wire(sch, [(10, 10), (20, 10)]) == (
            "❌ Cannot add the wire: the sheet has an item that KiCad cannot read: "
            "the symbol angle 45 is not 0, 90, 180 or 270."
        )
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 10))
        assert result.startswith("❌ Cannot add the label: the sheet has an item that KiCad")

    def test_project_file_that_is_not_json(self, sch):
        # KiCad uses the default settings (JSON_SETTINGS::LoadFromFile), the tool tells it.
        set_project(sch, "{ not json")
        result = wire(sch, [(10, 10), (20, 10)])
        assert "moved to the connection grid of 1.27 mm" in result, result
        assert "**Note:** The project file" in result
        assert "thus KiCad uses the default connection grid and ERC severities." in result


def label_at(sch: Path, head: str = "label") -> list[str]:
    at = parse(sch.read_text(encoding="utf-8")).children(head)[-1].first("at")
    return [at.value(i) for i in range(3)]


class TestLabel:
    def test_label_moves_to_the_grid(self, sch):
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 10))
        assert result == (
            "✅ Label 'A' added at (10.16, 10.16), moved to the connection grid of 1.27 mm "
            "from (10, 10)"
        )
        assert label_at(sch) == ["10.16", "10.16", "0"]

    def test_label_on_a_bus_stays(self, sch):
        content = sch.read_text(encoding="utf-8").rstrip()[:-1]
        bus = '\t(bus (pts (xy 0 10) (xy 20 10)) (uuid "b"))\n)\n'
        sch.write_text(content + bus, encoding="utf-8")
        result = asyncio.run(schematic_editor.add_label(str(sch), "D[0..3]", 5, 10))
        assert result == (
            "✅ Label 'D[0..3]' added at (5.08, 10), moved to the connection grid of 1.27 mm "
            "from (5, 10)"
        )

    def test_label_on_a_horizontal_wire_moves_along_the_wire(self, sch):
        assert wire(sch, [(0, 10), (20, 10)], snap_to_grid=False).startswith("✅")
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 5, 10))
        assert result == (
            "✅ Label 'A' added at (5.08, 10), moved to the connection grid of 1.27 mm from (5, 10)"
        )

    def test_label_does_not_move_off_its_wire(self, sch):
        # The nearest grid X, 5.08, is after the end of the wire at 4.98.
        assert wire(sch, [(0, 10), (4.98, 10)], snap_to_grid=False).startswith("✅")
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 4.95, 10))
        assert result == "✅ Label 'A' added at (4.95, 10)"

    def test_label_on_a_slanted_wire_stays(self, sch):
        assert wire(sch, [(0, 0), (20, 10)], snap_to_grid=False).startswith("✅")
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 5))
        assert result == "✅ Label 'A' added at (10, 5)"

    def test_label_that_the_move_connects(self, sch):
        assert wire(sch, [(10.16, 10.16), (20.32, 10.16)]).startswith("✅")
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 10))
        assert result.endswith(", where it connects to an item of the sheet"), result

    def test_no_snap(self, sch):
        asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 10, snap_to_grid=False))
        assert label_at(sch) == ["10", "10", "0"]

    @pytest.mark.parametrize("tool", ["add_global_label", "add_hierarchical_label"])
    def test_label_on_a_pin_is_on_the_pin(self, sch, tool):
        assert add(sch, snap_to_grid=False).startswith("✅")
        result = asyncio.run(getattr(schematic_editor, tool)(str(sch), "R1", "1", "SIG"))
        assert "anchored on R1.1 at (100, 46.19) angle 90°" in result, result
        head = "global_label" if tool == "add_global_label" else "hierarchical_label"
        assert label_at(sch, head) == ["100", "46.19", "90"]

    @pytest.mark.parametrize("tool", ["add_global_label", "add_hierarchical_label"])
    def test_label_at_explicit_coordinates(self, sch, tool):
        result = asyncio.run(
            getattr(schematic_editor, tool)(str(sch), "", "", "SIG", "input", 55.5, 66.6)
        )
        assert "at (55.88, 66.04), moved to the connection grid of 1.27 mm from (55.5, 66.6)" in (
            result
        ), result

    def test_position_that_is_not_a_number(self, sch):
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", float("inf"), 1))
        assert result == "❌ The position (inf, 1) is not a position in mm."


def test_projects_with_the_same_settings(sch, monkeypatch):
    other = sch.parent / "other"
    other.mkdir()
    for root in (sch, other / "main.kicad_sch"):
        root.with_suffix(".kicad_pro").write_text(
            json.dumps({"schematic": {"connection_grid_size": 100.0}}), encoding="utf-8"
        )

    def project_file(path):
        raise AmbiguousRootError(path, [sch, other / "main.kicad_sch"])

    monkeypatch.setattr(schematic_editor, "schematic_project_file", project_file)
    result = wire(sch, [(10, 10), (20, 10)])
    assert "Points moved to the connection grid of 2.54 mm" in result, result


class TestUnknownProject:
    """A schematic in two projects with different grids: its connection grid is not known."""

    @pytest.fixture(autouse=True)
    def ambiguous(self, sch, monkeypatch):
        roots = [sch.parent / "a.kicad_sch", sch.parent / "b.kicad_sch"]
        for root, mils in zip(roots, (50, 100), strict=True):
            root.with_suffix(".kicad_pro").write_text(
                json.dumps({"schematic": {"connection_grid_size": mils}}), encoding="utf-8"
            )

        def project_file(path):
            raise AmbiguousRootError(path, roots)

        monkeypatch.setattr(schematic_editor, "schematic_project_file", project_file)

    def test_wire_with_snap_is_refused(self, sch):
        result = wire(sch, [(10, 10), (20, 10)])
        assert result.startswith("❌ Cannot add the wire: ")
        assert result.endswith(
            "thus the connection grid of the schematic is not known. With snap_to_grid=False "
            "the tool writes the positions as they are."
        )
        assert wire_points(sch) == []

    def test_wire_without_snap(self, sch):
        result = wire(sch, [(10, 10), (20, 10)], snap_to_grid=False)
        assert result.startswith("✅ Wire added"), result
        assert "The tool cannot tell the points off the grid." in result
        assert wire_points(sch) == [["10 10", "20 10"]]

    def test_label_without_snap(self, sch):
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 10, snap_to_grid=False))
        assert result == "✅ Label 'A' added at (10, 10)"

    def test_label_with_snap_is_refused(self, sch):
        result = asyncio.run(schematic_editor.add_label(str(sch), "A", 10, 10))
        assert result.startswith("❌ Cannot add the label: ")


def test_label_on_a_pin_of_a_second_unit(sch):
    """Each unit of a part is a symbol with the same reference."""
    content = sch.read_text(encoding="utf-8").rstrip()[:-1].replace("\t(lib_symbols)\n", "")
    sch.write_text(content + MULTI_UNIT + ")\n", encoding="utf-8")
    result = asyncio.run(schematic_editor.add_global_label(str(sch), "U1", "7", "OUT"))
    assert "anchored on U1.7 at (60.96, 50.8)" in result, result


MULTI_UNIT = """\t(lib_symbols (symbol "Lib:U"
\t\t(symbol "U_1_1" (pin output line (at 7.62 0 180) (length 2.54) (number "1")))
\t\t(symbol "U_2_1" (pin output line (at 7.62 0 180) (length 2.54) (number "7")))))
\t(symbol (lib_id "Lib:U") (at 25.4 25.4 0) (unit 1) (uuid "a")
\t\t(property "Reference" "U1" (at 0 0 0)) (property))
\t(symbol (lib_id "Lib:U") (at 53.34 50.8 0) (unit 2) (uuid "b")
\t\t(property "Reference" "U1" (at 0 0 0)))
"""


def test_connection_points_after_the_tools(sch):
    """The positions that the tools write are the positions that sch_geometry reads."""
    add(sch)
    wire(sch, [(100.33, 45.72), (110, 45.72)])
    points = read_sheet(parse(sch.read_text(encoding="utf-8"))).connection_points()
    assert (iu(100.33), iu(45.72)) in points
    assert (iu(110.49), iu(45.72)) in points
