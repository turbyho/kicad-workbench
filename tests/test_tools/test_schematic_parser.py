"""Tests for the text parser of KiCad schematics on the S-expression parser."""

from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import KicadFileError, file_text
from kicad_workbench.parsers.schematic_parser import (
    SchematicParser,
    _component_pins,
    _definition_pins,
    _kicad_rules,
    _shown_text,
    unknown_alternate_note,
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
    # The library pins of unit 0 and of the unit, in the order of the (pin ...) items
    # (UpdatePins, GetPins). UpdatePins does not filter the units: the (pin "1") of U2
    # matches the first library pin "1" (unit 1), thus pins 14 and 1 of unit 2 are new
    # pins at the end, in the order of the library.
    assert pins == {
        "U1": [("1", "A", "input"), ("14", "VCC", "power_in")],
        "U2": [("14", "VCC", "power_in"), ("1", "B", "bidirectional")],
        # KiCad 8 and earlier write the body style as (convert N). VCC is in body
        # style 1 only.
        "U3": [("1", "A_DM", "output")],
    }


def definition_pins(text: str, version: int = 20260306) -> list[tuple]:
    pins = _definition_pins(parse(text), Path("a.kicad_sch"), version)
    return [(p["unit"], p["style"], p["number"], p["name"]) for p in pins]


def test_pin_number_of_a_definition_has_no_spaces():
    # SCH_PIN::SetNumber replaces each space with "_".
    text = '(symbol "Lib:U" (symbol "U_1_1" (pin input line (number "A 1"))))'
    assert definition_pins(text) == [(1, 1, "A_1", "")]


@pytest.mark.parametrize("version, text", [(20250114, ""), (20250318, "~"), (20260306, "~")])
def test_tilde_pin_texts(version, text):
    # Before 20250318 "~" is an empty name or number: the parser of KiCad 10, and
    # GetShownName and GetShownNumber of KiCad 8 and 9 (the netlist).
    definition = '(symbol "Lib:U" (symbol "U_1_1" (pin input line (name "~") (number "~"))))'
    assert definition_pins(definition, version) == [(1, 1, text, text)]


def test_pins_outside_a_sub_symbol_and_with_no_number():
    # parseLibSymbol: a pin outside a sub-symbol is in unit 1 with body style 1.
    # A pin with no (number ...) has an empty number.
    text = (
        '(symbol "Lib:U" (pin input line (number "1"))'
        ' (symbol "U_0_1" (pin input line (number "2")))'
        ' (symbol "U_2_1" (pin input line (name "X"))))'
    )
    assert definition_pins(text) == [(1, 1, "1", ""), (0, 1, "2", ""), (2, 1, "", "X")]


def test_sub_symbol_name_that_kicad_cannot_read():
    text = '(symbol "Lib:U" (symbol "Other_1_1" (pin input line (number "1"))))'
    with pytest.raises(KicadFileError, match="must be"):
        definition_pins(text)


def test_tilde_pin_of_a_placed_symbol(tmp_path):
    # KiCad 9 writes (pin "~") with GetNumber; the netlist number is "" (GetShownNumber).
    path = tmp_path / "a.kicad_sch"
    path.write_text(
        '(kicad_sch (version 20250114) (generator "eeschema") (uuid "a0")\n'
        '  (lib_symbols (symbol "Lib:U" (symbol "U_1_1"'
        ' (pin input line (name "IN") (number "~")))))\n'
        '  (symbol (lib_id "Lib:U") (at 0 0 0) (unit 1)\n'
        '    (property "Reference" "U1" (at 0 0 0)) (property "Value" "U" (at 0 0 0))\n'
        '    (pin "~" (uuid "p1"))))\n',
        encoding="utf-8",
    )
    (component,) = SchematicParser(str(path)).get_components()
    assert [(p["number"], p["name"], p["electrical_type"]) for p in component.pins] == [
        ("", "IN", "input")
    ]


@pytest.mark.parametrize(
    "text, version, expected",
    [("~", 20250114, ""), ("~", 20250318, "~"), ("~", None, "~"), ("x", 20231120, "x")],
)
def test_file_text(text, version, expected):
    # "~" of a file before EMPTY_TEXT_MARKER is an empty text; an unknown version
    # keeps the text.
    assert file_text(text, version) == expected


DEFINITION = (
    '(symbol "Lib:U" (symbol "U_1_1"'
    ' (pin bidirectional line (name "PA7") (number "1")'
    '  (alternate "SPI1_MOSI" output line) (alternate "TIM1" input line))'
    ' (pin passive line (name "B") (number "2"))'
    ' (pin passive line (name "C") (number "A 1"))))'
)


def component_pins(items: str, kicad: str | None = "10.0.6") -> list[tuple]:
    definition = _definition_pins(parse(DEFINITION), Path("a.kicad_sch"), 20260306)
    symbol = parse(f'(symbol (lib_id "Lib:U") (unit 1) {items})')
    return [
        (p["number"], p["name"], p["electrical_type"], p["alternate"])
        for p in _component_pins(symbol, definition, kicad)
    ]


def test_alternates_of_the_pins():
    # The alternate gives the name and the type (GetShownName, GetType). SetAlt of
    # KiCad 9 and 10 keeps no alternate that the library pin does not have, nor
    # its base name.
    items = (
        '(pin "1" (alternate "SPI1_MOSI")) (pin "2" (alternate "X")) (pin "A_1" (alternate "C"))'
    )
    assert component_pins(items) == [
        ("1", "SPI1_MOSI", "output", "SPI1_MOSI"),
        ("2", "B", "passive", ""),
        ("A_1", "C", "passive", ""),
    ]
    # KiCad 8 keeps each alternate; an alternate that the library pin does not have
    # gets the type of a zero ALT: input.
    assert component_pins(items, kicad="8.0.9")[1:] == [
        ("2", "X", "input", "X"),
        ("A_1", "C", "input", "C"),
    ]


def test_pins_that_the_symbol_does_not_have():
    # UpdatePins makes a pin for each library pin with no (pin "N"), and removes a
    # (pin "N") with no library pin. Here "9" is reused for "2": no alternate.
    assert component_pins('(pin "1") (pin "9")') == [
        ("1", "PA7", "bidirectional", ""),
        ("2", "B", "passive", ""),
        ("A_1", "C", "passive", ""),
    ]


def test_space_in_the_number_of_a_placed_pin():
    # The instance pin keeps the space; the library pin has "A_1": no match. The pin
    # gets the (pin "A 1") again, with its alternate (the only item left). KiCad 8
    # keeps it; SetAlt of KiCad 9 and 10 removes it (the library pin has no "Z").
    items = '(pin "1") (pin "2") (pin "A 1" (alternate "Z"))'
    assert component_pins(items, kicad="8.0.9")[2] == ("A_1", "Z", "input", "Z")
    assert component_pins(items)[2] == ("A_1", "C", "passive", "")


def test_alternate_of_a_reused_pin_that_is_not_known():
    # Two (pin ...) items with no library pin and other alternates: the order of
    # the addresses decides which one a library pin gets (KiCad 8). KiCad 9 and 10
    # remove both alternates (SetAlt), thus the result is known.
    items = '(pin "1") (pin "8" (alternate "P")) (pin "9" (alternate "Q"))'
    assert component_pins(items, kicad="8.0.9")[1:] == [("2", "", "", None), ("A_1", "", "", None)]
    assert component_pins(items)[1:] == [("2", "B", "passive", ""), ("A_1", "C", "passive", "")]


def test_last_alternate_of_a_number_wins():
    pins = component_pins('(pin "1" (alternate "TIM1")) (pin "1" (alternate "SPI1_MOSI"))')
    assert pins[0] == ("1", "SPI1_MOSI", "output", "SPI1_MOSI")


@pytest.mark.parametrize(
    "kicad, rules",
    [
        ("8.0.9", (8, "keep")),
        ("9.0.2", (9, "keep")),
        ("9.0.3", (9, "base")),
        ("9.0.4", (9, "library")),
        ("10.0.7", (10, "library")),
        ("10.99.0", (10, "library")),
        ("10.0.8", None),
        (None, None),
    ],
)
def test_kicad_rules(kicad, rules):
    assert _kicad_rules(kicad) == rules


def test_alternate_with_no_known_kicad_version():
    # The rules of SetAlt differ for an alternate that the library pin does not have.
    items = '(pin "1" (alternate "SPI1_MOSI")) (pin "2" (alternate "X")) (pin "A_1")'
    assert component_pins(items, None) == [
        ("1", "SPI1_MOSI", "output", "SPI1_MOSI"),
        ("2", "", "", None),
        ("A_1", "C", "passive", ""),
    ]


def test_unknown_alternate_note():
    from kicad_workbench.parsers.schematic_parser import SchematicComponent

    known = SchematicComponent("U1", "U", "Lib:U", pins=[{"number": "1", "alternate": ""}])
    unknown = SchematicComponent("U2", "U", "Lib:U", pins=[{"number": "2", "alternate": None}])
    assert unknown_alternate_note([known]) == ""
    note = unknown_alternate_note([known, unknown])
    assert note.startswith("⚠️ The name and the electrical type of these pins are not known")
    assert ": U2.2. KiCad gives" in note


@pytest.mark.parametrize(
    "kicad, expected",
    [
        ("8.0.9", [("", "??", "no_connect", ""), ("2", "X", "no_connect", "X")]),
        ("10.0.6", [("~", "", "unspecified", ""), ("2", "X", "unspecified", "X")]),
        (None, [("", "", "", None), ("2", "", "", None)]),
    ],
)
def test_pins_of_a_symbol_with_no_definition(tmp_path, kicad, expected):
    # UpdatePins stops with no m_part: the pins are the (pin ...) items of the file.
    path = tmp_path / "a.kicad_sch"
    path.write_text(
        '(kicad_sch (version 20250114) (generator "eeschema") (uuid "a0") (lib_symbols)\n'
        '  (symbol (lib_id "Lib:Gone") (at 0 0 0) (unit 1)\n'
        '    (property "Reference" "U1" (at 0 0 0)) (property "Value" "U" (at 0 0 0))\n'
        '    (pin "~" (uuid "p1")) (pin "2" (uuid "p2") (alternate "X"))))\n',
        encoding="utf-8",
    )
    (component,) = SchematicParser(str(path), kicad).get_components()
    pins = [(p["number"], p["name"], p["electrical_type"], p["alternate"]) for p in component.pins]
    assert pins == expected


def test_kicad_version_of_a_netlist(tmp_path):
    from kicad_workbench.parsers.netlist_parser import NetlistParser

    path = tmp_path / "n.xml"
    path.write_text(
        '<export version="E"><design><tool>Eeschema 9.0.3</tool></design></export>',
        encoding="utf-8",
    )
    assert NetlistParser(str(path)).kicad_version == "9.0.3"
    path.write_text('<export version="E"><design/></export>', encoding="utf-8")
    assert NetlistParser(str(path)).kicad_version is None


def test_place_of_a_pin_that_reuses_an_item():
    # One left item and one library pin with no match: the pin takes the place of the
    # item in m_pins (GetPins).
    pins = component_pins('(pin "9") (pin "1") (pin "2")')
    assert [p[0] for p in pins] == ["A_1", "1", "2"]


def test_base_name_of_a_pin_with_an_alternate():
    definition = _definition_pins(parse(DEFINITION), Path("a.kicad_sch"), 20260306)
    symbol = parse('(symbol (lib_id "Lib:U") (unit 1) (pin "1" (alternate "TIM1")))')
    first = _component_pins(symbol, definition, "10.0.6")[0]
    assert (first["name"], first["base_name"]) == ("TIM1", "PA7")


@pytest.mark.parametrize(
    "major, order",
    [(8, ["10", "2", "1"]), (9, ["2", "10", "1"]), (10, ["2", "10", "1"])],
)
def test_library_order(major, order):
    # KiCad 8: unit, body style, number (Cmp); KiCad 9: unit, body style, position (Y
    # inverted), number; KiCad 10: unit, body style, number (StrNumCmp), position.
    from kicad_workbench.parsers.schematic_parser import _library_order

    text = (
        '(symbol "Lib:U" (symbol "U_2_1" (pin passive line (at 0 0 0) (number "1")))'
        ' (symbol "U_1_1" (pin passive line (at 0 0 0) (number "10"))'
        ' (pin passive line (at 0 2.54 0) (number "2"))))'
    )
    definition = _definition_pins(parse(text), Path("a.kicad_sch"), 20260306)
    assert [definition[i]["file_number"] for i in _library_order(definition, major)] == order


STACKED = (
    '(symbol "Lib:S" (symbol "S_1_1"'
    ' (pin bidirectional line (name "E") (number "[5-6]") (alternate "ALT" output line))'
    ' (pin passive line (name "F") (number "[A1-A2,B3]"))'
    ' (pin passive line (name "G") (number "[3-1]"))'
    ' (pin passive line (name "H") (number "4"))))'
)


def stacked_pins(kicad: str | None) -> list[tuple]:
    definition = _definition_pins(parse(STACKED), Path("a.kicad_sch"), 20260306)
    symbol = parse('(symbol (lib_id "Lib:S") (unit 1) (pin "[5-6]" (alternate "ALT")))')
    return [
        (p["number"], p["name"], p["base_name"], p["electrical_type"])
        for p in _component_pins(symbol, definition, kicad)
    ]


def test_stacked_pin_numbers_of_kicad_10():
    # KiCad 10.0 gives the netlist one node for each number of a stacked pin number,
    # with the name and the type of the pin (SCH_PIN::GetStackedPinNumbers). "[3-1]"
    # is not a valid range: one number with the text. The pin of the (pin ...) item is
    # first, then the new pins in the order of the library (_component_pins).
    assert stacked_pins("10.0.7") == [
        ("5", "ALT", "E", "output"),
        ("6", "ALT", "E", "output"),
        ("4", "H", "H", "passive"),
        ("[3-1]", "G", "G", "passive"),
        ("A1", "F", "F", "passive"),
        ("A2", "F", "F", "passive"),
        ("B3", "F", "F", "passive"),
    ]
    # A patch release that the server does not know is a KiCad 10.0 too.
    assert [p[0] for p in stacked_pins("10.0.8")][:2] == ["5", "6"]


@pytest.mark.parametrize("kicad", ["8.0.9", "9.0.9", "10.99.0", None])
def test_stacked_pin_numbers_that_keep_the_text(kicad):
    # KiCad 8 and 9 use the text as one number. A nightly build can have other rules
    # (branch master: UnescapeStackedPinItem), and a version that is not known has no
    # rules: the number keeps its text (the tools refuse these two: placed_instances).
    assert [p[0] for p in stacked_pins(kicad)] == ["[5-6]", "4", "[3-1]", "[A1-A2,B3]"]
