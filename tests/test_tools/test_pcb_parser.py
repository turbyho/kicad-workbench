"""Tests for the text parser of KiCad boards (parsers/pcb_parser.py)."""

import math

import pytest

from kicad_workbench.parsers.kicad_versions import KicadFileError
from kicad_workbench.parsers.pcb_parser import PCBParser, _field
from kicad_workbench.parsers.sexpr import SExprError, parse

# A board in the format of KiCad 9: the nets are codes of the net table.
KICAD9 = """(kicad_pcb
\t(version 20241229)
\t(general (thickness 1.2) (legacy_teardrops no))
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" signal)
\t\t(6 "In2.Cu" power)
\t\t(2 "B.Cu" signal)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(1 "F.Mask" user)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "/SDA \\"bus\\"")
\t(footprint "R:R_0603" (layer "B.Cu") (uuid "a")
\t\t(at -12.5 -3.25 180)
\t\t(property "Reference" "R1" (at 0 0 0))
\t\t(property "Value" "4k7" (at 0 0 0))
\t\t(pad "1" smd rect (at -1 0) (layers "B.Cu"))
\t\t(pad "2" smd rect (at 1 0) (layers "B.Cu"))
\t)
\t(segment (start -5.08 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "b"))
\t(segment (start 0 0) (end 3 4) (width 0.2) (layer "B.Cu") (net 2) (uuid "c"))
\t(via (at -3 4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "d"))
\t(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "e"))
\t(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid "f")
\t\t(keepout (tracks not_allowed) (vias not_allowed)))
)
"""

# A board in the format of KiCad 10: the nets are names. A via has more
# items between (layers ...) and (net ...).
KICAD10 = """(kicad_pcb
\t(version 20260206)
\t(general (thickness 1.6))
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal))
\t(footprint "C:C_0402" (layer "F.Cu") (uuid "a")
\t\t(at 10 20)
\t\t(property "Reference" "C1" (at 0 0 0))
\t\t(property "Value" "100n" (at 0 0 0))
\t)
\t(segment (start 1 2) (end 1 -2) (width 0.2) (layer "F.Cu") (net "GND") (uuid "b"))
\t(via blind (at 5 -6) (size 0.45) (drill 0.2) (layers "F.Cu" "In1.Cu")
\t\t(capping no) (covering (front no) (back no)) (net "+3V3") (uuid "c"))
\t(zone (net "GND") (layer "In1.Cu") (uuid "d"))
)
"""

# A board in the format of KiCad 6 and 7: fields are (fp_text ...).
KICAD6 = """(kicad_pcb (version 20211014)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "D:LED" (layer "F.Cu") (at 1 2)
    (fp_text reference "D1" (at 0 0)) (fp_text value "red" (at 0 0)))
)
"""


def parse_board(tmp_path, text):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return PCBParser(str(path))._parse_file()


class TestKicad9:
    def test_general(self, tmp_path):
        general = parse_board(tmp_path, KICAD9)["general"]
        # F.SilkS and F.Mask start with "F." but are not copper.
        assert general == {"thickness": 1.2, "layers": 4}

    def test_footprint_with_negative_position(self, tmp_path):
        (footprint,) = parse_board(tmp_path, KICAD9)["footprints"]
        assert footprint["footprint_id"] == "R:R_0603"
        assert footprint["reference"] == "R1" and footprint["value"] == "4k7"
        assert footprint["layer"] == "B.Cu"
        assert footprint["at"] == {"x": -12.5, "y": -3.25, "rotation": 180.0}
        assert footprint["pad_count"] == 2

    def test_tracks_get_the_net_names_of_the_codes(self, tmp_path):
        tracks = parse_board(tmp_path, KICAD9)["tracks"]
        # Upstream issue #26: the start at x = -5.08 dropped the segment.
        assert [track["net"] for track in tracks] == ["GND", '/SDA "bus"']
        assert tracks[0]["start"] == {"x": -5.08, "y": 20.0}
        assert tracks[1]["length"] == pytest.approx(5.0)
        assert tracks[1]["width"] == 0.2 and tracks[1]["layer"] == "B.Cu"

    def test_via(self, tmp_path):
        (via,) = parse_board(tmp_path, KICAD9)["vias"]
        assert via == {
            "at": {"x": -3.0, "y": 4.0},
            "size": 0.6,
            "drill": 0.3,
            "top_layer": "F.Cu",
            "bottom_layer": "B.Cu",
            "net": "GND",
        }

    def test_keepout_is_not_a_copper_zone(self, tmp_path):
        zones = parse_board(tmp_path, KICAD9)["zones"]
        assert zones == [{"net_name": "GND", "layers": ["B.Cu"]}]


class TestKicad10:
    def test_nets_are_names(self, tmp_path):
        data = parse_board(tmp_path, KICAD10)
        assert [track["net"] for track in data["tracks"]] == ["GND"]
        # In1.Cu is not a copper layer of this board (F.Cu and B.Cu only).
        assert data["zones"] == [{"net_name": "GND", "layers": []}]

    def test_via_with_more_items_and_a_type(self, tmp_path):
        (via,) = parse_board(tmp_path, KICAD10)["vias"]
        assert via["net"] == "+3V3"
        assert via["at"] == {"x": 5.0, "y": -6.0}
        assert (via["top_layer"], via["bottom_layer"]) == ("F.Cu", "In1.Cu")

    def test_footprint_with_no_rotation(self, tmp_path):
        (footprint,) = parse_board(tmp_path, KICAD10)["footprints"]
        assert footprint["at"] == {"x": 10.0, "y": 20.0, "rotation": 0.0}
        assert footprint["pad_count"] == 0


# Items that the KiCad 9 and 10 boards above do not have.
MORE = """(kicad_pcb (version 20260206)
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (footprint "M:Mount" (layer "F.Cu") (at 0 0)
    (zone (net "GND") (layer "F.Cu") (uuid "z1")))
  (segment (start 0 0) (end 0 1) (width 0.2) (layer "F.Cu") (net GND) (uuid "s"))
  (arc (start 1 0) (mid 0.7071067811865476 0.7071067811865476) (end 0 1)
    (width 0.2) (layer "F.Cu") (net "GND") (uuid "a"))
  (zone (layer "B.Cu") (uuid "z2"))
)
"""


class TestMoreItems:
    def test_arc_is_a_track_with_its_arc_length(self, tmp_path):
        tracks = parse_board(tmp_path, MORE)["tracks"]
        assert [track["length"] for track in tracks] == [1.0, pytest.approx(math.pi / 2)]
        assert tracks[1]["start"] == {"x": 1.0, "y": 0.0}

    def test_net_name_with_no_quotes(self, tmp_path):
        tracks = parse_board(tmp_path, MORE)["tracks"]
        assert tracks[0]["net"] == "GND"

    def test_zones_of_the_board(self, tmp_path):
        # As in pcbnew: a zone in a footprint is not a board zone, and a
        # copper zone with no net is a zone.
        assert parse_board(tmp_path, MORE)["zones"] == [{"net_name": "", "layers": ["B.Cu"]}]


def _zone(head: str, extra: str = "") -> str:
    return f"  (zone {head} {extra} (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))\n"


# The zone layers as KiCad 10 reads them (spike/kicad-versions/zone_layers_check.py
# compares the readers with the save of KiCad 8, 9 and 10).
ZONES = (
    """(kicad_pcb (version 20260206)
  (layers
    (0 "F.Cu" signal "Top")
    (4 "Mid1" power)
    (6 "In2.Cu" signal "GND_PLANE")
    (2 "B.Cu" signal)
    (5 "F.SilkS" user "F.Silkscreen")
    (7 "In3.Cu" signal)
  )
"""
    + _zone('(net "A") (layers "*.Cu")')
    + _zone('(net "B") (layers "F&B.Cu")')
    + _zone('(net "C") (layers "*In.Cu")')
    + _zone('(net "D") (layer "Mid1")')
    + _zone('(net "E") (layer "In5.Cu")')
    + _zone('(net "F") (layers "B.Cu" "F.SilkS" "F.Cu")')
    + _zone('(layer "In2.Cu")', '(placement (enabled yes) (sheetname "/"))')
    + _zone('(layer "In2.Cu")', "(keepout (tracks not_allowed))")
    + _zone('(net "H") (layer "F.Cu")', "(attr (teardrop (type padvia)))")
    + _zone('(layers "F.SilkS" "B.SilkS")')
    + ")\n"
)


class TestZoneLayers:
    def test_the_copper_layers_of_the_layer_table(self, tmp_path):
        # The copper entries end at the first entry with an other type, but the parser
        # enables the layer of each later entry too: In3.Cu after F.SilkS is a copper
        # layer of the board (pcbnew of KiCad 10.0.6: 5 copper layers).
        assert parse_board(tmp_path, ZONES)["general"]["layers"] == 5

    def test_the_layers_of_each_copper_zone(self, tmp_path):
        zones = parse_board(tmp_path, ZONES)["zones"]
        assert [(zone["net_name"], zone["layers"]) for zone in zones] == [
            ("A", ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "B.Cu"]),
            ("B", ["F.Cu", "B.Cu"]),
            ("C", ["In1.Cu", "In2.Cu", "In3.Cu"]),
            # "Mid1" is the first inner layer of the table: In1.Cu.
            ("D", ["In1.Cu"]),
            # A copper zone on a layer that the board does not enable.
            ("E", []),
            # The copper layers, from the top.
            ("F", ["F.Cu", "B.Cu"]),
        ]

    def test_no_rule_area_teardrop_or_zone_off_copper(self, tmp_path):
        # The zones of the Zone Manager (ZONE_SETTINGS_BAG): no rule area ((placement
        # ...) or (keepout ...)), no teardrop, no zone on non-copper layers only.
        nets = {zone["net_name"] for zone in parse_board(tmp_path, ZONES)["zones"]}
        assert nets == {"A", "B", "C", "D", "E", "F"}


# The inner names of LSET::NameToLayer, as pcbnew of KiCad 10.0.6 loads them.
NAMES = (
    """(kicad_pcb (version 20260206)
  (layers
    (0 "F.Cu" signal)
    (4 "In1Sig" signal)
    (6 "User.1" signal)
    (8 "In4" signal)
    (10 "Mid4" signal)
    (2 "B.Cu" signal)
  )
"""
    + _zone('(net "A") (layers "*.Cu")')
    + _zone('(net "B") (layer "F.Cu")', "(attr (teardrop (type track_end)))")
    + _zone('(net "C") (layer "F.Cu")', "(attr (teardrop))")
    + _zone('(net "D") (layers "F.Cu" "B.Cu") (layer "In1.Cu")')
    + _zone('(net "E") (layer "In1.Cu") (layers "F.Cu" "B.Cu")')
    + ")\n"
)


class TestLayerNames:
    def test_the_inner_names(self, tmp_path):
        # "In1Sig": In1.Cu (NameToLayer does not compare the last 3 characters).
        # "User.1": a layer that is not copper. "In4": In4.Cu (RemoveLast(3) does not
        # change a text of 1 character). "Mid4": In4.Cu by its position.
        data = parse_board(tmp_path, NAMES)
        assert data["general"]["layers"] == 4
        (zone, *_) = data["zones"]
        assert zone == {"net_name": "A", "layers": ["F.Cu", "In1.Cu", "In4.Cu", "B.Cu"]}

    def test_the_teardrop_type_and_the_last_layer_item(self, tmp_path):
        # A teardrop with no type is a zone (TD_NONE). The last (layer ...) or
        # (layers ...) of a zone gives its layers.
        zones = parse_board(tmp_path, NAMES)["zones"]
        assert [(zone["net_name"], zone["layers"]) for zone in zones] == [
            ("A", ["F.Cu", "In1.Cu", "In4.Cu", "B.Cu"]),
            ("C", ["F.Cu"]),
            ("D", ["In1.Cu"]),
            ("E", ["F.Cu", "B.Cu"]),
        ]

    def test_a_single_layer_has_no_masks(self, tmp_path):
        # (layer X) is a layer name (lookUpLayer): "*.Cu" is no layer (KiCad: an
        # undefined layer), thus the zone is not a copper zone.
        text = (
            '(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n'
            + _zone('(net "A") (layer "*.Cu")')
            + _zone('(net "B") (layer "F&B.Cu")')
            + ")"
        )
        assert parse_board(tmp_path, text)["zones"] == []

    def test_no_layer_table(self, tmp_path):
        # The layers of a new BOARD: F.Cu and B.Cu.
        text = "(kicad_pcb (version 20260206)\n" + _zone('(net "A") (layers "*.Cu")') + ")"
        data = parse_board(tmp_path, text)
        assert data["general"]["layers"] == 2
        assert data["zones"] == [{"net_name": "A", "layers": ["F.Cu", "B.Cu"]}]


@pytest.mark.parametrize("version", [20171130, 20211014, 20991231])
def test_only_boards_of_kicad_8_to_10(tmp_path, version):
    """CLAUDE.md rule 4: the text reader has rules of KiCad 8 to 10 only."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(f"(kicad_pcb (version {version}))", encoding="utf-8")
    with pytest.raises(KicadFileError, match=f"file version {version}"):
        PCBParser(str(path)).get_statistics()


def test_kicad6_fields():
    """_field also reads the (fp_text ...) of KiCad 6 and 7 (board_models.py reads
    the boards of each version)."""
    (footprint,) = parse(KICAD6).children("footprint")
    assert (_field(footprint, "Reference"), _field(footprint, "Value")) == ("D1", "red")


def test_statistics(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(KICAD9, encoding="utf-8")
    stats = PCBParser(str(path)).get_statistics()
    assert stats["total_tracks"] == 2 and stats["total_vias"] == 1
    assert stats["total_pads"] == 2 and stats["layers"] == 4


def test_file_that_is_not_an_s_expression(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    # A board of KiCad 10 with no closing parenthesis.
    path.write_text("(kicad_pcb (version 20260206)", encoding="utf-8")
    with pytest.raises(SExprError):
        PCBParser(str(path))._parse_file()
