"""Tests of the derived symbol (utils/library_derive.py and the tool
create_derived_symbol), with the rules of "Derive from existing symbol" of the symbol
editor of KiCad 10.0.6. kicad-cli is replaced by a copy of the file; the check with
KiCad is spike/kicad-versions/derived_symbol_check.py."""

import pytest

from kicad_workbench.parsers.lib_symbols import read_library, symbol_chain
from kicad_workbench.parsers.sexpr import parse, property_atoms
from kicad_workbench.tools.project_libraries import create_derived_symbol
from kicad_workbench.utils.library_derive import derived_symbol, new_symbol_name

from . import test_library_symbol_fields as symbol_fields
from .test_library_symbol_fields import MANDATORY, library, prop, symbol

# The fixtures of the tool tests: kicad-cli replaced by a copy, a project with the
# library Mine.
kicad_10 = symbol_fields.kicad_10
project = symbol_fields.project

FIELDS = [
    prop("Reference", "U"),
    prop("Value", "LM358"),
    prop("Footprint", "Package_SO:SOIC-8"),
    prop("Datasheet", "https://x/ds.pdf"),
    prop("Description", "Op amp"),
    prop("MPN", "LM358DR"),
    prop("ki_keywords", "amp"),
]


def _fields(text: str) -> list[tuple[str, str]]:
    found = []
    for item in parse(text).children("property"):
        atoms = property_atoms(item)
        found.append((atoms[0].value, atoms[1].value))
    return found


def _derive(tmp_path, parent_text, name="NEW", **options):
    path = tmp_path / "lib.kicad_sym"
    path.write_text(library(parent_text), encoding="utf-8")
    chain = symbol_chain(read_library(path), parent_text.split('"')[1])
    flags = {
        "keep_footprint": False,
        "keep_datasheet": False,
        "transfer_user_fields": False,
        "keep_user_field_content": False,
        **options,
    }
    return derived_symbol(chain, name, **flags)


class TestRules:
    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("LM358 B", "LM358_B"),
            (" a ", "_a_"),  # no trim
            ('a:b"c<d>e\\f', "a{colon}b{dblquote}c{lt}d{gt}e{backslash}f"),
        ],
    )
    def test_the_name(self, text, name):
        assert new_symbol_name(text) == name

    def test_the_defaults_of_the_dialog(self, tmp_path):
        text = _derive(tmp_path, symbol("LM358", *FIELDS))
        assert text.startswith('\t(symbol "NEW" (extends "LM358")')
        assert _fields(text) == [
            ("Reference", "U"),
            ("Value", "LM358"),
            ("Footprint", ""),
            ("Datasheet", ""),
            ("Description", "Op amp"),
        ]

    def test_keep_and_transfer(self, tmp_path):
        text = _derive(
            tmp_path,
            symbol("LM358", *FIELDS),
            keep_footprint=True,
            keep_datasheet=True,
            transfer_user_fields=True,
        )
        fields = dict(_fields(text))
        assert fields["Footprint"] == "Package_SO:SOIC-8"
        assert fields["Datasheet"] == "https://x/ds.pdf"
        # The text of a user field is empty unless keep_user_field_content; the
        # properties ki_* are not fields.
        assert fields["MPN"] == "" and "ki_keywords" not in fields
        text = _derive(
            tmp_path, symbol("LM358", *FIELDS), transfer_user_fields=True,
            keep_user_field_content=True,
        )  # fmt: skip
        assert dict(_fields(text))["MPN"] == "LM358DR"

    def test_a_power_symbol(self, tmp_path):
        # The Value gets the new name (the power flag of the root).
        text = _derive(tmp_path, symbol("GND", *MANDATORY, power=True), name="GND2")
        assert dict(_fields(text))["Value"] == "GND2"


class TestTool:
    async def test_derive(self, project):
        out = await create_derived_symbol(str(project), "Mine:X", "X B")
        assert out.startswith("✅ The library 'Mine' has the new symbol 'X_B'"), out
        assert "the Footprint and the Datasheet are empty" in out
        library_text = (project.parent / "Mine.kicad_sym").read_text("utf-8")
        assert '(symbol "X_B" (extends "X")' in library_text

    async def test_errors(self, project):
        out = await create_derived_symbol(str(project), "Mine:X", "X")
        assert out.startswith("❌ Symbol 'X' already exists in library 'Mine'."), out
        out = await create_derived_symbol(str(project), "Mine:Nope", "Y")
        assert "has no symbol 'Nope'" in out
        # FIELD_VALIDATOR of the name control: no CR, LF or tab; then an empty name.
        out = await create_derived_symbol(str(project), "Mine:X", "a\r\nb")
        assert out.startswith("❌ The field cannot contain carriage return"), out
        out = await create_derived_symbol(str(project), "Mine:X", "a\tb")
        assert out.startswith("❌ The field cannot contain tab"), out
        assert await create_derived_symbol(str(project), "Mine:X", "") == (
            "❌ Symbol must have a name."
        )
        out = await create_derived_symbol(str(project), "Mine:X", "Y", keep_user_field_content=True)
        assert out == "❌ keep_user_field_content needs transfer_user_fields=True."
        before = (project.parent / "Mine.kicad_sym").read_bytes()
        (project.parent / "~board.kicad_pro.lck").write_text("{}", encoding="utf-8")
        out = await create_derived_symbol(str(project), "Mine:X", "Y")
        assert out.startswith("❌") and (project.parent / "Mine.kicad_sym").read_bytes() == before
