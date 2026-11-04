"""Tests for the text parser of KiCad schematics on the S-expression parser."""

import pytest

from kicad_workbench.parsers.schematic_parser import (
    SchematicParser,
    _definition_pins,
    _shown_text,
)
from kicad_workbench.parsers.sexpr import parse

# A schematic with the items that the regex parser read incorrectly:
# negative coordinates (issue #26), escaped strings, a power symbol of a
# project library whose Value is not its library name, and a symbol in the
# one-line form of KiCad 6.
SCH = r"""(kicad_sch
	(version 20250114)
	(generator "eeschema")
	(uuid "00000000-0000-0000-0000-000000000000")
	(paper "A4")
	(title_block
		(title "\"Slow\" GPIO pins")
		(rev "B")
		(comment 1 "first")
		(comment 2 "second")
	)
	(lib_symbols
		(symbol "Device:R"
			(symbol "R_1_1"
				(pin passive line (at 0 3.81 270) (length 1.27)
					(name "~" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27)))))
				(pin passive line (at 0 -3.81 90) (length 1.27)
					(name "~" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27)))))
			)
		)
		(symbol "myproject:VBUS_5V" (power global)
			(property "Value" "VBUS" (at 0 0 0))
			(symbol "VBUS_5V_0_1"
				(pin power_in line (at 0 0 90) (length 0) hide
					(name "VBUS" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
		)
		(symbol "power:GND" (power local)
			(property "Value" "GND" (at 0 0 0))
			(symbol "GND_0_1"
				(pin power_in line (at 0 0 270) (length 0) hide
					(name "GND" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
		)
		(symbol "power:PWR_FLAG" (power)
			(property "Value" "PWR_FLAG" (at 0 0 0))
			(symbol "PWR_FLAG_0_0"
				(pin power_out line (at 0 0 90) (length 0)
					(name "pwr" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
		)
	)
	(symbol
		(lib_id "Device:R")
		(at -25.4 -12.7 90)
		(unit 1)
		(dnp yes)
		(in_bom no)
		(property "Reference" "R1" (at 0 0 0))
		(property "Value" "4k7 \"1%\"" (at 0 0 0))
		(property "Footprint" "Resistor_SMD:R_0603" (at 0 0 0))
		(pin "1" (uuid "a"))
		(pin "2" (uuid "b"))
	)
	(symbol (lib_id "myproject:VBUS_5V") (at 10 -5 0) (unit 1)
		(property "Reference" "#PWR01" (at 0 0 0))
		(property "Value" "+5V_USB" (at 0 0 0))
	)
	(symbol (lib_id "power:GND") (at 20 30 0) (unit 1)
		(property "Reference" "#PWR02" (at 0 0 0))
		(property "Value" "GND" (at 0 0 0))
	)
	(symbol (lib_id "power:PWR_FLAG") (at 40 40 0) (unit 1)
		(property "Reference" "#FLG01" (at 0 0 0))
		(property "Value" "PWR_FLAG" (at 0 0 0))
	)
	(global_label "SDA \"bus\"" (shape bidirectional) (at -50.8 25.4 0))
	(global_label "SDO{slash}out1" (shape output) (at 60 60 0))
	(global_label "SDA \"bus\"" (shape bidirectional) (at 100 100 0))
	(label "NET_A" (at 1 2 0))
	(hierarchical_label "IN" (shape input) (at -20 -10 180))
	(wire (pts (xy -10 -20) (xy 30 -20)) (uuid "w1"))
	(wire (pts (xy 30 -20) (xy 30 40)) (uuid "w2"))
	(junction (at 30 -20) (diameter 0))
	(sheet (at 100 100) (size 20 10)
		(property "Sheet name" "Power" (at 0 0 0))
		(property "Sheet file" "power.kicad_sch" (at 0 0 0))
	)
)
"""


@pytest.fixture
def parser(tmp_path):
    path = tmp_path / "board.kicad_sch"
    path.write_text(SCH, encoding="utf-8")
    return SchematicParser(str(path))


def test_title_block_with_escapes(parser):
    title = parser.get_title_block()
    assert title["title"] == '"Slow" GPIO pins'
    assert title["rev"] == "B" and title["comment"] == "first"


def test_component(parser):
    (component,) = parser.get_components()
    assert component.reference == "R1"
    assert component.value == '4k7 "1%"'
    assert component.footprint == "Resistor_SMD:R_0603"
    assert component.position == (-25.4, -12.7)
    assert component.flags == {
        "dnp": True,
        "in_bom": False,
        "on_board": True,
        "exclude_from_sim": False,
    }
    assert [(pin["number"], pin["electrical_type"]) for pin in component.pins] == [
        ("1", "passive"),
        ("2", "passive"),
    ]


def test_nets(parser):
    nets = [(net.name, net.type, net.position) for net in parser.get_nets()]
    assert nets == [
        # The first label of a name gives the position.
        ('SDA "bus"', "global", (-50.8, 25.4)),
        # KiCad writes "/" in a label as "{slash}".
        ("SDO/out1", "global", (60.0, 60.0)),
        ("NET_A", "local", (1.0, 2.0)),
        ("IN", "hierarchical", (-20.0, -10.0)),
        # The net name of a power symbol is its Value, also when the symbol
        # comes from a project library.
        ("+5V_USB", "power", (10.0, -5.0)),
        ("GND", "power", (20.0, 30.0)),
        # PWR_FLAG has a power_out pin and names no net.
    ]


def test_kicad6_sheet_fields(parser):
    assert parser.get_sheets() == [{"name": "Power", "file": "power.kicad_sch"}]


def test_wire_network_with_negative_coordinates(parser):
    network = parser.build_wire_network()
    assert set(network[(-10.0, -20.0)]) == {(30.0, -20.0)}
    assert set(network[(30.0, -20.0)]) == {(-10.0, -20.0), (30.0, 40.0)}


def test_connections_of_a_component(parser):
    connections = parser.get_component_connections("R1")
    assert connections["pins"] == ["1", "2"]
    assert [label["name"] for label in connections["nearby_labels"]] == ["IN"]
    assert parser.get_component_connections("R9") == {"error": "Component R9 not found"}


def test_trace_through_wires_with_negative_coordinates(parser):
    traced = parser.trace_wire_network("R1")
    assert traced["position"] == (-25.4, -12.7)
    assert traced["trace_path"] == [(-10.0, -20.0), (30.0, -20.0), (30.0, 40.0)]
    # No label is near the path, and no power symbol is within 15 mm.
    assert traced["connected_labels"] == []


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("SDO{slash}out1", "SDO/out1"),
        ("{dblquote}A{dblquote}{backslash}", '"A"\\'),
        # Braces after $, ~, ^ and _ are markup and stay.
        ("~{crst}{slash}out2", "~{crst}/out2"),
        ("${VAR}", "${VAR}"),
        ("x^{2}_{i}", "x^{2}_{i}"),
        # An unknown escape and a brace with no end stay.
        ("a{unknown}b", "a{unknown}b"),
        ("{slash", "{slash"),
        ("{a{slash}b}", "{a/b}"),
    ],
)
def test_shown_text_of_a_label(text, shown):
    assert _shown_text(text) == shown


# Symbol definitions that are found by lib_name, and a symbol whose pin
# number "1" has another type in unit 2 and in body style 2.
UNITS = r"""(kicad_sch
	(version 20250114)
	(lib_symbols
		(symbol "GND_1" (power)
			(symbol "GND_1_0_1"
				(pin power_in line (at 0 0 270) (length 0) hide
					(name "GND" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
		)
		(symbol "Gates:NAND"
			(symbol "NAND_0_1"
				(pin power_in line (at 0 5 270) (length 1)
					(name "VCC" (effects (font (size 1.27 1.27))))
					(number "14" (effects (font (size 1.27 1.27))))))
			(symbol "NAND_1_1"
				(pin input line (at -5 0 0) (length 1)
					(name "A" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
			(symbol "NAND_1_2"
				(pin output line (at -5 0 0) (length 1)
					(name "A_DM" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
			(symbol "NAND_2_1"
				(pin bidirectional line (at -5 0 0) (length 1)
					(name "B" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
		)
	)
	(symbol (lib_name "GND_1") (lib_id "power:GND") (at 1 1 0) (unit 1)
		(property "Reference" "PWR1" (at 0 0 0))
		(property "Value" "GND{slash}A" (at 0 0 0))
	)
	(symbol (lib_id "Gates:NAND") (at 10 10 0) (unit 1) (body_style 1)
		(property "Reference" "U1" (at 0 0 0))
		(pin "1" (uuid "a")) (pin "14" (uuid "b"))
	)
	(symbol (lib_id "Gates:NAND") (at 20 10 0) (unit 2) (body_style 1)
		(property "Reference" "U2" (at 0 0 0))
		(pin "1" (uuid "c"))
	)
	(symbol (lib_id "Gates:NAND") (at 30 10 0) (unit 1) (convert 2)
		(property "Reference" "U3" (at 0 0 0))
		(pin "1" (uuid "d"))
	)
)
"""


@pytest.fixture
def units(tmp_path):
    path = tmp_path / "units.kicad_sch"
    path.write_text(UNITS, encoding="utf-8")
    return SchematicParser(str(path))


def test_definition_is_found_by_lib_name(units):
    # The power symbol has a reference with no "#", but it is not a component.
    assert [component.reference for component in units.get_components()] == ["U1", "U2", "U3"]
    # Its net name is the shown text of the Value.
    assert [(net.name, net.type) for net in units.get_nets()] == [("GND/A", "power")]


def test_pins_of_the_unit_and_of_the_body_style(units):
    pins = {
        component.reference: [
            (pin["number"], pin["name"], pin["electrical_type"]) for pin in component.pins
        ]
        for component in units.get_components()
    }
    assert pins == {
        "U1": [("1", "A", "input"), ("14", "VCC", "power_in")],
        "U2": [("1", "B", "bidirectional")],
        # KiCad 8 and earlier write the body style as (convert N).
        "U3": [("1", "A_DM", "output")],
    }


def test_pin_number_of_a_definition_has_no_spaces():
    # SCH_PIN::SetNumber replaces each space with "_".
    definition = parse('(symbol "Lib:U" (symbol "U_1_1" (pin input line (number "A 1"))))')
    assert [pin["number"] for pin in _definition_pins(definition)] == ["A_1"]
