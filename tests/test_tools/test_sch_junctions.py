"""Tests of sch_junctions: the junctions of the schematic editor of KiCad 8, 9 and 10."""

import pytest

from kicad_workbench.parsers.bus_names import is_bus_label
from kicad_workbench.parsers.sch_junctions import (
    Entry,
    Label,
    Line,
    Sheet,
    SymbolPins,
    UnknownJunctionError,
    finish_wire,
    junction_needed,
    merge_overlap,
    place_symbol,
    simplify_wire_list,
)

STROKE = (0, "solid", (0.0, 0.0, 0.0, 0.0))
G = 12700  # 50 mils


def line(x1, y1, x2, y2, bus=False, new=False, uid="") -> Line:
    return Line((x1 * G, y1 * G), (x2 * G, y2 * G), bus, STROKE, uid or f"{x1}{y1}{x2}{y2}", new)


def sheet(lines=(), symbols=(), entries=(), labels=(), junctions=()) -> Sheet:
    return Sheet(
        list(lines), list(junctions), list(entries), list(symbols), [], list(labels), [], []
    )


def pins(*points, hidden=(), nc=(), other_unit=()) -> SymbolPins:
    """A symbol with pins of unit 1. hidden: hidden pins outside the box of the body.
    other_unit: pins that only an other unit (of an other instance of the sheet) has."""
    pts = [(x * G, y * G) for x, y in points]
    hidden_pts = {(x * G, y * G) for x, y in hidden}
    nc_pts = {(x * G, y * G) for x, y in nc}
    other = [(x * G, y * G) for x, y in other_unit]
    connected = set(pts) - nc_pts - hidden_pts
    units = {1: pts, 2: other} if other else {1: pts}
    uncertain = (hidden_pts - nc_pts) | (set(other) - connected) if other else hidden_pts - nc_pts
    if other:
        connected -= {q for q in connected if q not in other}
        uncertain |= set(pts) - nc_pts - connected
    return SymbolPins(units, connected, uncertain, hidden_pts)


def p(x, y):
    return (x * G, y * G)


KICADS = (8, 9, 10)


@pytest.mark.parametrize("kicad", KICADS)
class TestDecision:
    def test_t_needs_a_junction(self, kicad):
        s = sheet([line(0, 0, 10, 0), line(5, 0, 5, 5)])
        assert junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_two_wire_ends_need_no_junction(self, kicad):
        s = sheet([line(0, 0, 5, 0), line(5, 0, 5, 5)])
        assert not junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_three_wire_ends_need_a_junction(self, kicad):
        s = sheet([line(0, 0, 5, 0), line(5, 0, 5, 5), line(5, 0, 10, 0)])
        # KiCad 10 merges the two collinear ends, but keeps a stub of 3 end angles.
        assert junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_crossing_needs_no_junction(self, kicad):
        s = sheet([line(0, 0, 10, 0), line(5, -5, 5, 5)])
        assert not junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_pin_in_the_middle_of_a_wire(self, kicad):
        s = sheet([line(0, 0, 10, 0)], [pins((5, 0))])
        assert junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_no_connect_pin_in_the_middle_of_a_wire(self, kicad):
        s = sheet([line(0, 0, 10, 0)], [pins((5, 0), nc=[(5, 0)])])
        assert not junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_existing_junction(self, kicad):
        s = sheet([line(0, 0, 10, 0), line(5, 0, 5, 5)], junctions=[p(5, 0)])
        assert not junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_hidden_pin_that_decides(self, kicad):
        s = sheet([line(0, 0, 10, 0)], [pins((5, 0), hidden=[(5, 0)])])
        with pytest.raises(UnknownJunctionError, match="the pin is hidden"):
            junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_pin_of_an_other_unit_that_decides(self, kicad):
        s = sheet([line(0, 0, 10, 0)], [pins((2, 2), other_unit=[(5, 0)])])
        with pytest.raises(UnknownJunctionError, match="other unit"):
            junction_needed(s, p(5, 0), kicad, is_bus_label)

    def test_hidden_pin_that_does_not_decide(self, kicad):
        # The T needs the junction with or without the pin.
        s = sheet([line(0, 0, 10, 0), line(5, 0, 5, 5)], [pins((5, 0), hidden=[(5, 0)])])
        assert junction_needed(s, p(5, 0), kicad, is_bus_label)


def test_split_wire_at_a_crossing_differs_in_kicad_10():
    # Case 4 of the spec: the existing wire is two collinear pieces that meet at the crossing.
    lines = [line(0, 0, 5, 0), line(5, 0, 10, 0), line(5, -5, 5, 5)]
    assert junction_needed(sheet(lines), p(5, 0), 8, is_bus_label)
    assert junction_needed(sheet(lines), p(5, 0), 9, is_bus_label)
    assert not junction_needed(sheet(lines), p(5, 0), 10, is_bus_label)


def test_label_at_a_crossing_in_kicad_10():
    # Case 8: a local label sets breakLines in KiCad 10 only.
    lines = [line(0, 0, 10, 0), line(5, -5, 5, 5)]
    labels = [Label("label", p(5, 0), "SIG")]
    assert not junction_needed(sheet(lines, labels=labels), p(5, 0), 9, is_bus_label)
    assert junction_needed(sheet(lines, labels=labels), p(5, 0), 10, is_bus_label)


def test_bus_entry_blocks_in_kicad_8():
    # Case 9: two wires end at the wire end of a bus entry.
    entry = Entry(p(5, 5), p(5, 0), 0)
    lines = [line(0, 0, 5, 0), line(5, 0, 10, 0)]
    assert not junction_needed(sheet(lines, entries=[entry]), p(5, 0), 8, is_bus_label)
    assert junction_needed(sheet(lines, entries=[entry]), p(5, 0), 9, is_bus_label)


def test_merge_overlap():
    merged = merge_overlap(line(5, 0, 15, 0), line(0, 0, 10, 0, uid="keep"))
    assert (merged.start, merged.end, merged.uuid) == (p(0, 0), p(15, 0), "keep")
    assert merge_overlap(line(0, 0, 5, 0), line(6, 0, 10, 0)) is None
    assert merge_overlap(line(0, 0, 5, 0), line(5, 0, 5, 5)) is None


def test_simplify_wire_list_merges_collinear_segments():
    segments = [
        line(0, 0, 5, 0, new=True),
        line(5, 0, 10, 0, new=True),
        line(10, 0, 10, 5, new=True),
    ]
    result = simplify_wire_list(segments)
    assert [(s.start, s.end) for s in result] == [(p(0, 0), p(10, 0)), (p(10, 0), p(10, 5))]


@pytest.mark.parametrize("kicad", KICADS)
class TestFlows:
    def test_new_wire_to_the_middle_of_a_wire(self, kicad):
        s = sheet([line(0, 0, 10, 0, uid="old")])
        finish_wire(s, [line(5, 0, 5, 5, new=True, uid="new")], kicad, is_bus_label)
        assert s.added_junctions == [p(5, 0)]
        ends = sorted((ln.start, ln.end) for ln in s.lines)
        assert ends == [(p(0, 0), p(5, 0)), (p(5, 0), p(5, 5)), (p(5, 0), p(10, 0))]
        old = next(ln for ln in s.lines if ln.uuid == "old")
        assert (old.start, old.end) == (p(0, 0), p(5, 0))  # BreakAt: the original keeps [start, p]

    def test_new_wire_through_two_pins(self, kicad):
        # Case 6: TrimWire removes the part between the pins, no junction.
        s = sheet(symbols=[pins((4, 0), (6, 0))])
        finish_wire(s, [line(0, 0, 10, 0, new=True)], kicad, is_bus_label)
        assert s.added_junctions == []
        assert sorted((ln.start, ln.end) for ln in s.lines) == [
            (p(0, 0), p(4, 0)),
            (p(6, 0), p(10, 0)),
        ]

    def test_new_wire_continues_a_wire(self, kicad):
        # CleanUp merges a new wire with a collinear wire that it touches.
        s = sheet([line(0, 0, 5, 0, uid="old")])
        finish_wire(s, [line(5, 0, 10, 0, new=True)], kicad, is_bus_label)
        assert [(ln.start, ln.end, ln.uuid) for ln in s.lines] == [(p(0, 0), p(10, 0), "old")]

    def test_symbol_with_two_pins_on_a_wire(self, kicad):
        # Case 12: TrimOverLappingWires, no junction.
        s = sheet([line(0, 0, 10, 0, uid="old")])
        place_symbol(s, pins((4, 0), (6, 0)), kicad, is_bus_label)
        assert s.added_junctions == []
        assert sorted((ln.start, ln.end) for ln in s.lines) == [
            (p(0, 0), p(4, 0)),
            (p(6, 0), p(10, 0)),
        ]

    def test_symbol_with_one_pin_in_the_middle_of_a_wire(self, kicad):
        # Case 11: a junction, and the wire splits there.
        s = sheet([line(0, 0, 10, 0)])
        place_symbol(s, pins((5, 0), (5, 4)), kicad, is_bus_label)
        assert s.added_junctions == [p(5, 0)]
        assert len(s.lines) == 2


@pytest.mark.parametrize("kicad, pieces", [(8, 2), (9, 2), (10, 1)])
def test_new_wire_through_a_junction(kicad, pieces):
    # CleanUp step a of KiCad 8 and 9 (BreakSegmentsOnJunctions) splits the new wire.
    s = sheet([line(0, 0, 10, 0), line(5, 0, 5, 5)], junctions=[p(5, 0)])
    finish_wire(s, [line(0, -5, 10, 5, new=True)], kicad, is_bus_label)
    assert len([ln for ln in s.lines if ln.new]) == pieces


def test_duplicate_wire_goes():
    s = sheet([line(0, 0, 10, 0, uid="old")])
    finish_wire(s, [line(0, 0, 10, 0, new=True)], 10, is_bus_label)
    assert [(ln.uuid, ln.new) for ln in s.lines] == [("old", False)]


@pytest.mark.parametrize("kicad", KICADS)
def test_old_lines_that_cleanup_merges_at_a_decision(kicad):
    # The example of the spec (section 6.1): KiCad merges A and B before the decision.
    s = sheet([line(0, 0, 20, 0, uid="A"), line(10, 0, 30, 0, uid="B")])
    with pytest.raises(UnknownJunctionError, match="CleanUp"):
        finish_wire(s, [line(20, -10, 20, 10, new=True)], kicad, is_bus_label)


@pytest.mark.parametrize("kicad", KICADS)
def test_old_wire_through_two_pins_at_a_decision(kicad):
    s = sheet([line(0, 0, 10, 0, uid="old")], symbols=[pins((4, 0), (6, 0))])
    with pytest.raises(UnknownJunctionError, match="TrimWire"):
        finish_wire(s, [line(2, 0, 2, 5, new=True)], kicad, is_bus_label)


@pytest.mark.parametrize("kicad", KICADS)
def test_old_lines_away_from_the_decision(kicad):
    s = sheet([line(0, 0, 20, 0), line(10, 0, 30, 0)])
    finish_wire(s, [line(50, 0, 50, 10, new=True)], kicad, is_bus_label)
    assert [ln.new for ln in s.lines].count(True) == 1


@pytest.mark.parametrize("kicad", KICADS)
def test_symbol_cleanup_removes_a_junction_that_is_not_explicit(kicad):
    # A junction where two collinear wires meet at (4, 0). The new symbol cuts the wire
    # between its pins at (4, 0) and (6, 0): (4, 0) has a wire end and a pin, and the
    # CleanUp of the commit removes the junction.
    s = sheet([line(0, 0, 4, 0), line(4, 0, 10, 0)], junctions=[p(4, 0)])
    place_symbol(s, pins((4, 0), (6, 0)), kicad, is_bus_label)
    assert s.junctions == []
    assert sorted((ln.start, ln.end) for ln in s.lines) == [(p(0, 0), p(4, 0)), (p(6, 0), p(10, 0))]


@pytest.mark.parametrize("kicad", KICADS)
def test_stacked_pins_count_two_times(kicad):
    # GetConnectionPoints keeps the two pins at (2, 0): three points on the wire, no trim.
    s = sheet([line(0, 0, 10, 0)])
    symbol = SymbolPins({1: [p(2, 0), p(2, 0), p(8, 0)]}, {p(2, 0), p(8, 0)}, set())
    place_symbol(s, symbol, kicad, is_bus_label)
    # The wire between the pins stays; the pins in the middle of the wire get junctions.
    assert sorted((ln.start, ln.end) for ln in s.lines) == [
        (p(0, 0), p(2, 0)),
        (p(2, 0), p(8, 0)),
        (p(8, 0), p(10, 0)),
    ]
    assert s.added_junctions == [p(2, 0), p(8, 0)]


@pytest.mark.parametrize("kicad", KICADS)
def test_two_pins_at_one_point_trim_nothing(kicad):
    s = sheet([line(0, 0, 2, 0, uid="old")], symbols=[pins((2, 0), (2, 0))])
    finish_wire(s, [line(1, 0, 1, 5, new=True)], kicad, is_bus_label)
    assert s.added_junctions == [p(1, 0)]


@pytest.mark.parametrize("kicad", KICADS)
def test_hidden_pins_on_a_bus_need_no_box(kicad):
    s = sheet([line(0, 0, 10, 0, bus=True)])
    place_symbol(s, pins((2, 0), (8, 0), hidden=[(2, 0), (8, 0)]), kicad, is_bus_label)
