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
        assert zones == [{"net_name": "GND", "layer": "B.Cu"}]


class TestKicad10:
    def test_nets_are_names(self, tmp_path):
        data = parse_board(tmp_path, KICAD10)
        assert [track["net"] for track in data["tracks"]] == ["GND"]
        assert data["zones"] == [{"net_name": "GND", "layer": "In1.Cu"}]

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
        assert parse_board(tmp_path, MORE)["zones"] == [{"net_name": "", "layer": "B.Cu"}]


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
