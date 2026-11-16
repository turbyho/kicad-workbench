"""Tests of the justify of the labels that the tools write (SCH_LABEL_BASE::SetSpinStyle of
KiCad 8, 9 and 10; spike/kicad-versions/label_justify_check.py compares the rules with
the labels that KiCad saved)."""

import asyncio
import uuid

import pytest

from kicad_workbench.tools.schematic_editor import add_label, label_justify

SCHEMATIC = """(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "%s")
\t(paper "A4")
\t(lib_symbols)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
\t(embedded_fonts no)
)
"""


@pytest.mark.parametrize(
    ("kind", "angle", "justify"),
    [
        ("label", 0, "left bottom"),
        ("label", 90, "left bottom"),
        ("label", 180, "right bottom"),
        ("label", 270, "right bottom"),
        ("global_label", 90, "left"),
        ("hierarchical_label", 270, "right"),
    ],
)
def test_label_justify(kind, angle, justify):
    assert label_justify(kind, angle) == justify


@pytest.mark.parametrize(
    ("orientation", "at", "justify"),
    [(0, "0", "left bottom"), (180.0, "180", "right bottom"), (270, "270", "right bottom")],
)
def test_add_label(tmp_path, orientation, at, justify):
    path = tmp_path / "t.kicad_sch"
    path.write_text(SCHEMATIC % uuid.uuid4(), encoding="utf-8")
    out = asyncio.run(add_label(str(path), "NET", 50.8, 50.8, orientation))
    assert out.startswith("✅"), out
    text = path.read_text(encoding="utf-8")
    assert f'(label "NET" (at 50.8 50.8 {at})' in text
    assert f"(justify {justify}))" in text


def test_label_justify_of_an_other_item():
    from kicad_workbench.errors import InputError

    with pytest.raises(InputError):
        label_justify("text", 0)
    with pytest.raises(InputError):
        label_justify("label", 45)


def test_add_label_with_an_other_angle(tmp_path):
    path = tmp_path / "t.kicad_sch"
    path.write_text(SCHEMATIC % uuid.uuid4(), encoding="utf-8")
    out = asyncio.run(add_label(str(path), "NET", 50.8, 50.8, 45))
    assert out.startswith("❌ The orientation 45 is not 0, 90, 180 or 270"), out
    # The check of the angle comes before the read of the file.
    out = asyncio.run(add_label(str(tmp_path / "missing.kicad_sch"), "NET", 0, 0, 45))
    assert out.startswith("❌ The orientation 45"), out
