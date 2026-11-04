"""Tests of the junctions that add_wire and add_component_from_library add, as KiCad does."""

import pytest

from kicad_workbench.parsers.sexpr import parse

from .test_snap_to_grid import add, make_schematic, wire, wire_points


@pytest.fixture
def sch(tmp_path, monkeypatch):
    return make_schematic(tmp_path, monkeypatch)


def junctions(path) -> list[str]:
    root = parse(path.read_text(encoding="utf-8"))
    return [f"{j.first('at').value(0)} {j.first('at').value(1)}" for j in root.children("junction")]


def test_t_gets_a_junction_and_the_wire_splits(sch):
    assert wire(sch, [(10.16, 10.16), (30.48, 10.16)]).startswith("✅")
    result = wire(sch, [(20.32, 10.16), (20.32, 20.32)])
    assert "**Junctions added** (as the schematic editor of KiCad adds them): (20.32, 10.16)." in (
        result
    ), result
    assert junctions(sch) == ["20.32 10.16"]
    assert sorted(wire_points(sch)) == [
        ["10.16 10.16", "20.32 10.16"],
        ["20.32 10.16", "20.32 20.32"],
        ["20.32 10.16", "30.48 10.16"],
    ]


def test_wire_through_a_resistor_does_not_short_it(sch):
    assert add(sch, x=101.6, y=101.6).startswith("✅")  # pins at (101.6, 97.79) and (101.6, 105.41)
    result = wire(sch, [(101.6, 91.44), (101.6, 111.76)])
    assert junctions(sch) == []
    assert sorted(wire_points(sch)) == [
        ["101.6 105.41", "101.6 111.76"],
        ["101.6 91.44", "101.6 97.79"],
    ], result


def test_power_symbol_on_a_wire(sch):
    # The hidden pin of GND is in the box of its body: KiCad finds it.
    assert wire(sch, [(10.16, 10.16), (30.48, 10.16)]).startswith("✅")
    result = add(sch, "GND", x=20.32, y=10.16, reference="#PWR01")
    assert result.startswith("✅"), result
    assert "**Junctions added** (as the schematic editor of KiCad adds them): (20.32, 10.16)." in (
        result
    )
    assert junctions(sch) == ["20.32 10.16"]


def test_resistor_on_a_wire(sch):
    assert wire(sch, [(101.6, 91.44), (101.6, 111.76)]).startswith("✅")
    result = add(sch, x=101.6, y=101.6)
    assert junctions(sch) == []
    assert "**Wires of the sheet**" in result, result
    assert sorted(wire_points(sch)) == [
        ["101.6 105.41", "101.6 111.76"],
        ["101.6 91.44", "101.6 97.79"],
    ]


def test_same_wire_again_is_no_change(sch):
    assert wire(sch, [(10.16, 10.16), (30.48, 10.16)]).startswith("✅")
    before = sch.read_text(encoding="utf-8")
    result = wire(sch, [(10.16, 10.16), (30.48, 10.16)])
    assert result.startswith("✅ No change: the sheet has this wire already"), result
    assert sch.read_text(encoding="utf-8") == before


def test_wire_that_continues_a_wire_merges(sch):
    assert wire(sch, [(10.16, 10.16), (20.32, 10.16)]).startswith("✅")
    result = wire(sch, [(20.32, 10.16), (30.48, 10.16)])
    assert "The wire is merged with a wire of the sheet" in result, result
    assert wire_points(sch) == [["10.16 10.16", "30.48 10.16"]]


def group_members(path) -> list[str]:
    group = parse(path.read_text(encoding="utf-8")).first("group")
    return [atom.value for atom in group.first("members").atoms()]


def test_piece_of_a_split_wire_joins_its_group(sch):
    content = sch.read_text(encoding="utf-8").rstrip()[:-1]
    sch.write_text(
        content
        + "\t(wire (pts (xy 10.16 10.16) (xy 30.48 10.16)) (stroke (width 0) (type default))"
        ' (uuid "00000000-0000-4000-8000-0000000000a1"))\n'
        '\t(group "G" (uuid "00000000-0000-4000-8000-0000000000a2")'
        ' (members "00000000-0000-4000-8000-0000000000a1"))\n)\n',
        encoding="utf-8",
    )
    assert wire(sch, [(20.32, 10.16), (20.32, 20.32)]).startswith("✅")
    root = parse(sch.read_text(encoding="utf-8"))
    pieces = [
        w.first("uuid").value(0)
        for w in root.children("wire")
        if w.first("pts").children("xy")[0].value(1) == "10.16"
        and w.first("pts").children("xy")[1].value(1) == "10.16"
    ]
    assert sorted(group_members(sch)) == sorted(pieces)
    assert len(pieces) == 2


def test_pin_in_the_middle_of_a_wire_moves_along_the_wire(sch):
    # Pin 1 of R is at (x, y - 3.81). The grid move keeps it on the horizontal wire.
    assert wire(sch, [(0, 45.72), (101.6, 45.72)], snap_to_grid=False).startswith("✅")
    result = add(sch, x=50, y=49.53)
    assert "**Position:** (49.53, 49.53), moved to the connection grid" in result, result
    assert "**Junctions added**" in result


def test_wire_inside_a_wire_is_no_change(sch):
    assert wire(sch, [(10.16, 10.16), (30.48, 10.16)]).startswith("✅")
    before = sch.read_text(encoding="utf-8")
    result = wire(sch, [(15.24, 10.16), (25.4, 10.16)])
    assert result.startswith("✅ No change"), result
    assert sch.read_text(encoding="utf-8") == before


def test_move_onto_a_junction_is_a_new_connection(sch):
    assert wire(sch, [(20.32, 0), (20.32, 20.32)]).startswith("✅")
    assert wire(sch, [(20.32, 10.16), (30.48, 10.16)]).startswith("✅")  # a T: a junction
    assert junctions(sch) == ["20.32 10.16"]
    # The grid move puts the middle of the slanted wire on the junction.
    result = wire(sch, [(12.7, 2.6), (27.94, 17.8)])
    assert "thus the wire connects there: (20.32, 10.16)." in result, result


def test_wire_that_takes_in_a_wire_merges(sch):
    # The new wire is the first line of the pair (the lower X): CleanUp keeps it.
    assert wire(sch, [(15.24, 10.16), (20.32, 10.16)]).startswith("✅")
    result = wire(sch, [(10.16, 10.16), (30.48, 10.16)])
    assert "The wire is merged with a wire of the sheet" in result, result
    assert wire_points(sch) == [["10.16 10.16", "30.48 10.16"]]
