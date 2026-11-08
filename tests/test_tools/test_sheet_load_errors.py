"""Tests of the files of a hierarchy that KiCad cannot read (sheet_list.build_sheet_list).

Measured with kicad-cli 8.0.9, 9.0.9, 10.0.6 and 10.0.7
(spike/kicad-versions/sheet_fields_check.py): KiCad keeps the items of a sub-sheet
before a parse error (unbalanced parentheses, a byte that is not UTF-8, KiCad 10 and
a sheet with no name). The server does not model the part of a file: an error (user
decision, 2026-11-07). A missing file is an empty sheet.
"""

import re

import pytest

from kicad_workbench.errors import KicadFileError, KicadParseError
from kicad_workbench.tools.schematic import placed_components

from .test_symbol_fields import KICAD9, symbol, write_project


def project(tmp_path, version="20260306"):
    return write_project(
        tmp_path,
        symbol("U1", 1, paths=(("/a0", "U1"),)),
        symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6"))),
        version=version,
    )


@pytest.mark.parametrize(
    "damage",
    [
        lambda text: text.rstrip()[:-1],  # unbalanced parentheses at the end
        lambda text: text.replace(
            "(symbol\n", '(sheet (at 1 1) (size 2 2) (uuid "x"))\n(symbol\n', 1
        ),
    ],
)
def test_a_parse_error_in_a_sub_sheet(tmp_path, damage):
    path = project(tmp_path)
    sub = tmp_path / "sub.kicad_sch"
    sub.write_text(damage(sub.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(KicadParseError, match="keeps only the items of the file before the error"):
        placed_components(path, "10.0.7", project_wide=True)


def test_a_byte_that_is_not_utf8_in_a_sub_sheet(tmp_path):
    path = project(tmp_path)
    sub = tmp_path / "sub.kicad_sch"
    data = sub.read_bytes()
    end = data.rindex(b")")
    sub.write_bytes(data[:end] + b"\xff\n" + data[end:])
    with pytest.raises(KicadParseError, match="The parser of KiCad stops in a sub-sheet"):
        placed_components(path, "10.0.7", project_wide=True)


def test_a_missing_sub_sheet_is_an_empty_sheet(tmp_path):
    path = project(tmp_path)
    (tmp_path / "sub.kicad_sch").unlink()
    placed, _problems = placed_components(path, "10.0.7", project_wide=True)
    assert [p.component.reference for p in placed] == ["U1"]


def test_kicad_9_and_a_sheet_with_one_field_in_a_sub_sheet(tmp_path):
    # KiCad 8 and 9 read the fields past their end: an error that names the file.
    path = project(tmp_path, KICAD9)
    sub = tmp_path / "sub.kicad_sch"
    sheet = '(sheet (at 1 1) (size 2 2) (uuid "x") (property "Sheetname" "S" (at 0 0 0)))\n'
    sub.write_text(
        sub.read_text(encoding="utf-8").replace("\t(symbol\n", sheet + "\t(symbol\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(KicadFileError, match=f"{re.escape(str(sub))}: A sheet has 1 properties"):
        placed_components(path, "9.0.9", project_wide=True)


def test_a_sub_sheet_of_an_old_version_names_the_file(tmp_path):
    # The hierarchy loader (build_sheet_list) gives the file of a KicadFileError.
    path = project(tmp_path)
    sub = tmp_path / "sub.kicad_sch"
    sub.write_text(
        sub.read_text(encoding="utf-8").replace("(version 20260306)", "(version 20230121)"),
        encoding="utf-8",
    )
    with pytest.raises(
        KicadFileError, match=f"{re.escape(str(sub))}: .*older than the version of KiCad 8"
    ):
        placed_components(path, "10.0.7", project_wide=True)
