"""Tests of the angle of a label on a pin (pin_label_angle) and of the orientation of a
placed symbol (symbol_orientation).

The rules come from KiCad 10.0.6: orientLabel (eeschema/tools/backannotate.cpp),
SCH_SYMBOL::SetOrientation and GetOrientation, and the transform of
parseSchematicSymbol.
"""

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.parsers.lib_symbols import LibraryPin, library_pins
from kicad_workbench.parsers.sch_geometry import (
    SYM_MIRROR_X,
    SYM_MIRROR_Y,
    SYM_ORIENT_0,
    SYM_ORIENT_90,
    SYM_ORIENT_180,
    SYM_ORIENT_270,
    Placement,
    symbol_orientation,
)
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools.schematic_editor import pin_label_angle

ORIENTATIONS = [(angle, mirror) for angle in (0, 90, 180, 270) for mirror in ("", "x", "y")]


def pin(x: float, y: float, angle: int | None) -> LibraryPin:
    return LibraryPin("1", "passive", x, y, False, angle)


@pytest.mark.parametrize(
    "angle, mirror, expected",
    [
        (0, "", SYM_ORIENT_0),
        (90, "", SYM_ORIENT_90),
        (180, "", SYM_ORIENT_180),
        (270, "", SYM_ORIENT_270),
        (0, "x", SYM_MIRROR_X + SYM_ORIENT_0),
        # GetOrientation: the transform of (mirror y) is found at SYM_MIRROR_Y + SYM_ORIENT_0.
        (0, "y", SYM_MIRROR_Y + SYM_ORIENT_0),
    ],
)
def test_symbol_orientation(angle, mirror, expected):
    assert symbol_orientation(Placement(0, 0, angle, mirror)) == expected


def test_each_placement_has_an_orientation_of_the_list():
    # GetOrientation finds each transform of the parser (no SYM_NORMAL fallback).
    for angle, mirror in ORIENTATIONS:
        assert symbol_orientation(Placement(0, 0, angle, mirror)) != 0, (angle, mirror)


def test_a_side_pin_of_a_tall_symbol_points_away_from_the_body():
    # A pin on the left side of a long connector, far above the origin of the symbol:
    # the pin goes to the right into the body (PIN_RIGHT), thus the label goes left.
    left = pin(-5.08, 20.32, 0)
    assert pin_label_angle(left, Placement(0, 0)) == 180
    # Rotated by 90 degrees (CCW): the left side is at the bottom, the label goes down.
    assert pin_label_angle(left, Placement(0, 0, 90)) == 270
    # Mirrored around Y: the pin is on the right side, the label goes right.
    assert pin_label_angle(left, Placement(0, 0, 0, "y")) == 0


def test_the_label_points_away_from_the_body_in_each_orientation():
    # orientLabel gives the direction from the body to the connection point of the pin
    # in the sheet (Placement.sheet_position) for each orientation and pin direction.
    for angle, mirror in ORIENTATIONS:
        where = Placement(0, 0, angle, mirror)
        for pin_angle, (dx, dy) in ((0, (1, 0)), (90, (0, 1)), (180, (-1, 0)), (270, (0, -1))):
            end = where.sheet_position(pin(0, 0, pin_angle))
            inner = where.sheet_position(pin(dx * 2.54, dy * 2.54, pin_angle))
            vx, vy = end[0] - inner[0], end[1] - inner[1]
            away = 0 if vx > 0 else 180 if vx < 0 else (90 if vy < 0 else 270)
            assert pin_label_angle(pin(0, 0, pin_angle), where) == away, (
                angle,
                mirror,
                pin_angle,
            )


def test_an_angle_that_kicad_cannot_read():
    with pytest.raises(InputError, match="has the angle 45, not 0, 90, 180 or 270"):
        pin_label_angle(pin(0, 0, 45), Placement(0, 0))
    with pytest.raises(InputError, match="has no angle"):
        pin_label_angle(pin(0, 0, None), Placement(0, 0))


@pytest.mark.parametrize(
    "at, angle",
    [
        ("(at 0 0 90)", 90),
        ("(at 0 0 90.0)", 90),  # NeedNUMBER, then (int)strtol
        ("(at 0 0 9e1)", 9),  # strtol stops at "e": KiCad refuses 9
        ("(at 0 0)", None),  # NeedNUMBER refuses the end of the list
        ("", 0),  # no (at ...): PIN_RIGHT of the constructor
    ],
)
def test_the_pin_angle_as_parse_symbol_pin_reads_it(at, angle):
    definition = parse('(symbol "A:B" (symbol "B_1_1" (pin passive line ' + at + ' (number "1"))))')
    assert [p.angle for p in library_pins(definition, 1, {1})] == [angle]


def test_an_angle_that_is_no_number():
    definition = parse('(symbol "A:B" (symbol "B_1_1" (pin passive line (at 0 0 x) (number "1"))))')
    with pytest.raises(InputError):
        library_pins(definition, 1, {1})
