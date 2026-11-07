"""Tests of the place of the netlist parse: off the event loop.

The tools parse the XML of a netlist in the thread that exports or reads it (the
constructors of NetlistParser), not on the event loop (CLAUDE.md: "Run file reads
and subprocesses off the event loop").
"""

import asyncio
import threading

import pytest

from kicad_workbench.parsers.netlist_parser import NetlistParser
from kicad_workbench.tools import netlist, schematic

NETLIST = (
    b"<export><design><tool>Eeschema 10.0.7</tool></design><components>"
    b'<comp ref="R1"><value>10k</value></comp></components><nets>'
    b'<net code="1" name="/A"><node ref="R1" pin="1" pinfunction="1" pintype="passive"/></net>'
    b"</nets></export>"
)


@pytest.fixture
def parse_threads(monkeypatch):
    """The threads in which NetlistParser parses the XML."""
    threads = []
    parse = NetlistParser._parse

    def record(self, data):
        threads.append(threading.current_thread())
        return parse(self, data)

    monkeypatch.setattr(NetlistParser, "_parse", record)
    return threads


def test_current_netlist_parses_in_a_thread(tmp_path, monkeypatch, parse_threads):
    monkeypatch.setattr(netlist, "export_netlist", lambda path: (NETLIST, None))

    async def run():
        parser = await netlist.current_netlist(tmp_path / "a.kicad_sch")
        # The queries use the data in memory: no other parse.
        assert parser.get_components() and parser.get_nets()
        return threading.current_thread()

    loop_thread = asyncio.run(run())
    assert len(parse_threads) == 1 and parse_threads[0] is not loop_thread


@pytest.mark.parametrize(
    "tool, args",
    [
        (netlist.get_netlist_components, ()),
        (netlist.get_netlist_nets, ()),
        (netlist.trace_netlist_connection, ("R1", "1")),
    ],
)
def test_the_netlist_file_tools_parse_in_a_thread(tmp_path, parse_threads, tool, args):
    path = tmp_path / "a.xml"
    path.write_bytes(NETLIST)

    async def run():
        result = await tool(str(path), *args)
        return result, threading.current_thread()

    result, loop_thread = asyncio.run(run())
    assert "R1" in result, result
    assert len(parse_threads) == 1 and parse_threads[0] is not loop_thread


def test_a_netlist_that_is_not_xml(tmp_path, monkeypatch):
    # get_symbol_details then gives the pins of the schematic, with the reason.
    monkeypatch.setattr(netlist, "export_netlist", lambda path: (b"<export>", None))
    pins, reason = schematic._netlist_pins(tmp_path / "a.kicad_sch", "R1")
    assert pins is None and "is not valid XML" in reason
