"""Tests for the tool update_symbols_from_library (tools/symbol_update.py) and the rules of
utils/symbol_update.py, from "Update Symbols from Library" of KiCad 10.0.6.
spike/kicad-versions/update_symbols_check.py compares the tool with kicad-cli 10.0.6, and
the roadmap ("Step 10: result") gives the check in the GUI."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kicad_workbench.errors import InputError
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import symbol_update
from kicad_workbench.tools.symbol_update import UpdateOptions, update_symbols_from_library
from kicad_workbench.utils.symbol_update import (
    TextAttributes,
    field_text,
    read_field,
    refdes_number,
    reset_reference,
    wild_compare,
)

from .symbol_tables import row, use_global_table

KICAD10 = "20260306"


def effects(extra=""):
    return f"(effects (font (size 1.27 1.27)){extra})"


def library_symbol(footprint="L:R_0603", mpn="RC0603", pins=("1", "2"), reference="R", units=None):
    """The library symbol R of the library L: one unit with the pins, or the given
    sub-symbols."""
    pin_items = "".join(
        f"(pin passive line (at 0 {3.81 if n == '1' else -3.81} 270) (length 1.27) "
        f'(name "~" {effects()}) (number "{n}" {effects()}))'
        for n in pins
    )
    mpn_field = f'(property "MPN" "{mpn}" (at 0 -2.54 0) (hide yes) {effects()})' if mpn else ""
    unit = f'(symbol "R_1_1" {pin_items})'
    return (
        '(symbol "R" (exclude_from_sim no) (in_bom yes) (on_board yes) '
        f'(property "Reference" "{reference}" (at 2.032 0 90) {effects()}) '
        f'(property "Value" "R" (at 0 0 90) {effects()}) '
        f'(property "Footprint" "{footprint}" (at -1.778 0 90) (hide yes) {effects()}) '
        f'(property "Datasheet" "~" (at 0 0 0) (hide yes) {effects()}) '
        f'(property "Description" "Resistor" (at 0 0 0) (hide yes) {effects()}) '
        f"{mpn_field} "
        f"{units or unit} (embedded_fonts no))"
    )


def write_library(directory: Path, symbol_text: str) -> Path:
    path = directory / "L.kicad_sym"
    path.write_text(
        '(kicad_symbol_lib (version 20251024) (generator "kicad_symbol_editor") '
        f'(generator_version "10.0") {symbol_text})\n',
        encoding="utf-8",
    )
    return path


def prop(name, text, at="100 50 0", hide=False):
    hidden = "\t\t\t(hide yes)\n" if hide else ""
    return (
        f'\t\t(property "{name}" "{text}"\n\t\t\t(at {at})\n{hidden}'
        "\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n"
        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
    )


def placed(reference, value="10k", footprint="", extra="", pins=("1",), autoplaced=False):
    """A placed symbol L:R with the five mandatory fields."""
    pin_items = "".join(f'\t\t(pin "{n}"\n\t\t\t(uuid "p{reference}{n}")\n\t\t)\n' for n in pins)
    autoplace = "\t\t(fields_autoplaced yes)\n" if autoplaced else ""
    return (
        '\t(symbol\n\t\t(lib_id "L:R")\n\t\t(at 100 50 0)\n\t\t(unit 1)\n'
        "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n"
        f"{autoplace}"
        f'\t\t(uuid "u{reference}")\n'
        + prop("Reference", reference, "102 50 0")
        + prop("Value", value, "104 50 0")
        + prop("Footprint", footprint, hide=True)
        + prop("Datasheet", "~", hide=True)
        + prop("Description", "Resistor", hide=True)
        + extra
        + pin_items
        + f'\t\t(instances\n\t\t\t(project "board"\n\t\t\t\t(path "/a0"\n'
        f'\t\t\t\t\t(reference "{reference}")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
    )


# The old copy of L:R in the schematic: no MPN field, one pin.
OLD_COPY = (
    '\t\t(symbol "L:R"\n\t\t\t(property "Reference" "R"\n\t\t\t\t(at 2.032 0 90)\n\t\t\t)\n'
    '\t\t\t(symbol "R_1_1"\n\t\t\t\t(pin passive line\n\t\t\t\t\t(at 0 3.81 270)\n'
    '\t\t\t\t\t(length 1.27)\n\t\t\t\t\t(name "~")\n\t\t\t\t\t(number "1")\n\t\t\t\t)\n'
    "\t\t\t)\n\t\t)\n"
)


def write_schematic(directory: Path, symbols: str, copies: str = OLD_COPY) -> Path:
    path = directory / "board.kicad_sch"
    path.write_text(
        f'(kicad_sch\n\t(version {KICAD10})\n\t(generator "eeschema")\n\t(uuid "a0")\n'
        f"\t(lib_symbols\n{copies}\t)\n{symbols})\n",
        encoding="utf-8",
    )
    (directory / "board.kicad_pro").write_text("{}", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def kicad(monkeypatch, tmp_path):
    """KiCad 10.0.6 with the global library L (the library symbol R)."""

    async def version():
        return "10.0.6"

    monkeypatch.setattr(symbol_update, "get_kicad_version", version)
    library = write_library(tmp_path, library_symbol())
    use_global_table(tmp_path, monkeypatch, row("L", library))
    return library


def symbols(path: Path) -> dict[str, tuple[dict[str, str], list[str]]]:
    """Reference -> (the field texts, the pin numbers) of the placed symbols."""
    found = {}
    for node in parse(path.read_text("utf-8")).children("symbol"):
        fields = {p.value(0): p.value(1) for p in node.children("property")}
        found[fields["Reference"]] = (fields, [p.value(0) for p in node.children("pin")])
    return found


async def update(path: Path, *args, **kwargs) -> str:
    return await update_symbols_from_library(str(path), *args, **kwargs)


class TestRules:
    def test_wild_compare(self):
        assert wild_compare("r*", "R12")
        assert wild_compare("R?2", "r12")
        assert wild_compare("*", "")
        assert wild_compare("U*A*", "U1AB")
        assert not wild_compare("R?", "R12")
        assert not wild_compare("C*", "R1")
        assert not wild_compare("R1", "R12")

    def test_refdes(self):
        assert refdes_number("U12") == 12
        # wxString::ToLong reads the full text from the first digit.
        assert refdes_number("U1A") == -1
        assert refdes_number("R") == -1
        assert reset_reference("IC", "U12") == "IC12"
        assert reset_reference("IC?", "U?") == "IC?"
        assert reset_reference("IC", "U1A") == "IC?"

    def test_the_number_of_a_reference_on_linux(self, long64):
        # static_cast<int> of the long of wxString::ToLong.
        assert refdes_number("R3000000000") == -1294967296
        assert reset_reference("RN", "R3000000000") == "RN?"
        assert reset_reference("RN", "R4294967297") == "RN1"

    def test_the_number_of_a_reference_on_windows(self, long32):
        # A long of 32 bits: ToLong gives false.
        assert refdes_number("R3000000000") == -1
        assert reset_reference("RN", "R4294967297") == "RN?"

    def test_field_read_and_write(self):
        text = (
            '(property "MPN" "X"\n\t(at 1.27 -2.54 90)\n\t(hide yes)\n\t(show_name yes)\n'
            "\t(do_not_autoplace yes)\n\t(effects\n\t\t(font\n\t\t\t(size 1 1.5)\n"
            "\t\t\t(bold yes)\n\t\t\t(color 255 0 0 1)\n\t\t)\n\t\t(justify left bottom)\n"
            '\t\t(href "https://example.com")\n\t)\n)'
        )
        field = read_field(parse(text), "R1")
        assert (field.x, field.y, field.angle) == (12700, -25400, "90")
        assert not field.visible and field.show_name and not field.can_autoplace
        assert field.attributes == TextAttributes(
            height=10000, width=15000, bold=True, color=(255, 0, 0, "1"),
            horizontal="left", vertical="bottom",
        )  # fmt: skip
        assert field.href == "https://example.com"
        assert field_text(field, 0) == text

    def test_a_library_field_has_the_y_axis_up(self):
        node = parse('(property "MPN" "X" (at 1.27 -2.54 0) (effects (font (size 1.27 1.27))))')
        assert read_field(node, "R", library=True).y == 25400

    def test_the_effects_of_each_field(self):
        field = read_field(parse('(property "MPN" "X" (at 0 0 0))'), "R1")
        # TEXT_ATTRIBUTES::m_Multiline: KiCad writes the effects of each field.
        assert "\t(effects\n\t\t(font\n\t\t\t(size 1.27 1.27)\n\t\t)\n\t)\n)" in field_text(
            field, 0
        )


class TestUpdate:
    async def test_the_defaults(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1"))
        out = await update(path)
        assert "✅ The tool updated 1 symbol and wrote `board.kicad_sch`." in out, out
        assert "| R1 | L:R | Footprint updated; MPN added; new pins 2 |" in out, out
        fields, pins = symbols(path)["R1"]
        # The defaults: not the Value, the Footprint and the new user field MPN.
        assert fields["Value"] == "10k"
        assert fields["Footprint"] == "L:R_0603"
        assert fields["MPN"] == "RC0603"
        assert pins == ["1", "2"]
        text = path.read_text("utf-8")
        assert '(uuid "pR11")' in text
        # The new copy of the library symbol, with its name in the schematic.
        copy = parse(text).first("lib_symbols").children("symbol")
        assert [c.value(0) for c in copy] == ["L:R"]
        assert '(property "MPN" "RC0603"' in text[copy[0].start : copy[0].end]
        before = path.read_bytes()
        again = await update(path)
        assert "✅ No change: the matching symbols (1)" in again, again
        assert "| R1 | L:R | no change |" in again, again
        assert path.read_bytes() == before

    async def test_the_matches(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1") + placed("R2", value="1k"))
        # The copy of the schematic is then the library symbol: no conflict of copies.
        assert "✅" in await update(path)
        out = await update(path, "reference", "r2")
        assert "| R2 |" in out and "| R1 |" not in out, out
        out = await update(path, "value", "10K")
        assert "| R1 |" in out and "| R2 |" not in out, out
        out = await update(path, "lib_id", "L:C")
        assert out.startswith("❌ No symbols matching criteria found"), out
        out = await update(path, "value")
        assert out == '❌ The match "value" needs a pattern.'

    async def test_the_rows_in_the_order_of_the_references(self, tmp_path):
        path = write_schematic(tmp_path, placed("R10") + placed("R2") + placed("R1"))
        out = await update(path)
        rows = [line.split(" | ")[0] for line in out.splitlines() if line.startswith("| R")]
        assert rows == ["| R1", "| R2", "| R10"], out

    async def test_a_field_name_in_an_other_case(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1"))
        out = await update(path, fields=["value"])
        assert out == "❌ Give the field name 'Value', not 'value'."

    async def test_the_value_and_the_reference(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(reference="RN"))
        path = write_schematic(tmp_path, placed("R7"))
        out = await update(path, fields=["Reference", "Value"])
        assert "Reference updated; Value updated" in out, out
        fields, _pins = symbols(path)["RN7"]
        assert fields["Value"] == "R"
        # The Footprint is not in the fields of the call.
        assert fields["Footprint"] == ""
        instance = parse(path.read_text("utf-8")).first("symbol").first("instances")
        assert instance.first("project").first("path").first("reference").value(0) == "RN7"

    async def test_remove_extra_fields(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1", extra=prop("Extra", "a", hide=True)))
        await update(path)
        assert symbols(path)["R1"][0]["Extra"] == "a"
        out = await update(path, options=UpdateOptions(remove_extra_fields=True))
        assert "Extra removed" in out, out
        assert "Extra" not in symbols(path)["R1"][0]

    async def test_a_removed_pin(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(pins=("1",)))
        path = write_schematic(tmp_path, placed("R1", pins=("1", "2")))
        out = await update(path)
        assert "pins 2 removed" in out, out
        assert symbols(path)["R1"][1] == ["1"]

    async def test_an_old_copy_that_an_other_symbol_keeps(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1") + placed("R2"))
        before = path.read_bytes()
        out = await update(path, "reference", "R1")
        assert out.startswith("❌ The symbols R2 of board.kicad_sch keep the old copy of L:R"), out
        assert path.read_bytes() == before

    async def test_fields_autoplaced_with_the_positions(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1", autoplaced=True))
        out = await update(path, options=UpdateOptions(update_field_positions=True))
        assert out.startswith("❌ The fields of R1 are placed automatically"), out
        assert "✅" in await update(path)

    async def test_a_missing_mandatory_field(self, tmp_path):
        text = placed("R1").replace(prop("Datasheet", "~", hide=True), "")
        path = write_schematic(tmp_path, text)
        before = path.read_bytes()
        out = await update(path)
        assert out.startswith("❌ The symbol R1 has no field 'Datasheet'"), out
        assert path.read_bytes() == before
        # Also when the field is not in the update: KiCad writes each mandatory field.
        out = await update(path, fields=["Footprint"])
        assert out.startswith("❌ The symbol R1 has no field 'Datasheet'"), out

    async def test_a_symbol_that_the_libraries_do_not_have(self, tmp_path):
        text = placed("R1").replace('(lib_id "L:R")', '(lib_id "L:X")')
        path = write_schematic(tmp_path, text)
        before = path.read_bytes()
        out = await update(path)
        assert out.startswith("❌ R1: KiCad does not find the symbol"), out
        assert path.read_bytes() == before

    async def test_the_attributes(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol().replace("(in_bom yes)", "(in_bom no)"))
        path = write_schematic(tmp_path, placed("R1"))
        await update(path)
        assert "(in_bom yes)" in path.read_text("utf-8")
        out = await update(path, options=UpdateOptions(update_attributes=True))
        assert "in_bom no" in out, out
        node = parse(path.read_text("utf-8")).first("symbol")
        assert node.first("in_bom").value(0) == "no"
        assert node.first("dnp").value(0) == "no"

    def test_the_options_refuse_an_unknown_name(self):
        with pytest.raises(ValidationError):
            UpdateOptions(update_values=True)


class TestUpdatePins:
    """SCH_SYMBOL::UpdatePins in the order of the files."""

    def test_an_unused_pin_takes_the_new_number(self):
        pins = [symbol_update._Pin("1", "a", ""), symbol_update._Pin("2", "b", "")]
        new = [symbol_update._LibPin("1", "", {}), symbol_update._LibPin("3", "", {})]
        result = symbol_update._update_pins(pins, new)
        assert [(p.number, p.uuid) for p in result] == [("1", "a"), ("3", "b")]
        assert result[1] is pins[1]

    def test_new_and_removed_pins(self):
        pins = [symbol_update._Pin("1", "a", "")]
        new = [symbol_update._LibPin("2", "", {}), symbol_update._LibPin("3", "", {})]
        result = symbol_update._update_pins(pins, new)
        # Pin 1 takes the number 2; pin 3 is new, with a new UUID.
        assert [p.number for p in result] == ["2", "3"]
        assert result[0] is pins[0] and result[1].uuid != "a"
        pins = [symbol_update._Pin("1", "a", ""), symbol_update._Pin("2", "b", "")]
        result = symbol_update._update_pins(pins, [symbol_update._LibPin("1", "", {})])
        assert result == [pins[0]]

    def test_an_alternate_with_the_same_type_and_shape(self):
        old = [symbol_update._LibPin("1", "IO", {"A": ("input", "line")})]
        new = [symbol_update._LibPin("1", "IO", {"B": ("input", "line")})]
        # The load gives the pin the alternates of the copy, then the update.
        loaded = symbol_update._update_pins([symbol_update._Pin("1", "a", "A")], old)
        assert loaded[0].alternate == "A"
        assert symbol_update._update_pins(loaded, new)[0].alternate == "B"

    def test_the_load_clears_an_alternate_that_the_copy_does_not_have(self):
        old = [symbol_update._LibPin("1", "IO", {})]
        loaded = symbol_update._update_pins([symbol_update._Pin("1", "a", "A")], old)
        assert loaded[0].alternate == ""


def two_units(alternate="A{n}", kind="input"):
    """The library symbol R with two units; each pin has an alternate."""
    pin = (
        '(pin bidirectional line (at 0 0 0) (length 1.27) (name "P{n}" {e}) (number "{n}" {e}) '
        f'(alternate "{alternate}" {kind} line))'
    )
    units = "".join(f'(symbol "R_{n}_1" {pin.format(n=n, e=effects())})' for n in ("1", "2"))
    return library_symbol(mpn="", units=units)


def copy_of(symbol_text):
    """The copy L:R in (lib_symbols ...) of a library symbol R: the load of the file gives
    the placed pins the pins of the copy (UpdatePins)."""
    return "\t\t" + symbol_text.replace('(symbol "R" ', '(symbol "L:R" ', 1) + "\n"


# The copy "R_1" (SCH_SCREEN::Append: a copy of the schematic only), with the old text.
COPY_1 = OLD_COPY.replace('"R_1_1"', '"R_1_1_1"').replace('"L:R"', '"R_1"')


class TestRepairs:
    async def test_the_alternates_of_the_units_of_the_instances(self, tmp_path, kicad):
        write_library(kicad.parent, two_units())
        text = placed("R1", pins=("1", "2"))
        for n in ("1", "2"):
            text = text.replace(
                f'(uuid "pR1{n}")\n', f'(uuid "pR1{n}")\n\t\t\t(alternate "A{n}")\n'
            )
        path = write_schematic(tmp_path, text, copy_of(two_units()))
        out = await update(path, options=UpdateOptions(reset_alternate_pins=True))
        assert "alternate A1 of pin 1 cleared" in out, out
        # GetPins of the sheet: pin 2 is a pin of unit 2, which no instance has.
        assert "pin 2" not in out
        text = path.read_text("utf-8")
        assert '(alternate "A1")' not in text and '(alternate "A2")' in text

    async def test_no_pins_and_a_new_field(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(pins=()))
        path = write_schematic(tmp_path, placed("R1", pins=("1",)))
        out = await update(path)
        assert "MPN added; pins 1 removed" in out, out
        fields, pins = symbols(path)["R1"]
        assert fields["MPN"] == "RC0603" and pins == []
        assert '(property "MPN" "RC0603"' in path.read_text("utf-8")

    async def test_an_old_copy_that_no_symbol_uses(self, tmp_path):
        text = placed("R1").replace('(lib_id "L:R")', '(lib_name "R_1")\n\t\t(lib_id "L:R")')
        path = write_schematic(tmp_path, text, OLD_COPY + COPY_1)
        before = path.read_bytes()
        out = await update(path)
        assert out.startswith("❌ board.kicad_sch has an old copy of L:R that no symbol uses"), out
        assert path.read_bytes() == before

    async def test_the_only_copy_has_an_other_name(self, tmp_path):
        text = placed("R1").replace('(lib_id "L:R")', '(lib_name "R_1")\n\t\t(lib_id "L:R")')
        path = write_schematic(tmp_path, text, COPY_1)
        out = await update(path)
        assert "✅" in out, out
        root = parse(path.read_text("utf-8"))
        assert [c.value(0) for c in root.first("lib_symbols").children("symbol")] == ["L:R"]
        assert root.first("symbol").first("lib_name") is None

    async def test_a_new_reference_for_an_instance_that_the_load_makes(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(reference="RN"))
        path = write_schematic(tmp_path, placed("R1").replace('(path "/a0"', '(path "/zz"'))
        before = path.read_bytes()
        out = await update(path, fields=["Reference"])
        assert out.startswith("❌ The symbol R1 has no instance for the sheet path /"), out
        assert path.read_bytes() == before

    def test_the_numbers_of_a_field(self):
        text = (
            '(property "MPN" "X" (at 0 0 90.0) (effects (font (size 1.27 1.27) '
            "(line_spacing 1.50) (color 0 0 0 0))))"
        )
        field = read_field(parse(text), "R1")
        # FormatAngle, FormatDouble2Str; the color 0 0 0 0 is COLOR4D::UNSPECIFIED.
        assert field.angle == "90"
        assert field.attributes.line_spacing == "1.5"
        assert field.attributes.color is None
        clamped = read_field(parse(text.replace("(color 0 0 0 0)", "(color 1 2 3 2)")), "R1")
        assert clamped.attributes.color == (1, 2, 3, "1")
        for bad in ("(line_spacing 1_0)", "(line_spacing)", "(color 1 2 3)"):
            with pytest.raises(InputError):
                read_field(parse(text.replace("(line_spacing 1.50)", bad)), "R1")

    async def test_a_field_that_the_dialog_does_not_list(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1"))
        out = await update(path, fields=["mpn"])
        assert out.startswith("❌ The matching symbols and their library symbols have no field"), (
            out
        )
        assert "MPN" in out
        assert "✅" in await update(path, fields=["MPN"])

    async def test_the_reference_of_the_ordinal_instance(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(reference="RN"))
        # The Reference field of a file that KiCad did not save: saveSymbol writes the
        # reference of the ordinal instance.
        text = placed("R7").replace('(property "Reference" "R7"', '(property "Reference" "R9"')
        path = write_schematic(tmp_path, text)
        await update(path, fields=["Reference"])
        root = parse(path.read_text("utf-8")).first("symbol")
        fields = {p.value(0): p.value(1) for p in root.children("property")}
        assert fields["Reference"] == "RN7"

    async def test_new_pins_of_a_symbol_with_no_instances(self, tmp_path):
        text = placed("R1", pins=())
        text = text[: text.index("\t\t(instances")] + "\t)\n"
        path = write_schematic(tmp_path, text)
        out = await update(path)
        assert "new pins 1, 2" in out, out
        source = path.read_text("utf-8")
        node = parse(source).first("symbol")
        assert [p.value(0) for p in node.children("pin")] == ["1", "2"]
        # The pins after the fields, the ")" of the symbol on its own line.
        assert source[node.start : node.end].endswith("\t\t)\n\t)")
        assert node.children()[-1].head == "pin"

    async def test_an_alternate_that_the_new_pin_does_not_have(self, tmp_path, kicad):
        # SCH_PIN::SetAlt in UpdatePins clears it also on a pin of an other unit.
        write_library(kicad.parent, two_units(alternate="B{n}", kind="output"))
        text = placed("R1", pins=("1", "2")).replace(
            '(uuid "pR12")\n', '(uuid "pR12")\n\t\t\t(alternate "A2")\n'
        )
        path = write_schematic(tmp_path, text, copy_of(two_units()))
        assert "✅" in await update(path)
        placed_pins = parse(path.read_text("utf-8")).first("symbol").children("pin")
        assert [pin.first("alternate") for pin in placed_pins] == [None, None]

    async def test_the_canonical_name_and_the_text_of_a_mandatory_field(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(footprint=" L:R_0603 "))
        text = placed("R1").replace('(property "Footprint"', '(property "footprint"')
        path = write_schematic(tmp_path, text)
        out = await update(path)
        assert "| R1 | L:R | Footprint updated;" in out, out
        # saveField writes GetCanonicalName; SCH_FIELD::SetText strips the text.
        assert symbols(path)["R1"][0]["Footprint"] == "L:R_0603"

    async def test_the_output_gives_each_cleared_alternate(self, tmp_path, kicad):
        write_library(kicad.parent, two_units(alternate="B{n}", kind="output"))
        text = placed("R1", pins=("1", "2")).replace(
            '(uuid "pR12")\n', '(uuid "pR12")\n\t\t\t(alternate "A2")\n'
        )
        path = write_schematic(tmp_path, text, copy_of(two_units()))
        out = await update(path)
        assert "alternate A2 of pin 2 cleared" in out, out

    async def test_a_field_with_no_change_keeps_its_text(self, tmp_path):
        # Not in the update set (Value) and no change (Datasheet): the text of the file.
        text = placed("R1").replace('(property "Value" "10k"', '(property "value" " 10k "')
        text = text.replace(prop("Datasheet", "~", hide=True), prop("datasheet", "~", hide=True))
        path = write_schematic(tmp_path, text)
        assert "✅" in await update(path)
        source = path.read_text("utf-8")
        assert '(property "value" " 10k "' in source
        assert '(property "datasheet" "~"' in source

    async def test_a_new_reference_for_a_path_with_no_reference(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(reference="RN"))
        text = placed("R1").replace('\t\t\t\t\t(reference "R1")\n', "")
        path = write_schematic(tmp_path, text)
        before = path.read_bytes()
        out = await update(path, fields=["Reference"])
        assert "has no (reference ...)" in out, out
        assert path.read_bytes() == before

    async def test_the_problems_of_the_library_tables(self, tmp_path, monkeypatch):
        found = symbol_update.find_library_symbol

        def with_problem(*args):
            chain, row, _problems = found(*args)
            return chain, row, ["The project table has a duplicate nickname L."]

        monkeypatch.setattr(symbol_update, "find_library_symbol", with_problem)
        path = write_schematic(tmp_path, placed("R1") + placed("R2"))
        out = await update(path)
        assert out.count("Library table problems (KiCad shows them too)") == 1, out
        assert "duplicate nickname L." in out

    async def test_each_fields_autoplaced_token(self, tmp_path):
        text = placed("R1").replace(
            '\t\t(uuid "uR1")',
            '\t\t(fields_autoplaced no)\n\t\t(fields_autoplaced yes)\n\t\t(uuid "uR1")',
        )
        path = write_schematic(tmp_path, text)
        out = await update(path, options=UpdateOptions(update_field_positions=True))
        assert out.startswith("❌ The fields of R1 are placed automatically"), out

    async def test_the_change_of_an_instance_reference(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(reference="RN"))
        path = write_schematic(tmp_path, placed("R7"))
        out = await update(path, fields=["Reference"])
        assert "reference R7 of / is now RN7" in out, out

    async def test_a_renumbered_pin(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol(pins=("1", "3")))
        path = write_schematic(
            tmp_path, placed("R1", pins=("1", "2")), copy_of(library_symbol(pins=("1", "2")))
        )
        out = await update(path)
        assert "pin 2 is now pin 3" in out, out
        assert '(pin "3"\n\t\t\t(uuid "pR12")' in path.read_text("utf-8")

    async def test_the_count_of_the_changed_symbols(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1") + placed("R2"))
        assert "✅" in await update(path)
        placed_field = '(property "Footprint" "L:R_0603"\n\t\t\t(at 100 50 0)'
        text = path.read_text("utf-8")
        assert text.count(placed_field) == 2
        path.write_text(
            text.replace(placed_field, placed_field.replace("L:R_0603", ""), 1), "utf-8"
        )
        out = await update(path)
        assert (
            "✅ The tool updated 1 symbol and wrote `board.kicad_sch`. 1 matching symbol has no change."
            in out
        ), out
        assert "| R2 | L:R | no change |" in out

    async def test_a_library_flag_that_is_not_yes_or_no(self, tmp_path, kicad):
        write_library(kicad.parent, library_symbol().replace("(in_bom yes)", '(in_bom "no")'))
        path = write_schematic(tmp_path, placed("R1"))
        out = await update(path, options=UpdateOptions(update_attributes=True))
        assert out.startswith('❌ The (in_bom "no") of the library symbol L:R is not yes or no'), (
            out
        )

    async def test_the_library_id_as_kicad_parses_it(self, tmp_path):
        path = write_schematic(tmp_path, placed("R1"))
        assert "| R1 |" in await update(path, "lib_id", "L:R")
        # getLibIdValue: "L" is the library "L" with no item name.
        out = await update(path, "lib_id", "L")
        assert out.startswith("❌ No symbols matching criteria found"), out

    async def test_pins_that_are_not_next_to_each_other(self, tmp_path):
        text = placed("R1", pins=("1", "2"))
        pin_2 = '\t\t(pin "2"\n\t\t\t(uuid "pR12")\n\t\t)\n'
        text = text.replace(pin_2, "").replace(
            "\t\t(instances",
            pin_2.replace("\t\t(pin", "\t\t(dnp no)\n\t\t(pin", 1) + "\t\t(instances",
        )
        path = write_schematic(tmp_path, text)
        out = await update(path)
        assert out.startswith("❌ The pins of R1 are not next to each other"), out

    async def test_a_new_field_of_a_rotated_symbol(self, tmp_path):
        # SCH_FIELD::GetPosition: the offset of the library field, rotated as the symbol.
        text = placed("R1").replace(
            "\t\t(at 100 50 0)\n\t\t(unit 1)", "\t\t(at 100 50 90)\n\t\t(unit 1)"
        )
        path = write_schematic(tmp_path, text)
        assert "MPN added" in await update(path)
        node = parse(path.read_text("utf-8")).first("symbol")
        mpn = next(p for p in node.children("property") if p.value(0) == "MPN")
        # The library field is at (0, -2.54) with the Y axis up: (0, 2.54) in the sheet;
        # the transform of 90 degrees gives (2.54, 0).
        assert mpn.first("at").atoms()[0].text == "102.54"
        assert mpn.first("at").atoms()[1].text == "50"

    async def test_a_private_mandatory_field_of_the_file(self, tmp_path, kicad):
        # parseSchField: a mandatory field is never private, thus not always updated.
        write_library(kicad.parent, library_symbol(reference="RN"))
        text = placed("R1").replace('(property "Reference"', '(property private "Reference"')
        path = write_schematic(tmp_path, text)
        out = await update(path)
        assert "Reference updated" not in out, out
        assert '(property private "Reference" "R1"' in path.read_text("utf-8")

    async def test_fields_that_the_load_changes(self, tmp_path):
        for extra, message in (
            (prop("MPN", "a") + prop("MPN", "b"), "more than one field 'MPN'"),
            (prop("Sim.Enable", "0"), "the field 'Sim.Enable' of an older KiCad"),
        ):
            path = write_schematic(tmp_path, placed("R1", extra=extra))
            out = await update(path)
            assert message in out, out

    async def test_a_library_id_with_a_slash_escape(self, tmp_path):
        text = placed("R1").replace('(lib_id "L:R")', '(lib_id "L:R{slash}1")')
        path = write_schematic(tmp_path, text)
        out = await update(path)
        assert 'has a library ID with "{slash}"' in out, out
