"""Tests for the writes in the format of the file version (parsers/kicad_versions.py).

The schematic tools add items to a schematic, and they do not change its file
version. spike/kicad-versions/write_check.py loads the results in KiCad 8, 9
and 10.
"""

import asyncio

import pytest

from kicad_workbench.parsers.kicad_versions import (
    file_version,
    header_version,
    release_of_schematic,
    release_of_symbol_library,
    symbol_library_problem,
)
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import schematic_editor

from .installed_kicad import write_schematic
from .symbol_tables import row, use_global_table

# A symbol with the tokens of KiCad 10 that the old code removed.
LIBRARY = """(kicad_symbol_lib
\t(version {version})
\t(generator "kicad_symbol_editor")
\t(symbol "R"
\t\t(duplicate_pin_numbers_are_jumpers yes)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(in_pos_files no)
\t\t(property "Reference" "R"
\t\t\t(at 0 0 0)
\t\t\t(show_name yes)
\t\t\t(do_not_autoplace yes)
\t\t\t(effects (font (size 1.27 1.27){font}))
\t\t)
\t\t(symbol "R_1_1"
\t\t\t(pin passive line
\t\t\t\t(at 0 3.81 270)
\t\t\t\t(length 1.27)
\t\t\t\t(name "{pin_name}" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t\t)
\t\t){sub_symbols}
\t)
)
"""


def library_text(version: int, pin_name: str = "", font: str = "", sub_symbols: str = "") -> str:
    return LIBRARY.format(version=version, pin_name=pin_name, font=font, sub_symbols=sub_symbols)


def symbol(**parts) -> object:
    """Return the (symbol "R" ...) of a test library."""
    return parse(library_text(20251024, **parts)).first("symbol")


# A sub-symbol of the alternate body style (De Morgan).
DE_MORGAN = '\n\t\t(symbol "R_1_2" (rectangle (start 0 0) (end 1 1)))'
BOLD = " (thickness 0.3) (bold yes)"
# A bare token with "|". KiCad writes no such token, but a file can have it.
BARE_BAR = "\n\t\t(extra a|b)"


class TestVersions:
    @pytest.mark.parametrize(
        "version, release",
        [
            (20231120, "8.0"),
            # A nightly build of KiCad 9 reads the files of KiCad 8.
            (20240130, "8.0"),
            (20250114, "9.0"),
            (20250318, "9.0"),
            (20260306, "10.0"),
            (20261025, "10.0"),
        ],
    )
    def test_release_of_schematic(self, version, release):
        assert release_of_schematic(version).name == release

    @pytest.mark.parametrize(
        "version, release",
        [(20231120, "8.0"), (20240529, "9.0"), (20241209, "9.0"), (20251024, "10.0")],
    )
    def test_release_of_symbol_library(self, version, release):
        assert release_of_symbol_library(version).name == release

    @pytest.mark.parametrize(
        "text, head, version",
        [
            ("(kicad_sch (version 20250114) (generator x))", "kicad_sch", 20250114),
            ("# comment\n  (kicad_sch\n\t(version 20260306)", "kicad_sch", 20260306),
            # KiCad reads the version only as the first item (parseHeader).
            ("(kicad_sch (generator x) (version 20250114))", "kicad_sch", None),
            ("(kicad_sch (version 2025011\u00b2))", "kicad_sch", None),
            ("(kicad_symbol_lib (version 20231120)", "kicad_sch", None),
            ("(kicad_symbol_lib (version 20231120)", "kicad_symbol_lib", 20231120),
            # A head ends at "(", a comment line can be in the header.
            ("(kicad_sch(version 20250114)", "kicad_sch", 20250114),
            ("(kicad_sch\n# note\n(version 20250114)", "kicad_sch", 20250114),
            ('(kicad_sch (version "20250114"))', "kicad_sch", 20250114),
            # "#" is a comment only at the start of a line.
            ("(kicad_sch # note\n(version 20250114)", "kicad_sch", None),
        ],
    )
    def test_header_version(self, text, head, version):
        assert header_version(text, head) == version

    def test_file_version_is_the_first_item(self):
        assert file_version(parse("(kicad_sch (version 20250114))")) == 20250114
        assert file_version(parse("(kicad_sch (generator x) (version 20250114))")) is None

    @pytest.mark.parametrize(
        "library, schematic",
        [
            # The library and the schematic of one release.
            (20231120, 20231120),
            (20241209, 20250114),
            (20251024, 20260306),
            # A nightly build of KiCad 11 (branch master, 2026-11-02).
            (20261015, 20261025),
            # The symbol has none of the items that the conversions change.
            (20231120, 20250114),
            (20231120, 20260306),
            (20251024, 20261025),
        ],
    )
    def test_symbol_fits(self, library, schematic):
        assert symbol_library_problem(symbol(), library, schematic) is None

    @pytest.mark.parametrize(
        "library, schematic, messages",
        [
            (None, 20250114, ["has no file version"]),
            (20220914, 20231120, ["older than the version of KiCad 8.0"]),
            (20241209, 20231120, ["newer than the schematic", "cannot read the symbol"]),
            (20251024, 20250114, ["newer than the schematic"]),
        ],
    )
    def test_version_does_not_fit(self, library, schematic, messages):
        problem = symbol_library_problem(symbol(), library, schematic)
        assert problem is not None
        for message in messages:
            assert message in problem

    @pytest.mark.parametrize(
        "parts, library, schematic, message",
        [
            ({"pin_name": "~"}, 20231120, 20260306, "before 20250318"),
            ({"pin_name": "~"}, 20241209, 20260306, '"~"'),
            ({"sub_symbols": DE_MORGAN}, 20241209, 20260306, "before 20250827"),
            ({"font": BOLD}, 20251024, 20261025, "before 20260826"),
            ({"sub_symbols": BARE_BAR}, 20231120, 20250114, '"|"'),
        ],
    )
    def test_symbol_with_a_converted_item(self, parts, library, schematic, message):
        problem = symbol_library_problem(symbol(**parts), library, schematic)
        assert problem is not None and message in problem

    @pytest.mark.parametrize(
        "parts, library, schematic",
        [
            # Both files are before 20250318: KiCad 8 and 9 show "~" as empty.
            ({"pin_name": "~"}, 20231120, 20250114),
            # A bold text with no line width: the conversion changes nothing.
            ({"font": " (bold yes)"}, 20251024, 20261025),
            # The bar of a quoted string is text.
            ({"pin_name": "A|B"}, 20231120, 20250114),
        ],
    )
    def test_conversion_that_does_not_change_the_symbol(self, parts, library, schematic):
        assert symbol_library_problem(symbol(**parts), library, schematic) is None


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Give add_component_from_library a library "Device" of a file version."""

    def make(version: int, **parts):
        path = tmp_path / "Device.kicad_sym"
        path.write_text(library_text(version, **parts), encoding="utf-8")
        use_global_table(tmp_path, monkeypatch, row("Device", path))
        return path

    return make


def add_resistor(path) -> str:
    return asyncio.run(
        schematic_editor.add_component_from_library(str(path), "Device", "R", "R1", "1k")
    )


class TestComponent:
    def test_symbol_of_the_same_release(self, tmp_path, library):
        library(20251024)
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add_resistor(sch)
        content = sch.read_text(encoding="utf-8")
        assert '(symbol "Device:R"' in content
        # KiCad writes these tokens in a schematic of KiCad 10 too.
        for token in (
            "(duplicate_pin_numbers_are_jumpers yes)",
            "(in_pos_files no)",
            "(show_name yes)",
            "(do_not_autoplace yes)",
        ):
            assert token in content
        assert "(version 20260306)" in content

    @pytest.mark.parametrize(
        "library_version, parts, schematic_version, advice",
        [
            (20251024, {}, 20250114, "Open the schematic in KiCad 10.0 and save it"),
            (20231120, {"pin_name": "~"}, 20260306, "Use a symbol library of KiCad 10.0."),
            (20251024, {"font": BOLD}, 20261025, "Use a symbol library of the nightly build"),
            # A nightly build of KiCad 9 between two releases.
            (20231120, {"sub_symbols": BARE_BAR}, 20240620, "of the nightly build"),
            # A nightly library that is older than the nightly schematic.
            (20260500, {"font": BOLD}, 20261025, "Use a symbol library of the nightly build"),
        ],
    )
    def test_symbol_that_reads_in_a_different_way_is_refused(
        self, tmp_path, library, library_version, parts, schematic_version, advice
    ):
        library(library_version, **parts)
        sch = write_schematic(tmp_path, schematic_version)
        before = sch.read_text(encoding="utf-8")
        result = add_resistor(sch)
        assert result.startswith("❌ Cannot add component from"), result
        assert advice in result
        assert sch.read_text(encoding="utf-8") == before

    def test_symbol_of_an_other_release_that_reads_the_same(self, tmp_path, library):
        # A KiCad 8 symbol with no "~", no De Morgan, no bold and no bare "|".
        library(20231120)
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add_resistor(sch)

    def test_definition_in_the_schematic_is_used(self, tmp_path, library):
        library(20251024)
        sch = write_schematic(tmp_path, 20260306)
        assert "✅" in add_resistor(sch)
        # The library is now of an other release: the tool needs only the
        # definition that the schematic has.
        library(20231120, pin_name="~")
        result = asyncio.run(
            schematic_editor.add_component_from_library(str(sch), "Device", "R", "R2", "2k")
        )
        assert "✅" in result, result
        content = sch.read_text(encoding="utf-8")
        assert content.count('(symbol "Device:R"') == 1
        assert "**Symbol definition:** already in lib_symbols" in result
        assert '(property "Reference" "R2"' in content

    def test_derived_symbol_gets_the_items_of_its_parent(self, tmp_path, library):
        path = library(20251024)
        text = path.read_text(encoding="utf-8")
        derived = (
            '\t(symbol "R_Small"\n\t\t(extends "R")\n'
            '\t\t(property "Value" "R_Small" (at 0 0 0) (effects (font (size 1.27 1.27))))\n\t)\n)\n'
        )
        path.write_text(text[: text.rstrip().rindex(")")] + derived, encoding="utf-8")
        sch = write_schematic(tmp_path, 20260306)
        result = asyncio.run(
            schematic_editor.add_component_from_library(str(sch), "Device", "R_Small", "R1", "1k")
        )
        assert "✅" in result, result
        assert "**Pins:** 1 pins detected" in result
        content = sch.read_text(encoding="utf-8")
        assert '(symbol "Device:R_Small"' in content
        assert '(symbol "R_Small_1_1"' in content
        assert "(extends" not in content
        assert '(property "Value" "R_Small"' in content
        assert '(property "Reference" "R"' in content

    def test_derived_symbol_with_no_parent(self, tmp_path, library):
        path = library(20251024)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '(symbol "R"\n', '(symbol "R"\n\t\t(extends "X")\n', 1
            ),
            encoding="utf-8",
        )
        sch = write_schematic(tmp_path, 20260306)
        before = sch.read_text(encoding="utf-8")
        result = add_resistor(sch)
        assert result == (
            '❌ Cannot add component Device:R: The library has no parent symbol "X" for the '
            'derived symbol "R".'
        )
        assert sch.read_text(encoding="utf-8") == before

    def test_library_that_cannot_be_read(self, tmp_path, library):
        path = library(20251024)
        path.write_bytes(b'(kicad_symbol_lib (version 20251024) \xff (symbol "R"))')
        sch = write_schematic(tmp_path, 20260306)
        result = add_resistor(sch)
        assert result.startswith(
            f"❌ Cannot add component Device:R: the symbol library {path} cannot"
        ), result


TOOLS = {
    "add_component_from_library": lambda p: schematic_editor.add_component_from_library(
        p, "Device", "R", "R1", "1k"
    ),
    "add_wire": lambda p: schematic_editor.add_wire(p, [(0, 0), (10, 0)]),
    "add_label": lambda p: schematic_editor.add_label(p, "SIG", 0, 0),
    "add_global_label": lambda p: schematic_editor.add_global_label(p, "", "", "G", "input", 0, 0),
    "add_hierarchical_label": lambda p: schematic_editor.add_hierarchical_label(
        p, "", "", "H", "input", 0, 0
    ),
}


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "version, message",
    [
        (20230121, "older than the version of KiCad 8.0"),
        (20261026, "newer than the newest version that this server knows"),
    ],
)
def test_tool_does_not_change_a_file_of_an_unknown_version(
    tmp_path, library, tool, version, message
):
    library(20251024)
    sch = write_schematic(tmp_path, version)
    before = sch.read_text(encoding="utf-8")
    result = asyncio.run(TOOLS[tool](str(sch)))
    assert result.startswith(f"❌ {sch}: "), result
    assert message in result
    assert "The tool did not change the file." in result
    assert sch.read_text(encoding="utf-8") == before
