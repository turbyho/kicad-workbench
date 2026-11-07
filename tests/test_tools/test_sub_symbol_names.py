"""Tests of the names of sub-symbols, as parseLibSymbol of KiCad reads them.

The name "<name>_<unit>_<body style>" must start with the item name of the
symbol; the parser skips one more character, splits the rest at "_" into two
tokens and reads each one with wxString::ToLong into an int. The cases are the
cases of spike/kicad-versions/sub_symbol_names_check.py, measured with kicad-cli
8.0.9, 9.0.9, 10.0.6 and 10.0.7.
"""

import pytest

from kicad_workbench.errors import KicadParseError, LocaleNumberError
from kicad_workbench.parsers.kicad_versions import KicadFileError
from kicad_workbench.parsers.lib_symbols import flatten, unit_count
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.parsers.sub_symbols import sub_symbol_unit

from .test_lib_symbols import ROOT, lib  # noqa: F401 (fixture)


@pytest.mark.parametrize(
    "name, unit",
    [
        ("S_2_1", (2, 1)),
        ("SX2_1", (2, 1)),  # any character after the item name
        ("S_+2_1", (2, 1)),
        ("S_ 2_1", (2, 1)),
        ("S_\t2_1", (2, 1)),
        ("S_2_ 1", (2, 1)),
        ("S_02_01", (2, 1)),
        ("S_-2_1", (-2, 1)),
        ("S_2_1_", (2, 1)),  # no token after the last character that is not "_"
        ("S_2_1___", (2, 1)),
        ("S{slash}_2_1", None),
    ],
)
def test_names_that_kicad_reads(long64, name, unit):
    if unit is None:
        # "{slash}" is "/": "S/_2_1" starts with "S", the rest "_2_1" has three tokens.
        with pytest.raises(KicadFileError, match="Invalid symbol unit name suffix"):
            sub_symbol_unit("S", name)
    else:
        assert sub_symbol_unit("S", name) == unit


@pytest.mark.parametrize(
    "name, message",
    [
        ("T_2_1", "Invalid symbol unit name prefix"),
        ("S", "Invalid symbol unit name suffix"),
        ("S_2", "Invalid symbol unit name suffix"),
        ("S__2_1", "Invalid symbol unit name suffix"),  # an empty token at the start
        ("S___2_1", "Invalid symbol unit name suffix"),
        ("S_2__1", "Invalid symbol unit name suffix"),  # an empty token in the middle
        ("S_2_1_1", "Invalid symbol unit name suffix"),
        ("S_a_1", "Invalid symbol unit number"),
        ("S_2 _1", "Invalid symbol unit number"),  # white space at the end
        ("S_99999999999999999999_1", "Invalid symbol unit number"),  # out of the range
        ("S_2_1 ", "Invalid symbol body style number"),
    ],
)
def test_names_that_kicad_refuses(long64, name, message):
    with pytest.raises(KicadFileError, match=message):
        sub_symbol_unit("S", name)


def test_a_unit_out_of_the_range_of_an_int_on_linux(long64):
    # Measured with kicad-cli 8.0.9 to 10.0.7 on Linux: unit 2, the cast of the long
    # 2**32 + 2 to an int.
    assert sub_symbol_unit("S", "S_4294967298_1") == (2, 1)


def test_a_unit_out_of_the_range_of_a_long_on_windows(long32):
    # A long of 32 bits: wxString::ToLong gives false (from the source).
    with pytest.raises(KicadParseError, match="Invalid symbol unit number 4294967298_1"):
        sub_symbol_unit("S", "S_4294967298_1")


def test_the_unit_count():
    text = '(symbol "Lib:S" (symbol "S_0_1") (symbol "S_ 3_1") (symbol "S_-5_1"))'
    assert unit_count(parse(text)) == 3
    with pytest.raises(KicadFileError):
        unit_count(parse('(symbol "Lib:S" (symbol "S_x_1"))'))


def test_flatten_writes_the_numbers(lib):  # noqa: F811
    # KiCad saves the numbers that it read (SCH_IO_KICAD_SEXPR_LIB_CACHE::saveSymbol).
    root = ROOT.replace('"R_0_1"', '"R_ 0_01"')
    symbols = lib(root)
    node = parse(flatten([symbols["R"]], "Device:R", 10))
    assert [sub.value(0) for sub in node.children("symbol")][0] == "R_0_1"


def test_a_white_space_that_is_not_ascii():
    # The number of a name depends on the locale of KiCad (c_numbers.to_long).
    with pytest.raises(LocaleNumberError, match="only in some locales"):
        sub_symbol_unit("S", "S_\u20032_1")


def test_the_errors_are_parse_errors():
    with pytest.raises(KicadParseError, match="Invalid symbol unit name suffix 2__1"):
        sub_symbol_unit("S", "S_2__1")


def test_a_sub_sheet_that_kicad_cannot_read_is_an_empty_sheet(tmp_path):
    # Measured with kicad-cli 8.0.9 to 10.0.7: a sub-sheet with a sub-symbol name that
    # KiCad cannot read (also in a definition that no symbol uses) is an empty sheet,
    # the other sheets load.
    from kicad_workbench.tools.schematic import placed_components

    from .test_symbol_fields import symbol, write_project

    path = write_project(
        tmp_path,
        symbol("U1", 1, paths=(("/a0", "U1"),)),
        symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6"))),
    )
    sub = tmp_path / "sub.kicad_sch"
    sub.write_text(
        sub.read_text(encoding="utf-8").replace('"LM358_2_1"', '"LM358_x_1"'),
        encoding="utf-8",
    )
    placed, problems = placed_components(path, "10.0.7", project_wide=True)
    assert [p.component.reference for p in placed] == ["U1"]
    assert len(problems) == 2, problems
    assert "cannot be read: Invalid symbol unit number x_1" in problems[0]
    assert "KiCad shows an empty sheet." in problems[0]


def test_a_library_with_a_name_that_kicad_cannot_read(tmp_path):
    # Measured with kicad-cli 8.0.9 to 10.0.7: KiCad does not load the library, also
    # not its other symbols (sym export svg of B fails).
    from kicad_workbench.parsers.lib_symbols import read_library

    library = tmp_path / "L.kicad_sym"
    library.write_text(
        '(kicad_symbol_lib (version 20251024) (generator "test")\n'
        '(symbol "A" (symbol "A_x_1"))\n(symbol "B" (symbol "B_1_1")))\n',
        encoding="utf-8",
    )
    with pytest.raises(KicadParseError, match="Invalid symbol unit number x_1"):
        read_library(library)
