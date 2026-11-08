"""Tests for parsers/symbol_info.py and the tool get_library_symbol."""

import pytest

from kicad_workbench.parsers.kicad_versions import KicadFileError
from kicad_workbench.parsers.lib_symbols import read_library, symbol_chain
from kicad_workbench.parsers.symbol_info import (
    PinAlternate,
    SymbolPin,
    letter_reference,
    str_num_cmp,
    symbol_info,
)
from kicad_workbench.tools import library as library_tools

from .test_library_search import make_tables, table_rows


def library(*symbols: str, version: int = 20251024) -> str:
    return (
        f'(kicad_symbol_lib (version {version}) (generator "test")\n' + "\n".join(symbols) + ")\n"
    )


@pytest.fixture
def info(tmp_path):
    """Write a library and return the symbol_info of one of its symbols."""

    def make(*symbols: str, name: str = "U", version: int = 20251024, kicad: int = 10):
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(library(*symbols, version=version), encoding="utf-8")
        return symbol_info(symbol_chain(read_library(path), name), kicad)

    return make


def pin(number: str, extra: str = "", kind: str = "passive line") -> str:
    return f'(pin {kind} (at 0 0 0) (length 2.54) (name "N{number}") (number "{number}"){extra})'


class TestFieldsAndFlags:
    SYMBOL = """(symbol "U"
        (power local)
        (pin_names (offset 1.016) (hide yes))
        (pin_numbers hide)
        (exclude_from_sim yes) (in_bom no) (on_board no) (in_pos_files no)
        (property "Reference" "U" (at 0 0 0))
        (property "MPN" "X1" (at 0 0 0))
        (property "value" "LM1" (at 0 0 0))
        (property "ki_keywords" "amp op" (at 0 0 0))
        (property "ki_fp_filters" "SOIC*  DIP*{slash}x" (at 0 0 0))
    )"""

    def test_fields(self, info):
        result = info(self.SYMBOL)
        # The mandatory fields first, in the order of their IDs.
        assert result.fields == [
            ("Reference", "U"),
            ("Value", "LM1"),
            ("Footprint", ""),
            ("Datasheet", ""),
            ("Description", ""),
            ("MPN", "X1"),
        ]
        assert result.keywords == "amp op"
        assert result.footprint_filters == ["SOIC*", "DIP*/x"]

    def test_footprint_filters_split_only_at_ascii_blanks(self, info):
        # wxStringTokenizer(" \t\r\n", wxTOKEN_STRTOK): a form feed or a
        # no-break space is part of a filter.
        result = info('(symbol "U" (property "ki_fp_filters" "A\fB C\u00a0D\tE" (at 0 0 0)))')
        assert result.footprint_filters == ["A\fB", "C\u00a0D", "E"]

    def test_flags_and_pin_settings(self, info):
        result = info(self.SYMBOL)
        assert result.power == "local"
        assert (result.in_bom, result.on_board, result.in_pos_files) == (False, False, False)
        assert result.exclude_from_sim
        assert (result.pin_names_shown, result.pin_name_offset) == (False, 10160)
        assert not result.pin_numbers_shown

    def test_defaults(self, info):
        result = info('(symbol "U" (power))')
        assert result.power == "global"
        assert (result.in_bom, result.on_board, result.in_pos_files) == (True, True, True)
        assert not result.exclude_from_sim
        assert (result.pin_names_shown, result.pin_name_offset) == (True, 5080)
        assert result.pin_numbers_shown
        assert (result.unit_count, result.unit_names, result.units_locked) == (1, {1: "A"}, False)
        assert result.body_styles == []

    def test_derived_symbol(self, info):
        root = f'(symbol "R" (in_bom no) (property "Value" "R" (at 0 0 0)) {pin("1")})'
        middle = '(symbol "R_M" (extends "R") (on_board no) (property "Value" "M" (at 0 0 0)))'
        derived = '(symbol "U" (extends "R_M") (in_bom no) (property "MPN" "X" (at 0 0 0)))'
        result = info(root, middle, derived)
        # The flags of the parent, the pins of the root.
        assert (result.in_bom, result.on_board) == (True, False)
        assert ("Value", "M") in result.fields and ("MPN", "X") in result.fields
        assert [p.number for p in result.pins] == ["1"]


class TestUnits:
    def test_unit_names_and_lock(self, info):
        result = info(
            """(symbol "U" (property "ki_locked" "" (at 0 0 0))
            (symbol "U_1_1" (unit_name "Gate"))
            (symbol "U_2_1" (unit_name ""))
            (symbol "U_3_1")
            (symbol "U_0_1"))"""
        )
        assert result.unit_count == 3
        # An empty name is a name (GetUnitDisplayName).
        assert result.unit_names == {1: "Gate", 2: "", 3: "C"}
        assert result.units_locked

    @pytest.mark.parametrize("unit, letters", [(1, "A"), (26, "Z"), (27, "AA"), (52, "AZ")])
    def test_letters(self, unit, letters):
        assert letter_reference(unit) == letters


class TestBodyStyles:
    ALTERNATE = f'(symbol "U_1_1" {pin("1")}) (symbol "U_1_2" {pin("2")})'

    def test_old_file_finds_de_morgan_in_the_drawings(self, info):
        result = info(f'(symbol "U" {self.ALTERNATE})', version=20241209)
        assert result.body_styles == ["Standard", "Alternate"]
        assert [(p.number, p.body_style) for p in result.pins] == [("1", 1), ("2", 2)]

    def test_old_file_with_an_empty_alternate(self, info):
        result = info('(symbol "U" (symbol "U_1_2" (unit_name "X")))', version=20241209)
        assert result.body_styles == []

    def test_new_file_without_declaration_removes_the_alternate(self, info):
        result = info(f'(symbol "U" {self.ALTERNATE})')
        assert result.body_styles == []
        assert [p.number for p in result.pins] == ["1"]

    def test_declared_styles(self, info):
        result = info(f'(symbol "U" (body_styles demorgan) {self.ALTERNATE})')
        assert result.body_styles == ["Standard", "Alternate"]
        result = info(f'(symbol "U" (body_styles "A" "B" "C") {self.ALTERNATE})')
        assert result.body_styles == ["A", "B", "C"]
        assert len(result.pins) == 2


class TestPins:
    def test_angle_out_of_the_range_of_an_int(self, info, long64):
        # parseInt: (int)strtol. Measured with kicad-cli 8.0.9 to 10.0.7 on Linux:
        # "4294967386" (2**32 + 90) is 90 (spike/kicad-versions/c_numbers_check.py).
        result = info('(symbol "U" (pin input line (at 0 0 4294967386) (number "1")))')
        assert result.pins[0].orientation == "up"

    def test_angle_out_of_the_range_of_a_long(self, info, long32):
        # Windows (a long of 32 bits): strtol gives LONG_MAX, not an angle of KiCad.
        with pytest.raises(KicadFileError, match="the angle 4294967386"):
            info('(symbol "U" (pin input line (at 0 0 4294967386) (number "1")))')

    def test_pin(self, info):
        result = info(
            """(symbol "U" (symbol "U_0_1"
              (pin unconnected inverted (at 1.27 -2.54 90) (length 5.08) hide
                (name "CLK" (effects (font (size 1.27 1.27))))
                (number "7" (effects (font (size 1.27 1.27))))
                (alternate "TX" output line) (alternate "RX" input clock))
              (pin input line (at 0 0 270.0) (hide yes) (name "") (number "8"))
              (pin input line (number "9"))))"""
        )
        assert result.pins == [
            SymbolPin(
                "7",
                "CLK",
                "no_connect",
                "inverted",
                0,
                1,
                12700,
                -25400,
                "up",
                50800,
                True,
                # KiCad keeps the alternates sorted by name.
                (PinAlternate("RX", "input", "clock"), PinAlternate("TX", "output", "line")),
            ),
            SymbolPin("8", "", "input", "line", 0, 1, 0, 0, "down", 25400, True, ()),
            # The defaults of SCH_PIN.
            SymbolPin("9", "", "input", "line", 0, 1, 0, 0, "right", 25400, False, ()),
        ]

    def test_order_of_kicad(self, info):
        # SCH_PIN::compare: unit, body style, number (StrNumCmp), position.
        result = info(
            f"""(symbol "U" (body_styles demorgan)
            (symbol "U_2_1" {pin("1")})
            (symbol "U_1_2" {pin("1")})
            (symbol "U_1_1" {pin("10")} {pin("9")} {pin("A2")} {pin("A10")}
              (pin input line (at 0 1 0) (number "B"))
              (pin input line (at 0 2 0) (number "B"))))"""
        )
        assert [(p.unit, p.body_style, p.number, p.y) for p in result.pins] == [
            (1, 1, "9", 0),
            (1, 1, "10", 0),
            (1, 1, "A2", 0),
            (1, 1, "A10", 0),
            # KiCad has the Y axis down.
            (1, 1, "B", 20000),
            (1, 1, "B", 10000),
            (1, 2, "1", 0),
            (2, 1, "1", 0),
        ]

    @pytest.mark.parametrize(
        "first, second, result",
        [
            ("9", "10", -1),
            ("A10", "A9", 1),
            ("01", "1", 0),
            ("1a", "1b", -1),
            ("a", "B", 1),
            ("12", "12a", -1),
            ("", "1", -1),
        ],
    )
    def test_str_num_cmp(self, first, second, result):
        assert str_num_cmp(first, second) == result
        assert str_num_cmp(second, first) == -result

    def test_pin_outside_a_sub_symbol_is_in_unit_1(self, info):
        result = info(f'(symbol "U" {pin("1")})')
        assert (result.pins[0].unit, result.pins[0].body_style) == (1, 1)

    @pytest.mark.parametrize("version, name", [(20241209, ""), (20251024, "~")])
    def test_tilde_is_an_empty_text_in_an_old_file(self, info, version, name):
        result = info('(symbol "U" (pin input line (name "~") (number "~")))', version=version)
        assert (result.pins[0].name, result.pins[0].number) == (name, name)

    def test_pin_number_has_no_spaces(self, info):
        # SCH_PIN::SetNumber replaces each space (not a tab) with "_".
        result = info('(symbol "U" (pin input line (name "A B") (number " 1 2\t")))')
        assert (result.pins[0].name, result.pins[0].number) == ("A B", "_1_2\t")

    @pytest.mark.parametrize(
        "item, message",
        [
            ("(pin inout line)", "the electrical type 'inout'"),
            ("(pin input round)", "the graphic shape 'round'"),
            ('(pin input line (alternate "A" input round))', "the graphic shape 'round'"),
            ("(pin input line (at 0 0 45))", "the angle 45, not 0, 90, 180 or 270"),
            ("(pin input line (at 0 x 0))", "not a number"),
            ("(pin input line (hide maybe))", "(hide maybe), not yes or no"),
            ("(pin_names (hide))", "(hide ), not yes or no"),
            ("(pin_numbers (hide maybe))", "(hide maybe), not yes or no"),
            ('(symbol "U_A_1")', "Invalid symbol unit number A_1"),
            ("(jumper_pin_groups (1 2))", "a jumper pin group"),
        ],
    )
    def test_item_that_kicad_cannot_load(self, info, item, message):
        with pytest.raises(KicadFileError, match=message.replace("(", r"\(").replace(")", r"\)")):
            info(f'(symbol "U" {item})')

    def test_jumpers(self, info):
        result = info(
            '(symbol "U" (duplicate_pin_numbers_are_jumpers yes)'
            ' (jumper_pin_groups ("1" "2") ("3" "4")))'
        )
        assert result.duplicate_pins_are_jumpers
        assert result.jumper_pin_groups == [["1", "2"], ["3", "4"]]


@pytest.fixture
def tables(tmp_path, monkeypatch):
    return make_tables(tmp_path, monkeypatch)


def set_version(monkeypatch, version: str) -> None:
    async def kicad_version():
        return version

    monkeypatch.setattr(library_tools, "get_kicad_version", kicad_version)


class TestTool:
    async def test_symbol_of_the_project(self, tables):
        out = await library_tools.get_library_symbol("Device:R", str(tables["project"]))
        assert out.startswith("# Library symbol Device:R")
        assert f"**Library**: Device (project): {tables['project'] / 'Device.kicad_sym'}" in out
        assert "| Description | Resistor of the project |" in out

    async def test_symbol_of_the_global_table(self, tables):
        (tables["libs"] / "Device.kicad_sym").write_text(
            library(
                '(symbol "R" (property "ki_keywords" "R res" (at 0 0 0))'
                ' (symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27)'
                ' (name "~") (number "1"))))',
                version=20241209,
            ),
            encoding="utf-8",
        )
        out = await library_tools.get_library_symbol("Device:R")
        assert "**File version**: 20241209" in out
        assert "**Keywords**: R res" in out
        pins = out.split("## Pins (1)", 1)[1]
        assert table_rows(pins) == [
            ["1", "", "passive", "line", "1", "1", "0", "3.81", "down", "1.27", "", ""]
        ]

    @pytest.mark.parametrize(
        "lib_id, message",
        [
            ("R", 'is not "<library>:<symbol>"'),
            ("Device:Q", 'has no symbol "Q"'),
            ("Nothing:R", 'no symbol library "Nothing" is in the global library table'),
            ("Off:ROff", "disabled"),
        ],
    )
    async def test_errors(self, tables, lib_id, message):
        out = await library_tools.get_library_symbol(lib_id)
        assert out.startswith("❌") and message in out, out

    @pytest.mark.parametrize(
        "version, message",
        [
            (20230121, "older than the version of KiCad 8.0"),
            (20260622, "newer than the version of KiCad 10.0"),
        ],
    )
    async def test_library_versions_without_rules(self, tables, version, message):
        (tables["libs"] / "Device.kicad_sym").write_text(
            library('(symbol "R")', version=version), encoding="utf-8"
        )
        out = await library_tools.get_library_symbol("Device:R")
        assert out.startswith("❌ Cannot read the symbol Device:R:") and message in out, out

    @pytest.mark.file_versions
    async def test_library_newer_than_the_installed_kicad(self, tables, monkeypatch):
        (tables["libs"] / "Device.kicad_sym").write_text(
            library('(symbol "R")', version=20251024), encoding="utf-8"
        )
        (tables["libs"].parent / "config" / "9.0").mkdir()
        (tables["libs"].parent / "config" / "9.0" / "sym-lib-table").write_text(
            (tables["libs"].parent / "config" / "10.0" / "sym-lib-table").read_text("utf-8"),
            encoding="utf-8",
        )
        set_version(monkeypatch, "9.0.9")
        out = await library_tools.get_library_symbol("Device:R")
        assert out.startswith("❌ Cannot read the symbol Device:R: The symbol library "), out
        assert "of a newer file version than KiCad 9.0.9 writes (20241209)" in out
        assert "KiCad 9.0.9 cannot read them. Tell the user: use the library in the KiCad" in out
        assert "or install KiCad 10.0 or later" in out

    @pytest.mark.file_versions
    async def test_a_library_before_kicad_8_gives_only_the_error(self, tables, monkeypatch):
        library_file = tables["libs"] / "Device.kicad_sym"
        library_file.write_text(library('(symbol "R")', version=20211014), encoding="utf-8")
        set_version(monkeypatch, "10.0.6")
        out = await library_tools.get_library_symbol("Device:R")
        assert out.startswith("❌ Cannot read the symbol Device:R: The symbol library file"), out
        assert "⚠️" not in out and f"run `kicad-cli sym upgrade {library_file}`" in out

    @pytest.mark.file_versions
    async def test_an_older_library_gives_a_warning(self, tables, monkeypatch):
        library_file = tables["libs"] / "Device.kicad_sym"
        library_file.write_text(library('(symbol "R")', version=20241209), encoding="utf-8")
        set_version(monkeypatch, "10.0.6")
        out = await library_tools.get_library_symbol("Device:R")
        assert out.startswith(
            f"⚠️ The symbol library {library_file} has files of an older file version than "
            f"KiCad 10.0.6 writes (20251024): {library_file} (version 20241209). The tool "
            "reads them as KiCad 10.0.6 loads them, and does not change the library. To convert the "
            f"library, tell the user: run `kicad-cli sym upgrade {library_file}` of KiCad 10.0.6."
        ), out
        assert "\n\n# Library symbol Device:R" in out

    @pytest.mark.parametrize("version", ["9.99.0", "11.0.0", "10.1.0"])
    async def test_installed_kicad_without_rules(self, tables, monkeypatch, version):
        set_version(monkeypatch, version)
        out = await library_tools.get_library_symbol("Device:R")
        assert out.startswith(f"❌ The installed KiCad is {version}."), out

    async def test_nightly_build_of_master(self, tables, monkeypatch):
        (tables["config"] / "10.99").mkdir()
        (tables["config"] / "10.99" / "sym-lib-table").write_text(
            (tables["config"] / "10.0" / "sym-lib-table").read_text("utf-8"), encoding="utf-8"
        )
        set_version(monkeypatch, "10.99.0")
        out = await library_tools.get_library_symbol("Device:R")
        assert out.startswith("# Library symbol Device:R"), out
