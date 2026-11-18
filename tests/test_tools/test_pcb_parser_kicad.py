"""Tests for the pcbnew parser of KiCad boards (parsers/pcb_parser_kicad.py)."""

import math

import pytest

from kicad_workbench.parsers.pcb_parser_kicad import PCBParserKiCad

pcbnew = pytest.importorskip("pcbnew", reason="KiCad pcbnew is not installed")


@pytest.fixture
def board(tmp_path):
    """A board with a track, an arc and a via, made with pcbnew."""
    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(0, 0))
    track.SetEnd(pcbnew.VECTOR2I(mm(3), mm(4)))
    track.SetWidth(mm(0.2))
    track.SetLayer(pcbnew.F_Cu)
    board.Add(track)
    arc = pcbnew.PCB_ARC(board)
    arc.SetStart(pcbnew.VECTOR2I(mm(1), 0))
    arc.SetMid(pcbnew.VECTOR2I(mm(math.sqrt(0.5)), mm(math.sqrt(0.5))))
    arc.SetEnd(pcbnew.VECTOR2I(0, mm(1)))
    arc.SetWidth(mm(0.2))
    arc.SetLayer(pcbnew.F_Cu)
    board.Add(arc)
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(mm(5), mm(5)))
    via.SetWidth(mm(0.6))
    via.SetDrill(mm(0.3))
    board.Add(via)
    path = tmp_path / "board.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    return path


def test_tracks_and_vias(board):
    # KiCad 10.0.6 and earlier with SWIG 4.4 and later: BOARD.GetTracks()
    # fails with "'SwigPyIterator' object has no attribute 'next'".
    parser = PCBParserKiCad(str(board))
    tracks = parser.get_tracks()
    assert sorted(round(track.length, 3) for track in tracks) == [
        round(math.pi / 2, 3),
        5.0,
    ]
    (via,) = parser.get_vias()
    assert via.position == (5.0, 5.0)
    assert via.drill == pytest.approx(0.3)


def test_counts_of_the_board_info(board):
    info = PCBParserKiCad(str(board)).get_board_info()
    assert (info["tracks_count"], info["vias_count"]) == (2, 1)


@pytest.fixture
def zone_board(tmp_path):
    """A board with 4 copper layers and zones, made with pcbnew."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    nets = {}
    for name in ("GND", "+3V3"):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])

    def zone(layers, net=None):
        item = pcbnew.ZONE(board)
        layer_set = pcbnew.LSET()
        for layer in layers:
            layer_set.AddLayer(layer)
        item.SetLayerSet(layer_set)
        if net:
            item.SetNetCode(nets[net].GetNetCode())
        outline = item.Outline()
        outline.NewOutline()
        for x, y in ((0, 0), (10, 0), (10, 10)):
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board.Add(item)
        return item

    zone([pcbnew.B_Cu, pcbnew.F_Cu], "GND")
    zone([pcbnew.In2_Cu], "+3V3")
    rule = zone([pcbnew.In1_Cu])
    rule.SetIsRuleArea(True)
    zone([pcbnew.F_SilkS])
    path = tmp_path / "zones.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    return path


def test_the_copper_zones_and_their_layers(zone_board):
    # GetLayerName() of a zone gives F.Cu for each zone: a zone keeps its layers in its
    # layer set. The rule area and the zone on F.SilkS are not copper zones.
    parser = PCBParserKiCad(str(zone_board))
    zones = parser.get_zones()
    assert [(zone.net_name, zone.layers) for zone in zones] == [
        ("GND", ["F.Cu", "B.Cu"]),
        ("+3V3", ["In2.Cu"]),
    ]
    assert parser.get_board_info()["zones_count"] == 2


def test_the_two_readers_give_the_same_zones(zone_board):
    from kicad_workbench.parsers.pcb_parser import PCBParser

    text = PCBParser(str(zone_board))._parse_file()["zones"]
    zones = PCBParserKiCad(str(zone_board)).get_zones()
    assert [(zone["net_name"], zone["layers"]) for zone in text] == [
        (zone.net_name, zone.layers) for zone in zones
    ]


def test_the_power_tool_counts_each_layer_of_a_zone(zone_board):
    import asyncio

    from kicad_workbench.tools import pcb

    answer = asyncio.run(pcb.analyze_pcb_power_integrity(str(zone_board)))
    assert "| GND | 1 | F.Cu, B.Cu (unfilled) |" in answer, answer
    assert "| +3V3 | 1 | In2.Cu (unfilled) |" in answer, answer
    assert "**GND Coverage:** 1 zones across 2 layers (0 filled)" in answer, answer
    assert "GND zones only on 2 of 4 copper layers" in answer, answer


def test_a_net_with_only_zones(zone_board):
    import asyncio

    from kicad_workbench.tools import pcb

    answer = asyncio.run(pcb.find_tracks_by_net(str(zone_board), "+3V3"))
    assert "**Copper Zones:** 1" in answer, answer
    assert "| +3V3 | In2.Cu | No |" in answer, answer
    assert "**Track Layers:** N/A" in answer, answer
    similar = asyncio.run(pcb.find_tracks_by_net(str(zone_board), "3v3"))
    assert "- `+3V3` (0 segments, 0 vias, 1 copper zones)" in similar, similar
