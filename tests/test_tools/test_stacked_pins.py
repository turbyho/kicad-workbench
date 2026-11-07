"""Tests of the stacked pin numbers ("[5-6]") in the pin tools.

KiCad 10.0 gives the netlist one node for each number of a stacked pin number
(SCH_PIN::GetStackedPinNumbers). The pin tools find the base name of the library pin
for each of these numbers. KiCad 9 gives one node with the text of the number. The
pin tools refuse a nightly build, which can have other rules.
"""

import asyncio
import json

import pytest

from kicad_workbench.parsers.netlist_parser import NetlistParser
from kicad_workbench.tools import device_tree, pin_analysis

LIB = (
    '\t(lib_symbols\n\t\t(symbol "A:STM32"\n\t\t\t(symbol "STM32_1_1"\n'
    '\t\t\t\t(pin bidirectional line (at 0 0 0) (length 2.54) (name "PA7") (number "[5-6]"))\n'
    '\t\t\t\t(pin bidirectional line (at 0 5 0) (length 2.54) (name "PB0") (number "4"))\n'
    "\t\t\t)\n\t\t)\n\t)\n"
)
SYMBOL = (
    '\t(symbol\n\t\t(lib_id "A:STM32")\n\t\t(at 100 50 0)\n\t\t(unit 1)\n'
    '\t\t(property "Reference" "U1"\n\t\t\t(at 100 45 90)\n\t\t)\n'
    '\t\t(property "Value" "STM32F103"\n\t\t\t(at 100 55 0)\n\t\t)\n'
    '\t\t(instances\n\t\t\t(project "board"\n\t\t\t\t(path "/a0"\n'
    '\t\t\t\t\t(reference "U1")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
)


def netlist_xml(kicad: str, nodes: list[tuple[str, str]]) -> str:
    """A netlist with U1 and one net for each (pin, pinfunction) node."""
    nets = "".join(
        f'<net code="{n}" name="/GPIO{n}"><node ref="U1" pin="{pin}" pinfunction="{function}" '
        'pintype="bidirectional"/></net>'
        for n, (pin, function) in enumerate(nodes, 1)
    )
    return (
        f"<export><design><tool>Eeschema {kicad}</tool></design><components>"
        '<comp ref="U1"><value>STM32F103</value></comp></components>'
        f"<nets>{nets}</nets></export>"
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    def write(kicad: str, nodes: list[tuple[str, str]]):
        path = tmp_path / "board.kicad_sch"
        path.write_text(
            f'(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(uuid "a0")\n'
            f"{LIB}{SYMBOL})\n",
            encoding="utf-8",
        )
        (tmp_path / "board.kicad_pro").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "net.xml").write_text(netlist_xml(kicad, nodes), encoding="utf-8")

        async def current_netlist(sch_path):
            return NetlistParser(str(tmp_path / "net.xml"))

        monkeypatch.setattr(pin_analysis, "current_netlist", current_netlist)
        monkeypatch.setattr("kicad_workbench.tools.netlist.current_netlist", current_netlist)
        return str(path)

    return write


KICAD10_NODES = [("5", "PA7_5"), ("6", "PA7_6"), ("4", "PB0_4")]


def test_the_base_names_of_the_netlist_numbers_of_kicad_10(project):
    path = project("10.0.7", KICAD10_NODES)
    result = asyncio.run(pin_analysis.extract_pinmux_config(path))
    # The base name PA7 gives the code lines of the two numbers of the stacked pin.
    assert result.count("// GPIO_A_7 // GPIO_AF") == 2, result
    assert "| PA7 (5) |" in result and "| PA7 (6) |" in result
    assert "⚠️ The server does not know" not in result


def test_the_gpio_names_of_the_netlist_numbers_of_kicad_10(project):
    path = project("10.0.7", KICAD10_NODES)
    result = asyncio.run(device_tree.extract_gpio_config(path))
    assert "| U1 | PA7 (5) | /GPIO1 |" in result, result
    assert "| U1 | PA7 (6) | /GPIO2 |" in result


def test_kicad_9_uses_the_text_as_one_number(project):
    path = project("9.0.9", [("[5-6]", "PA7"), ("4", "PB0")])
    result = asyncio.run(device_tree.extract_gpio_config(path))
    assert "| U1 | PA7 ([5-6]) | /GPIO1 |" in result, result


def test_the_pin_tools_refuse_a_nightly_build(project):
    # Branch master has other rules for the stacked pin numbers
    # (UnescapeStackedPinItem). The pin tools refuse a nightly build before they read
    # the pins (placed_instances): no pin keeps a number that the server cannot expand.
    path = project("10.99.0", KICAD10_NODES)
    result = asyncio.run(pin_analysis.analyze_pin_functions(path))
    assert result.startswith("❌ The server has no verified rules of KiCad 10.99.0"), result
