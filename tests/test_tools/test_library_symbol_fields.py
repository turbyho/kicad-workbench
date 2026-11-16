"""Tests of the properties of a library symbol (utils/library_symbol_fields.py and the
tool set_library_symbol_properties), with the rules of the Symbol Properties dialog of
KiCad 10.0.6. kicad-cli is replaced by a copy of the file."""

import os
import re
import shutil
from pathlib import Path

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.parsers.lib_symbols import read_library, symbol_chain
from kicad_workbench.tools import project_libraries
from kicad_workbench.tools.project_libraries import set_library_symbol_properties
from kicad_workbench.utils import library_copy, library_tables
from kicad_workbench.utils.library_symbol_fields import (
    change_fields,
    convert_path_to_file_uri,
    field_text,
    name_problem,
    same_name,
    symbol_editor_text_size,
    text_problem,
)
from kicad_workbench.utils.library_tables import KicadEnvironment

ENV = KicadEnvironment(10, 0, Path("/config"), {})


def symbol(name, *props, extends=None, power=False):
    parts = [f'\t(symbol "{name}"']
    if power:
        parts.append("\t\t(power)")
    if extends:
        parts.append(f'\t\t(extends "{extends}")')
    parts += [f"\t\t{prop}" for prop in props]
    if not extends:
        parts.append(f'\t\t(symbol "{name}_1_1" (pin input line (at 0 0 0) (length 1)))')
    return "\n".join(parts) + "\n\t)\n"


def prop(name, value, hide=False):
    return f'(property "{name}" "{value}" (at 1 2 0){" (hide yes)" if hide else ""})'


MANDATORY = [prop("Reference", "U"), prop("Value", "X"), prop("Footprint", ""),
             prop("Datasheet", ""), prop("Description", "")]  # fmt: skip


def library(*symbols):
    return '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "x")\n' + "".join(symbols) + ")\n"


def chain_of(tmp_path, text, name):
    path = tmp_path / "lib.kicad_sym"
    path.write_text(text, encoding="utf-8")
    return symbol_chain(read_library(path), name)


def change(tmp_path, chain, fields, keywords=None, filters=None):
    return change_fields(chain, fields, keywords, filters, ENV, {}, tmp_path, 1.27)


class TestRules:
    @pytest.mark.parametrize(
        ("name", "problem"),
        [
            ("MPN", None),
            ("", "Fields must have a name."),
            ("a\tb", "cannot contain tab"),
            ("ki_keywords", "property of the symbol"),
            ("${X}", "generated field"),
        ],
    )
    def test_name_problem(self, name, problem):
        result = name_problem(name)
        assert result is None if problem is None else problem in result

    @pytest.mark.parametrize(
        ("name", "text", "root", "problem"),
        [
            ("Reference", "U", True, None),
            ("Reference", "U 1", True, "cannot contain space"),
            ("Reference", "", True, "cannot be empty"),
            ("Reference", "${X}", True, "text variable"),
            ("Reference", "12", True, "References must start with a letter."),
            ("Reference", "12", False, None),
            ("MPN", "a\nb", True, "cannot contain line feed"),
            ("Value", 'a "b"', True, None),
        ],
    )
    def test_text_problem(self, name, text, root, problem):
        result = text_problem(name, text, root)
        assert result is None if problem is None else problem in result

    def test_same_name(self):
        assert same_name("value", "Value") and same_name("MPN", "MPN")
        assert not same_name("mpn", "MPN")

    def test_file_uri_and_text(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "ds.pdf").write_text("x", encoding="utf-8")
        project = {"KIPRJMOD": str(tmp_path)}
        assert (
            convert_path_to_file_uri("${KIPRJMOD}/docs/ds.pdf", ENV, project, tmp_path)
            == "file://${KIPRJMOD}/docs/ds.pdf"
        )
        assert convert_path_to_file_uri("./docs", ENV, project, tmp_path) == "file://./docs"
        # A path to no file, and a text that is not a path, stay.
        assert convert_path_to_file_uri("./nope.pdf", ENV, project, tmp_path) == "./nope.pdf"
        assert convert_path_to_file_uri("https://x/a.pdf", ENV, project, tmp_path) == (
            "https://x/a.pdf"
        )
        # UnescapeString, and the trim of a mandatory field only.
        assert field_text("Description", " a{slash}b ", ENV, project, tmp_path) == "a/b"
        assert field_text("MPN", " a ", ENV, project, tmp_path) == " a "

    def test_text_size(self, tmp_path):
        assert symbol_editor_text_size(tmp_path) == (pytest.approx(1.27), None)
        settings = tmp_path / "symbol_editor.json"
        settings.write_text('{"defaults": {"text_size": 60}}', "utf-8")
        assert symbol_editor_text_size(tmp_path) == (pytest.approx(1.524), None)
        settings.write_text('{"defaults": {"text_size": "big"}}', "utf-8")
        size, warning = symbol_editor_text_size(tmp_path)
        assert size == pytest.approx(1.27) and "not a number" in warning
        settings.write_text("{bad", "utf-8")
        size, warning = symbol_editor_text_size(tmp_path)
        assert size == pytest.approx(1.27) and "cannot be read" in warning


class TestChangeFields:
    def test_set_add_remove(self, tmp_path):
        text = library(symbol("X", *MANDATORY, prop("Old", "1")))
        chain = chain_of(tmp_path, text, "X")
        result = change(
            tmp_path, chain, {"value": "10k", "MPN": "P1", "Old": None}, "k w", ["A B", ""]
        )
        new = result.text
        assert '(property "Value" "10k"' in new and '(property "Old"' not in new
        assert (
            '(property "MPN" "P1" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))' in new
        )
        assert '(property "ki_keywords" "k w"' in new
        assert '(property "ki_fp_filters" "A_B"' in new

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"Value": None}, "The first 5 fields are mandatory"),
            ({"Nope": None}, "no field 'Nope'"),
            ({"MPN": "a", "MPN ": "b"}, "already in use"),
            # FIELD_VALIDATOR sees the cell text before the trim.
            ({"MPN\t": "a"}, "cannot contain tab"),
        ],
    )
    def test_refused(self, tmp_path, fields, message):
        chain = chain_of(tmp_path, library(symbol("X", *MANDATORY)), "X")
        with pytest.raises(InputError, match=message):
            change(tmp_path, chain, fields)

    def test_no_change_and_filters(self, tmp_path):
        chain = chain_of(tmp_path, library(symbol("X", *MANDATORY)), "X")
        assert change(tmp_path, chain, {"Value": "X"}).lines == []
        with pytest.raises(InputError, match="cannot contain a tab"):
            change(tmp_path, chain, {}, filters=["SOT\t23"])

    def test_missing_mandatory_property(self, tmp_path):
        chain = chain_of(tmp_path, library(symbol("X", prop("Reference", "U"))), "X")
        with pytest.raises(InputError, match="no 'Datasheet' property"):
            change(tmp_path, chain, {"Datasheet": "x"})

    def test_power_symbol(self, tmp_path):
        chain = chain_of(tmp_path, library(symbol("GND", *MANDATORY, power=True)), "GND")
        with pytest.raises(InputError, match="power symbol"):
            change(tmp_path, chain, {"Value": "x"})
        result = change(tmp_path, chain, {"Description": "ground"})
        assert '(property "Value" "GND"' in result.text
        # A symbol derived from a power symbol is a power symbol (the flag of the root).
        text = library(
            symbol("GND", *MANDATORY, power=True), symbol("GNDA", *MANDATORY, extends="GND")
        )
        chain = chain_of(tmp_path, text, "GNDA")
        with pytest.raises(InputError, match="power symbol"):
            change(tmp_path, chain, {"Footprint": "x"})

    def test_field_of_a_parent(self, tmp_path):
        text = library(
            symbol("P", *MANDATORY, prop("MPN", "parent", hide=True)),
            symbol("D", *MANDATORY, extends="P"),
        )
        chain = chain_of(tmp_path, text, "D")
        result = change(tmp_path, chain, {"MPN": "child"})
        # The field of the parent, with its attributes and the new text.
        assert '(property "MPN" "child" (at 1 2 0) (hide yes))' in result.text
        with pytest.raises(InputError, match="comes from the parent"):
            change(tmp_path, chain, {"MPN": None})


# The tool.


def fake_upgrade(kind, source, output, force):
    shutil.copyfile(source, output)


@pytest.fixture(autouse=True)
def kicad_10(monkeypatch, tmp_path):
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)

    async def version():
        return "10.0.6"

    monkeypatch.setattr(project_libraries, "get_kicad_version", version)
    monkeypatch.setattr(library_copy, "run_upgrade", fake_upgrade)
    (tmp_path / "config" / "10.0").mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "board"
    folder.mkdir()
    (folder / "board.kicad_pro").write_text("{}", encoding="utf-8")
    (folder / "Mine.kicad_sym").write_text(library(symbol("X", *MANDATORY)), encoding="utf-8")
    (folder / "sym-lib-table").write_text(
        '(sym_lib_table\n\t(version 7)\n\t(lib (name "Mine") (type "KiCad") '
        '(uri "${KIPRJMOD}/Mine.kicad_sym") (options "") (descr ""))\n)\n',
        encoding="utf-8",
    )
    return folder / "board.kicad_pro"


class TestTool:
    async def test_change(self, project):
        out = await set_library_symbol_properties(
            str(project), "Mine:X", {"MPN": "P1"}, keywords="k", footprint_filters=["SOT*"]
        )
        assert out.startswith("✅ The symbol 'X' of the library 'Mine' has the changes."), out
        assert "- New field MPN (hidden): 'P1'" in out
        text = (project.parent / "Mine.kicad_sym").read_text("utf-8")
        assert '(property "MPN" "P1"' in text and '(property "ki_fp_filters" "SOT*"' in text

    async def test_no_change(self, project):
        out = await set_library_symbol_properties(str(project), "Mine:X", {"Value": "X"})
        assert out.endswith("the tool wrote no file."), out

    async def test_errors(self, project):
        assert (await set_library_symbol_properties(str(project), "Mine:X")).startswith(
            "❌ Give fields"
        )
        out = await set_library_symbol_properties(str(project), "Mine:Nope", {"MPN": "1"})
        assert "has no symbol 'Nope'" in out
        out = await set_library_symbol_properties(str(project), "Other:X", {"MPN": "1"})
        assert "no library 'Other'" in out
        (project.parent / "~board.kicad_pro.lck").write_text("{}", encoding="utf-8")
        out = await set_library_symbol_properties(str(project), "Mine:X", {"MPN": "1"})
        assert out.startswith("❌") and "MPN" not in (project.parent / "Mine.kicad_sym").read_text(
            "utf-8"
        )
