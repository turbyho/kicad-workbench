"""Tests for the field rules of each KiCad version (parsers/kicad_fields.py).

The expected values come from the parsers of the KiCad branches 8.0, 9.0 and
10.0 (parseSchField, parseSchematicSymbol, parseSheet and
SCH_SHEET::SetFields).
"""

import asyncio

import pytest

from kicad_workbench.errors import KicadParseError
from kicad_workbench.parsers.kicad_fields import (
    FieldRules,
    record_field_rules,
    rules_for_file,
    sheet_fields,
    symbol_fields,
    with_field_rules_note,
)
from kicad_workbench.parsers.kicad_versions import KicadFileError
from kicad_workbench.parsers.schematic_parser import SchematicParser
from kicad_workbench.parsers.sexpr import parse, property_field

KICAD_9 = FieldRules(9, 20250114)
KICAD_10 = FieldRules(10, 20260306)


def item(head: str, *props: str) -> object:
    return parse(f"({head} " + " ".join(f"(property {prop})" for prop in props) + ")")


class TestVersions:
    @pytest.mark.parametrize(
        "version, kicad",
        [
            # KiCad 8.0, a nightly build of KiCad 9, and KiCad 9.0.
            (20231120, 9),
            (20240130, 9),
            (20250114, 9),
            # A nightly build of KiCad 10, KiCad 10.0, and the newest known
            # nightly build of KiCad 11.
            (20250318, 10),
            (20260306, 10),
            (20261025, 10),
        ],
    )
    def test_version_of_the_file(self, version, kicad):
        # The KiCad that loads the file is not known: the KiCad that saved it.
        root = parse(f'(kicad_sch (version {version}) (generator "eeschema"))')
        assert rules_for_file(root, None) == FieldRules(kicad, version)

    @pytest.mark.parametrize(
        "version, installed, kicad",
        [
            # Each KiCad reads the fields of an older file with its own rules.
            (20231120, 8, 9),
            (20231120, 9, 9),
            (20231120, 10, 10),
            (20250114, 10, 10),
            (20250114, 11, 10),
            (20261025, 10, 10),
        ],
    )
    def test_the_rules_of_the_installed_kicad(self, version, installed, kicad):
        root = parse(f'(kicad_sch (version {version}) (generator "eeschema"))')
        assert rules_for_file(root, installed) == FieldRules(kicad, version)

    def test_a_kicad_before_kicad_8_has_no_rules(self):
        root = parse('(kicad_sch (version 20231120) (generator "eeschema"))')
        with pytest.raises(KicadFileError, match="KiCad 7 has no field rules"):
            rules_for_file(root, 7)

    @pytest.mark.parametrize(
        "header, message",
        [
            ("", "has no file version"),
            # A digit that is not ASCII is not a version.
            ("(version 2025011²)", "has no file version"),
            # KiCad 7.0, and a nightly build of KiCad 8.
            ("(version 20230121)", r"file version 20230121, which is older than .* \(20231120\)"),
            ("(version 20230819)", r"file version 20230819, which is older than .* \(20231120\)"),
            ("(version 20261026)", "newer than the newest version that this server knows"),
        ],
    )
    def test_version_that_the_server_cannot_read(self, header, message):
        with pytest.raises(KicadFileError, match=message):
            rules_for_file(parse(f"(kicad_sch {header})"), 10)

    def test_note(self):
        assert FieldRules(9, 20231120).note() == (
            "Fields read with the rules of KiCad 8 and 9 (file version 20231120)."
        )
        assert KICAD_10.note() == (
            "Fields read with the rules of KiCad 10 (file version 20260306)."
        )


@pytest.mark.parametrize(
    "text, field",
    [
        ('(property "Value" "1k")', ("Value", "1k")),
        ('(property private "MPN" "RC0603")', ("MPN", "RC0603")),
        # A quoted "private" is a name.
        ('(property "private" "x")', ("private", "x")),
        ('(property "Empty")', ("Empty", "")),
        ("(property private)", None),
    ],
)
def test_property_field(text, field):
    assert property_field(parse(text)) == field


class TestSymbolFields:
    def test_private_field(self):
        symbol = item("symbol", 'private "MPN" "RC0603"', '"Reference" "R1"', '"Value" "1k"')
        for rules in (KICAD_9, KICAD_10):
            assert symbol_fields(symbol, rules) == {"Reference": "R1", "Value": "1k"}

    def test_case_of_the_name(self):
        symbol = item("symbol", '"reference" "R7"', '"VALUE" "2k2"')
        # KiCad 10 compares with no case (CmpNoCase), KiCad 8 and 9 exactly.
        assert symbol_fields(symbol, KICAD_10) == {"Reference": "R7", "Value": "2k2"}
        assert symbol_fields(symbol, KICAD_9) == {}

    def test_later_mandatory_field_wins(self):
        symbol = item("symbol", '"Value" "1k"', '"Value" "2k2"')
        for rules in (KICAD_9, KICAD_10):
            assert symbol_fields(symbol, rules) == {"Value": "2k2"}

    def test_id_of_a_user_field_in_kicad_9(self):
        # KiCad 8 and 9 use an (id N) that is not a mandatory ID: the field is
        # a user field then. KiCad 10 ignores the ID.
        symbol = item("symbol", '"Value" "1k" (id 7)', '"Footprint" "F" (id 2)')
        assert symbol_fields(symbol, KICAD_9) == {"Footprint": "F"}
        assert symbol_fields(symbol, KICAD_10) == {"Value": "1k", "Footprint": "F"}


class TestSheetFields:
    def test_kicad_10_first_field_with_no_case(self):
        sheet = item("sheet", '"SHEETNAME" "Power"', '"Sheetfile" "p.kicad_sch"', '"Sheetname" "B"')
        assert sheet_fields(sheet, KICAD_10) == {"Sheetname": "Power", "Sheetfile": "p.kicad_sch"}

    def test_kicad_9_duplicate_gets_the_next_id(self):
        # The second "Sheetname" gets the ID of the sheet file.
        sheet = item("sheet", '"Sheetname" "A"', '"Sheetname" "B"', '"Sheetfile" "f.kicad_sch"')
        assert sheet_fields(sheet, KICAD_9) == {"Sheetname": "A", "Sheetfile": "B"}
        assert sheet_fields(sheet, KICAD_10) == {"Sheetname": "A", "Sheetfile": "f.kicad_sch"}

    def test_kicad_6_names(self):
        sheet = item("sheet", '"Sheet name" "Power"', '"Sheet file" "p.kicad_sch"')
        for rules in (KICAD_9, KICAD_10):
            assert sheet_fields(sheet, rules) == {"Sheetname": "Power", "Sheetfile": "p.kicad_sch"}

    def test_kicad_9_reads_the_fields_by_index(self):
        # Two user fields: SetFields sorts them and reads the first two.
        sheet = item("sheet", '"X" "Power"', '"Y" "p.kicad_sch"')
        assert sheet_fields(sheet, KICAD_9) == {"Sheetname": "Power", "Sheetfile": "p.kicad_sch"}
        # KiCad 10: a parse error (SCH_IO_KICAD_SEXPR_PARSER::parseSheet).
        with pytest.raises(KicadParseError, match="Missing sheet name property"):
            sheet_fields(sheet, KICAD_10)

    def test_kicad_10_missing_sheet_file(self):
        # SCH_SHEET::SetFields adds an empty file field: no sub-sheet, no error
        # (measured with kicad-cli 10.0.6 and 10.0.7).
        sheet = item("sheet", '"Sheetname" "Power"', '"Note" "x"')
        assert sheet_fields(sheet, KICAD_10) == {"Sheetname": "Power", "Sheetfile": ""}

    def test_kicad_9_needs_two_fields(self):
        sheet = item("sheet", '"Sheetname" "Power"')
        with pytest.raises(KicadFileError, match="KiCad 8 and 9 need two"):
            sheet_fields(sheet, KICAD_9)


SCH = """(kicad_sch
	(version 20250114)
	(generator "eeschema")
	(generator_version "9.0")
	(lib_symbols)
	(symbol (lib_id "Device:R") (at 0 0 0) (unit 1)
		(property "Reference" "R1" (at 0 0 0))
		(property "Value" "1k" (at 0 0 0))
		(property "value" "lower case" (at 0 0 0))
	)
)
"""


class TestSchematicParser:
    def write(self, tmp_path, text: str) -> str:
        path = tmp_path / "board.kicad_sch"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_kicad_9_file(self, tmp_path):
        parser = SchematicParser(self.write(tmp_path, SCH))
        assert parser.field_rules == KICAD_9
        # "value" is a user field in KiCad 8 and 9.
        assert parser.get_components()[0].value == "1k"

    def test_kicad_10_file(self, tmp_path):
        text = SCH.replace("20250114", "20260306").replace('"9.0"', '"10.0"')
        parser = SchematicParser(self.write(tmp_path, text))
        assert parser.field_rules == KICAD_10
        # KiCad 10 reads "value" as the Value field; the later field wins.
        assert parser.get_components()[0].value == "lower case"

    def test_old_file_is_an_error(self, tmp_path):
        path = self.write(tmp_path, SCH.replace("20250114", "20211123"))
        with pytest.raises(
            KicadFileError, match=r"board\.kicad_sch: .*older than the version of KiCad 8\.0"
        ):
            SchematicParser(path).get_components()


class TestNote:
    @pytest.fixture
    def path(self, tmp_path):
        path = tmp_path / "board.kicad_sch"
        path.write_text(SCH, encoding="utf-8")
        return str(path)

    async def test_note_in_the_output(self, path):
        @with_field_rules_note
        async def tool():
            # A parser in a worker thread records its rules too.
            await asyncio.to_thread(lambda: SchematicParser(path).get_components())
            SchematicParser(path).get_components()
            return "out"

        assert await tool() == (
            "out\n\n_Fields read with the rules of KiCad 8 and 9 (file version 20250114)._"
        )

    async def test_no_note_when_no_parser_reads_fields(self, path):
        @with_field_rules_note
        async def tool():
            return "out"

        assert await tool() == "out"

    async def test_nested_tool_adds_no_note(self, path):
        @with_field_rules_note
        async def inner():
            SchematicParser(path).get_components()
            return "inner"

        @with_field_rules_note
        async def outer():
            return f"outer {await inner()}"

        assert await outer() == (
            "outer inner\n\n_Fields read with the rules of KiCad 8 and 9 (file version 20250114)._"
        )

    def test_record_outside_a_tool_call(self):
        record_field_rules(KICAD_10)

    async def test_parser_used_before_the_tool_call(self, path):
        parser = SchematicParser(path)
        parser.get_components()

        @with_field_rules_note
        async def tool():
            # The parser gives the data that it read before the call.
            parser.get_components()
            return "out"

        assert await tool() == (
            "out\n\n_Fields read with the rules of KiCad 8 and 9 (file version 20250114)._"
        )


SUB_SCH = """(kicad_sch
	(version 20260306)
	(generator "eeschema")
	(generator_version "10.0")
	(lib_symbols)
	(symbol (lib_id "Device:R") (at 0 0 0) (unit 1)
		(property "Reference" "R2" (at 0 0 0))
		(property "value" "only KiCad 10 reads this" (at 0 0 0))
	)
)
"""


def write_hierarchy(tmp_path, sub_sheet: str) -> str:
    root = SCH.replace(
        "(lib_symbols)",
        "(lib_symbols)\n\t(sheet (at 0 0) (size 10 10)\n"
        '\t\t(property "Sheetname" "Sub" (at 0 0 0))\n'
        '\t\t(property "Sheetfile" "sub.kicad_sch" (at 0 0 0)))',
    )
    (tmp_path / "board.kicad_sch").write_text(root, encoding="utf-8")
    (tmp_path / "sub.kicad_sch").write_text(sub_sheet, encoding="utf-8")
    return str(tmp_path / "board.kicad_sch")


async def test_each_sheet_uses_the_rules_of_its_file(tmp_path):
    path = write_hierarchy(tmp_path, SUB_SCH)

    @with_field_rules_note
    async def tool():
        sub = str(tmp_path / "sub.kicad_sch")
        components = SchematicParser(path).get_components() + SchematicParser(sub).get_components()
        return str({c.reference: c.value for c in components})

    assert await tool() == (
        str({"R1": "1k", "R2": "only KiCad 10 reads this"})
        + "\n\n_Fields read with the rules of KiCad 8 and 9 (file version 20250114)._"
        + "\n_Fields read with the rules of KiCad 10 (file version 20260306)._"
    )


def test_sub_sheet_of_an_old_version_is_an_error(tmp_path):
    write_hierarchy(tmp_path, SUB_SCH.replace("20260306", "20230121"))
    with pytest.raises(
        KicadFileError, match=r"sub\.kicad_sch: .*older than the version of KiCad 8\.0"
    ):
        SchematicParser(str(tmp_path / "sub.kicad_sch")).get_components()
