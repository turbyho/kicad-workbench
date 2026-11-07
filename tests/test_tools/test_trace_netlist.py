"""Tests of trace_netlist_connection with no pin number.

NetlistParser.trace_connection gives a list of the pins of the component for
each net: a component can have more than one pin on a net. The tool read one
pin for each net and always failed (TypeError).
"""

import asyncio

from kicad_workbench.tools import netlist

NETLIST = (
    "<export><design><tool>Eeschema 10.0.7</tool></design><components>"
    '<comp ref="U1"><value>X</value></comp><comp ref="R1"><value>10k</value></comp>'
    "</components><nets>"
    '<net code="1" name="GND"><node ref="U1" pin="1" pintype="power_in"/>'
    '<node ref="U1" pin="3" pintype="power_in"/><node ref="R1" pin="2" pintype="passive"/></net>'
    '<net code="2" name="/A"><node ref="U1" pin="2" pintype="output"/></net>'
    "</nets></export>"
)


def test_all_pins_of_a_component(tmp_path):
    path = tmp_path / "a.xml"
    path.write_text(NETLIST, encoding="utf-8")
    result = asyncio.run(netlist.trace_netlist_connection(str(path), "U1"))
    assert "**Total Nets:** 2" in result, result
    assert "### Net: GND\n**Pins:** 1, 3\n\n| Reference | Pin |" in result
    assert "| R1 | 2 |" in result
    assert "### Net: /A\n**Pin:** 2\n\nNo other components on this net." in result
