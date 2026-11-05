"""Tests of the insertion of a symbol definition into the lib_symbols section.

SCH_IO_KICAD_SEXPR::Format of KiCad 8, 9 and 10 writes the section after the
header items: version, generator, generator_version, uuid, paper, title_block.
spike/kicad-versions/lib_symbols_check.py reads the results with kicad-cli.
"""

from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools.schematic_editor import (
    _append_top_level,
    _insert_symbol_into_lib_symbols,
)

from .test_snap_to_grid import add, make_schematic

DEFINITION = '\t\t(symbol "Lib:X" (symbol "X_1_1"))'


def heads(text: str) -> list[str]:
    return [child.head for child in parse(text).children()]


def test_file_with_the_header_of_kicad():
    content = (
        "(kicad_sch\n"
        "\t(version 20260306)\n"
        '\t(generator "eeschema")\n'
        '\t(generator_version "10.0")\n'
        '\t(uuid "a0")\n'
        '\t(paper "A4")\n'
        '\t(title_block (title "T"))\n'
        '\t(wire (pts (xy 0 0) (xy 1 0)) (uuid "w1"))\n'
        ")\n"
    )
    result = _insert_symbol_into_lib_symbols(content, DEFINITION)
    assert heads(result) == [
        "version",
        "generator",
        "generator_version",
        "uuid",
        "paper",
        "title_block",
        "lib_symbols",
        "wire",
    ]
    assert parse(result).first("lib_symbols").first("symbol").value(0) == "Lib:X"
    assert result.endswith(")\n")


def test_file_in_one_line():
    content = '(kicad_sch (version 20260306) (uuid "a0") (paper "A4") (wire (uuid "w1")))'
    result = _insert_symbol_into_lib_symbols(content, DEFINITION)
    assert heads(result) == ["version", "uuid", "paper", "lib_symbols", "wire"]


def test_item_before_the_header():
    # The section goes after the header items at the start of the file only.
    content = '(kicad_sch (version 20260306) (wire (uuid "w1")) (uuid "a0"))'
    result = _insert_symbol_into_lib_symbols(content, DEFINITION)
    assert heads(result) == ["version", "lib_symbols", "wire", "uuid"]


def test_file_with_no_items():
    result = _insert_symbol_into_lib_symbols("(kicad_sch)", DEFINITION)
    assert heads(result) == ["lib_symbols"]


def test_existing_section():
    # The new symbol goes after the last symbol, on its own lines (the format of KiCad).
    content = '(kicad_sch (version 20260306)\n\t(lib_symbols\n\t\t(symbol "Lib:A")\n\t)\n)\n'
    result = _insert_symbol_into_lib_symbols(content, DEFINITION)
    assert result == (
        '(kicad_sch (version 20260306)\n\t(lib_symbols\n\t\t(symbol "Lib:A")\n'
        f"{DEFINITION}\n\t)\n)\n"
    )


def test_empty_section():
    # KiCad writes "(lib_symbols)" for no symbols, and the ")" on its own line else.
    content = "(kicad_sch (version 20260306)\n\t(lib_symbols)\n)\n"
    result = _insert_symbol_into_lib_symbols(content, DEFINITION)
    assert result == f"(kicad_sch (version 20260306)\n\t(lib_symbols\n{DEFINITION}\n\t)\n)\n"


def test_append_top_level():
    content = "(kicad_sch (version 20260306)\n\t(lib_symbols)\n)\n"
    assert _append_top_level(content, '\t(wire (uuid "w1"))') == (
        '(kicad_sch (version 20260306)\n\t(lib_symbols)\n\t(wire (uuid "w1"))\n)\n'
    )
    # A comment after the root list stays after it: the item goes into the list.
    result = _append_top_level(content + "# a comment\n", '\t(wire (uuid "w1"))')
    assert heads(result) == ["version", "lib_symbols", "wire"]
    assert result.endswith(")\n# a comment\n")


def test_tool_adds_the_section(tmp_path, monkeypatch):
    sch = make_schematic(tmp_path, monkeypatch)
    sch.write_text(sch.read_text(encoding="utf-8").replace("\t(lib_symbols)\n", ""), "utf-8")
    assert parse(sch.read_text(encoding="utf-8")).first("lib_symbols") is None
    result = add(sch)
    assert result.startswith("✅"), result
    text = sch.read_text(encoding="utf-8")
    # The test schematic has no generator_version.
    assert heads(text)[: heads(text).index("lib_symbols") + 1] == [
        "version",
        "generator",
        "uuid",
        "paper",
        "lib_symbols",
    ]
    definition = parse(text).first("lib_symbols").first("symbol")
    assert definition.value(0) == "Device:R"
