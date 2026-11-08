"""Tests of the pin numbers on more than one net.

KiCad gives a pin number more than one net when two pins of a symbol have that
number, or in KiCad 10 when a stacked pin number ("[1-2]") and a pin "2" are on
different nets (NETLIST_EXPORTER_XML::makeListOfNets). The netlist model keeps each
node (NetlistComponent.nodes), and the tools give each net of such a pin with a
warning. The netlist is the netlist of kicad-cli 10.0.7 for
spike/kicad-versions/duplicate_pins_check.py.
"""

import asyncio

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.parsers.netlist_parser import NetlistParser
from kicad_workbench.tools import netlist, pin_analysis


def netlist_xml(a_type: str = "passive", nc_same_net: bool = False) -> str:
    nodes = {
        "/NA": [("U1", "2", "A_2", a_type), ("U2", "1", "1", "output")],
        "/NB": [("U1", "2", "B_2", "passive"), ("U2", "2", "2", "output")],
        "/NC": [("U1", "1", "C_1", "passive"), ("U1", "2", "C_2", "passive")],
        "/ND": [("U1", "3", "E_3", "passive"), ("U2", "3", "3", "passive")],
    }
    if nc_same_net:
        # KiCad 10: "[1-2]" (C) and "2" (A) on one net give two nodes U1 2.
        nodes = {
            "/NA": [
                ("U1", "2", "A_2", a_type),
                ("U1", "1", "C_1", a_type),
                ("U1", "2", "C_2", a_type),
            ],
            "/ND": [("U1", "3", "E_3", "passive")],
        }
    nets = "".join(
        f'<net code="{code}" name="{name}">'
        + "".join(
            f'<node ref="{ref}" pin="{pin}" pinfunction="{function}" pintype="{kind}"/>'
            for ref, pin, function, kind in items
        )
        + "</net>"
        for code, (name, items) in enumerate(nodes.items(), 1)
    )
    return (
        "<export><design><tool>Eeschema 10.0.7</tool></design><components>"
        '<comp ref="U1"><value>D</value></comp><comp ref="U2"><value>X</value></comp>'
        f"</components><nets>{nets}</nets></export>"
    )


def parser(**options) -> NetlistParser:
    return NetlistParser.from_bytes(netlist_xml(**options).encode())


def test_each_node_of_a_pin_number():
    component = parser().get_components()["U1"]
    assert [(n.number, n.net, n.name) for n in component.nodes if n.number == "2"] == [
        ("2", "/NA", "A"),
        ("2", "/NB", "B"),
        ("2", "/NC", "C"),
    ]
    assert component.numbers() == ["2", "1", "3"]
    assert component.pins_on_more_nets() == {"2": ["/NA", "/NB", "/NC"]}


def test_the_warning():
    note = parser().pins_on_more_nets_note(["U1", "U2"])
    assert note.startswith("⚠️ The netlist has these pin numbers on more than one net: ")
    assert "U1.2 (/NA, /NB, /NC). The tool gives each net" in note
    assert "duplicate_pins" in note
    assert parser().pins_on_more_nets_note(["U2"]) == ""


def test_the_warning_of_one_pin():
    # The answer of one pin number warns only about that number.
    assert "U1.2 (/NA, /NB, /NC)" in parser().pins_on_more_nets_note(["U1"], "2")
    assert parser().pins_on_more_nets_note(["U1"], "3") == ""


def test_two_nodes_of_one_number_on_one_net():
    # KiCad 10: "[1-2]" and "2" on one net give two nodes U1 2; the trace gives the net
    # one time, and the conflict check names the two pins.
    netlist_parser = parser(nc_same_net=True)
    assert netlist_parser.trace_connection("U1", "2")["nets"] == [
        {"net": "/NA", "connected_to": []}
    ]
    assert [p["pin"] for p in netlist_parser.trace_connection("U1")["nets"]["/NA"]] == ["2", "1"]
    assert netlist_parser.pins_on_more_nets_note() == ""


def test_trace_of_a_pin_number_on_more_nets():
    result = parser().trace_connection("U1", "2")
    assert [net["net"] for net in result["nets"]] == ["/NA", "/NB", "/NC"]
    assert result["nets"][0]["connected_to"] == [("U2", "1")]
    assert parser().trace_connection("U1", "9") == {"error": "Pin 9 not found in component U1"}


def test_the_type_of_each_node_on_its_net(tmp_path, monkeypatch):
    # Pin 2 of U1 is an output on /NA and passive on /NB: only /NA has two drivers.
    netlist_parser = parser(a_type="output")

    async def current_netlist(path):
        return netlist_parser

    monkeypatch.setattr(pin_analysis, "current_netlist", current_netlist)
    conflicts = asyncio.run(pin_analysis._pin_conflicts(tmp_path / "a.kicad_sch"))
    drivers = [c for c in conflicts if c["type"] == "multiple_outputs"]
    assert [c["net"] for c in drivers] == ["/NA"]


@pytest.mark.parametrize("pin", ["", "2"])
def test_the_netlist_tools(tmp_path, pin):
    path = tmp_path / "a.xml"
    path.write_text(netlist_xml(), encoding="utf-8")
    result = asyncio.run(netlist.trace_netlist_connection(str(path), "U1", pin))
    # The trace of one pin has "**Net:** /NA", the trace of all pins "### Net: /NA".
    for net in ("/NA", "/NB", "/NC"):
        assert f"Net:** {net}\n" in result or f"### Net: {net}\n" in result, result
    assert "U1.2 (/NA, /NB, /NC)" in result
    result = asyncio.run(netlist.get_netlist_components(str(path), "U1"))
    assert "| 2 | /NA |\n| 2 | /NB |\n| 2 | /NC |" in result, result
    assert "U1.2 (/NA, /NB, /NC)" in result


def test_duplicate_references_give_an_annotation_warning():
    # Two symbols "U1" (an annotation error): the nodes of the two symbols are not
    # two pins with one number.
    text = netlist_xml().replace(
        '<comp ref="U2">', '<comp ref="U1"><value>D</value></comp><comp ref="U2">'
    )
    netlist_parser = NetlistParser.from_bytes(text.encode())
    assert netlist_parser.get_duplicate_references() == ["U1"]
    note = netlist_parser.pins_on_more_nets_note()
    assert note.startswith("⚠️ More than one component of the netlist has the reference"), note
    assert "U1.2 (/NA, /NB, /NC)" in note
    assert "duplicate_pins" not in note


def test_a_net_with_no_name():
    with pytest.raises(InputError, match="has a net with no name"):
        NetlistParser.from_bytes(b'<export><nets><net code="1"/></nets></export>')
