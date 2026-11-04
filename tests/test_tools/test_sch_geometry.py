"""Tests of sch_geometry: positions in IU and the connection points of a sheet."""

import pytest

from kicad_workbench.parsers.lib_symbols import LibraryPin
from kicad_workbench.parsers.sch_geometry import (
    Placement,
    align,
    body_style,
    iu,
    on_grid,
    placed_pins,
    placed_units,
    placement,
)
from kicad_workbench.parsers.sch_junctions import read_sheet
from kicad_workbench.parsers.sexpr import parse

GRID = 12700  # 50 mils


@pytest.mark.parametrize(
    "mm, value",
    [(1.27, 12700), (46.19, 461900), (0.00005, 1), (-0.00005, -1), (0.000049, 0), (-2.54, -25400)],
)
def test_iu_rounds_as_kicad(mm, value):
    assert iu(mm) == value


@pytest.mark.parametrize(
    "value, offset, aligned",
    [
        (500000, 0, 495300),  # 50 mm: 39.37 steps of 1.27 mm
        (6350, 0, 12700),  # half a step: away from zero
        (-6350, 0, -12700),
        (-6349, 0, 0),
        (6349, 0, 0),
        (12700, 0, 12700),
        (500000, 6350, 501650),
        (-19050, 0, -25400),  # -1.5 steps
    ],
)
def test_align_rounds_half_away_from_zero(value, offset, aligned):
    assert align(value, GRID, offset) == aligned
    assert (aligned - offset) % GRID == 0


def test_on_grid():
    assert on_grid((12700, -25400), GRID)
    assert not on_grid((12700, 1), GRID)


PIN = LibraryPin("1", "passive", 7.62, 2.54)


@pytest.mark.parametrize(
    "angle, mirror, position",
    [
        # The transform of KiCad 8.0.9, 9.0.9 and 10.0.6 for a pin at (7.62, 2.54)
        # of the library (spike/kicad-versions/grid_check.py compares it with ERC).
        (0, "", (76200, -25400)),
        (90, "", (-25400, -76200)),
        (180, "", (-76200, 25400)),
        (270, "", (25400, 76200)),
        (0, "x", (76200, 25400)),
        (0, "y", (-76200, -25400)),
        (90, "x", (-25400, 76200)),
        (90, "y", (25400, -76200)),
        (270, "y", (-25400, 76200)),
    ],
)
def test_pin_position_in_the_sheet(angle, mirror, position):
    assert Placement(0, 0, angle, mirror).sheet_position(PIN) == position
    x, y = position
    assert Placement(1000, 2000, angle, mirror).sheet_position(PIN) == (x + 1000, y + 2000)


DEFINITION = """(symbol "Lib:U"
\t(symbol "U_0_1" (pin power_in line (at 0 5.08 270) (length 2.54) (number "8")))
\t(symbol "U_1_1" (pin input line (at -7.62 2.54 0) (length 2.54) (number "1")))
\t(symbol "U_1_2" (pin input line (at -7.62 0 0) (length 2.54) (number "1")))
\t(symbol "U_2_1" (pin output line (at 7.62 0 180) (length 2.54) (number "7")))
)"""


def symbol(extra: str) -> str:
    return f'(symbol (lib_id "Lib:U") (at 10.16 20.32 0) {extra} (uuid "u"))'


class TestPlacedSymbol:
    def test_placement(self):
        node = parse('(symbol (lib_id "Lib:U") (at 1.27 -2.54 90) (mirror y) (unit 1))')
        assert placement(node) == Placement(12700, -25400, 90, "y")
        assert placement(parse('(symbol (lib_id "Lib:U"))')) == Placement(0, 0, 0, "")

    def test_units_of_the_instances(self):
        node = parse(
            symbol(
                '(unit 1) (instances (project "p" (path "/a" (reference "U1") (unit 1)) '
                '(path "/b" (reference "U2") (unit 2))))'
            )
        )
        assert placed_units(node) == {1, 2}

    @pytest.mark.parametrize(
        "extra, style", [("", 1), ("(convert 2)", 2), ("(body_style 2)", 2), ("(unit 1)", 1)]
    )
    def test_body_style(self, extra, style):
        assert body_style(parse(symbol(extra))) == style

    def test_pins_of_the_unit_and_the_body_style(self):
        definition = parse(DEFINITION)
        pins = placed_pins(parse(symbol("(unit 1)")), definition, {1})
        assert sorted((pin.number, position) for pin, position in pins) == [
            ("1", (25400, 177800)),
            ("8", (101600, 152400)),
        ]
        # Pin 8 is in the body style 1 of all units.
        pins = placed_pins(parse(symbol("(unit 1) (convert 2)")), definition, {1})
        assert [(pin.number, position) for pin, position in pins] == [("1", (25400, 203200))]
        pins = placed_pins(parse(symbol("(unit 2)")), definition)
        assert sorted(pin.number for pin, _position in pins) == ["7", "8"]


SHEET = f"""(kicad_sch
\t(lib_symbols {DEFINITION})
\t{symbol("(unit 2)")}
\t(wire (pts (xy 0 0) (xy 10.16 0)) (uuid "w"))
\t(bus (pts (xy 0 50.8) (xy 0 60.96)) (uuid "b"))
\t(bus_entry (at 2.54 2.54) (size 2.54 -2.54) (uuid "e"))
\t(junction (at 1.1 1.1) (uuid "j"))
\t(no_connect (at 3.3 3.3) (uuid "n"))
\t(label "A" (at 4.4 4.4 0) (uuid "l"))
\t(global_label "B" (shape input) (at 5.5 5.5 0) (uuid "g"))
\t(hierarchical_label "C" (shape input) (at 6.6 6.6 0) (uuid "h"))
\t(netclass_flag "" (length 2.54) (shape round) (at 7.7 7.7 0) (uuid "d"))
\t(sheet (at 100 100) (size 10 10)
\t\t(pin "P" input (at 100 103.3 180) (uuid "p")))
\t(text "T" (at 8.8 8.8 0))
)"""


class TestConnectionPoints:
    def test_points_of_each_item(self):
        points = read_sheet(parse(SHEET)).connection_points(label=True)
        for mm in [
            (17.78, 20.32),  # pin 7 of unit 2
            (10.16, 15.24),  # pin 8 of unit 0
            (0, 0),
            (10.16, 0),
            (0, 60.96),
            (2.54, 2.54),
            (5.08, 0),  # the end of the bus entry
            (1.1, 1.1),
            (3.3, 3.3),
            (4.4, 4.4),
            (5.5, 5.5),
            (6.6, 6.6),
            (7.7, 7.7),
            (100, 103.3),
        ]:
            assert (iu(mm[0]), iu(mm[1])) in points, mm

    def test_point_on_a_segment_of_a_wire_or_a_bus(self):
        points = read_sheet(parse(SHEET)).connection_points(label=True)
        assert (iu(5), 0) in points
        assert (0, iu(55.5)) in points
        assert (iu(5), 1) not in points
        assert (iu(10.17), 0) not in points

    def test_wire_does_not_connect_to_a_bus(self):
        points = read_sheet(parse(SHEET)).connection_points()
        assert (0, iu(60.96)) not in points
        assert (0, iu(55.5)) not in points
        assert (iu(5.08), 0) in points  # the end of the bus entry

    def test_middle_of_a_wire_connects_through_a_junction(self):
        # The editing tools add the junction (sch_junctions).
        points = read_sheet(parse(SHEET)).connection_points()
        assert (iu(5), 0) in points
        assert (iu(5), 0) not in points.points

    def test_definition_of_the_lib_name(self):
        sheet = SHEET.replace(
            '(symbol (lib_id "Lib:U")', '(symbol (lib_name "U_1") (lib_id "Lib:U")'
        )
        sheet = sheet.replace('(lib_symbols (symbol "Lib:U"', '(lib_symbols (symbol "U_1"')
        assert (iu(17.78), iu(20.32)) in read_sheet(parse(sheet)).connection_points()

    def test_points_that_are_not_connection_points(self):
        points = read_sheet(parse(SHEET)).connection_points()
        assert (iu(8.8), iu(8.8)) not in points  # a text
        assert (iu(2.54), iu(17.78)) not in points  # pin 1 of unit 1: not placed
        assert (iu(100), iu(100)) not in points  # the sheet symbol


@pytest.mark.parametrize(
    "text, message",
    [
        ('(symbol (lib_id "L:U") (at 0 0 45))', "the symbol angle 45 is not 0, 90, 180 or 270"),
        ('(symbol (lib_id "L:U") (at 0 0 -90))', "the symbol angle -90 is not 0, 90, 180 or 270"),
        ('(symbol (lib_id "L:U") (at 0 0 0) (mirror z))', "the symbol mirror 'z' is not x or y"),
        ('(symbol (lib_id "L:U") (at 1e999 0 0))', "'1e999' is not a number that KiCad reads"),
    ],
)
def test_placement_that_kicad_cannot_read(text, message):
    with pytest.raises(ValueError, match=message):
        placement(parse(text))
