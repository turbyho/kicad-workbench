"""Tests of the copy of library items (utils/library_copy.py and the tools
copy_symbol_to_library and copy_footprint_to_library). kicad-cli is replaced by a copy
of the file: spike/kicad-versions/library_copy_check.py compares the tools with real
KiCad."""

import os
import re
import shutil

import pytest

from kicad_workbench.parsers.lib_symbols import read_library, symbol_chain
from kicad_workbench.tools import project_libraries
from kicad_workbench.tools.project_libraries import (
    copy_footprint_to_library,
    copy_symbol_to_library,
)
from kicad_workbench.utils import library_copy, library_tables, library_upgrade
from kicad_workbench.utils.library_copy import (
    CopyError,
    dialog_symbol_name,
    footprint_name_problem,
    library_text,
    symbol_conflicts,
    symbol_copy,
)

V_SYM = 20251024
V_FP = 20260206


def symbol(name, value=None, extends=None, units=True):
    value = name if value is None else value
    parts = [f'\t(symbol "{name}"']
    if extends:
        parts.append(f'\t\t(extends "{extends}")')
    parts.append('\t\t(property "Reference" "U" (at 0 0 0))')
    parts.append(f'\t\t(property "Value" "{value}" (at 0 0 0))')
    if units and not extends:
        parts.append(f'\t\t(symbol "{name}_1_1" (pin input line (at 0 0 0) (length 1)))')
    parts.append("\t)")
    return "\n".join(parts) + "\n"


def library(*symbols, version=V_SYM):
    return (
        f'(kicad_symbol_lib\n\t(version {version})\n\t(generator "kicad_symbol_editor")\n'
        + "".join(symbols)
        + ")\n"
    )


def chain_of(tmp_path, text, name):
    path = tmp_path / "source.kicad_sym"
    path.write_text(text, encoding="utf-8")
    return symbol_chain(read_library(path), name)


class TestNames:
    @pytest.mark.parametrize(
        ("text", "name"),
        [
            (" My Op Amp ", "My_Op_Amp"),
            ("A<1>", "A{lt}1{gt}"),
            ('a"b:c', "a{dblquote}b{colon}c"),
            # wxString::Trim removes only ASCII white space.
            ("\u00a0R1\t", "\u00a0R1"),
        ],
    )
    def test_dialog_symbol_name(self, text, name):
        assert dialog_symbol_name(text) == name

    @pytest.mark.parametrize(
        ("name", "problem"),
        [("R 0603", None), ("", "empty"), ("a/b", "refuses them"), ("a*b", "file name")],
    )
    def test_footprint_name_problem(self, name, problem):
        result = footprint_name_problem(name)
        assert result is None if problem is None else problem in result


class TestSymbolCopy:
    def test_chain_to_an_other_library(self, tmp_path):
        text = library(symbol("Root"), symbol("Mid", extends="Root"), symbol("Leaf", extends="Mid"))
        chain = chain_of(tmp_path, text, "Leaf")
        copy = symbol_copy(chain, "New", same_library=False, flatten_symbol=False)
        assert [name for name, _ in copy.symbols] == ["Root", "Mid", "New"]
        leaf = copy.symbols[-1][1]
        # The leaf gets the new name; its Value follows (it was equal to the name), and
        # each parent gets its own name as Value (DoSave sets the Value of each symbol).
        assert '(symbol "New"' in leaf and '(property "Value" "New"' in leaf
        assert '(extends "Mid")' in leaf

    def test_value_that_does_not_follow(self, tmp_path):
        chain = chain_of(tmp_path, library(symbol("R", value="10k")), "R")
        copy = symbol_copy(chain, "R2", same_library=False, flatten_symbol=False)
        text = copy.symbols[0][1]
        assert '(symbol "R2"' in text and '(symbol "R2_1_1"' in text
        assert '(property "Value" "10k"' in text

    def test_same_library_and_flatten(self, tmp_path):
        text = library(symbol("Root"), symbol("Leaf", extends="Root"))
        chain = chain_of(tmp_path, text, "Leaf")
        same = symbol_copy(chain, "Leaf2", same_library=True, flatten_symbol=False)
        assert [name for name, _ in same.symbols] == ["Leaf2"]
        assert '(extends "Root")' in same.symbols[0][1]
        flat = symbol_copy(chain, "Flat", same_library=False, flatten_symbol=True)
        assert [name for name, _ in flat.symbols] == ["Flat"]
        assert "extends" not in flat.symbols[0][1]
        assert '(symbol "Flat_1_1"' in flat.symbols[0][1]
        assert '(property "Value" "Flat"' in flat.symbols[0][1]

    def test_conflicts_and_circular_inheritance(self, tmp_path):
        text = library(
            symbol("Root"), symbol("Leaf", extends="Root"), symbol("Child", extends="Leaf")
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(text, encoding="utf-8")
        target = read_library(path)
        chain = symbol_chain(target, "Leaf")
        # The symbol itself: KiCad asks to overwrite it (a conflict, not an error).
        copy = symbol_copy(chain, "Leaf", same_library=True, flatten_symbol=False)
        assert symbol_conflicts(copy, target, chain, same=True) == ["Leaf"]
        for name, message in (
            ("Root", "that it descends from"),
            ("Child", "that is a descendent of it"),
        ):
            copy = symbol_copy(chain, name, same_library=True, flatten_symbol=False)
            with pytest.raises(CopyError, match=message):
                symbol_conflicts(copy, target, chain, same=True)
        copy = symbol_copy(chain, "Leaf", same_library=False, flatten_symbol=False)
        assert symbol_conflicts(copy, target, chain, same=False) == ["Root", "Leaf"]
        # To an other library, the leaf cannot get the name of a parent that goes too.
        copy = symbol_copy(chain, "Root", same_library=False, flatten_symbol=False)
        with pytest.raises(CopyError, match="name of a parent"):
            symbol_conflicts(copy, {}, chain, same=False)

    def test_library_text(self, tmp_path):
        chain = chain_of(tmp_path, library(symbol("A")), "A")
        copy = symbol_copy(chain, "A", same_library=False, flatten_symbol=False)
        text = library_text(library(symbol("A", value="old"), symbol("B")), copy, ["A"])
        assert text.count('(symbol "A"') == 1 and '(property "Value" "A"' in text
        assert '(symbol "B"' in text


# The tools.


def fake_upgrade(kind, source, output, force):
    """kicad-cli sym/fp upgrade: a copy of the input (the files are in the version)."""
    if kind == "symbol":
        shutil.copyfile(source, output)
    else:
        shutil.copytree(source, output)


@pytest.fixture(autouse=True)
def kicad_10(monkeypatch, tmp_path):
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)

    async def version():
        return "10.0.6"

    monkeypatch.setattr(project_libraries, "get_kicad_version", version)
    monkeypatch.setattr(library_copy, "run_upgrade", fake_upgrade)
    monkeypatch.setattr(library_upgrade, "run_upgrade", fake_upgrade)
    config = tmp_path / "config" / "10.0"
    config.mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
    stock = tmp_path / "stock"
    stock.mkdir()
    (stock / "Amp.kicad_sym").write_text(
        library(symbol("NE5534"), symbol("AD797", extends="NE5534")), encoding="utf-8"
    )
    (stock / "Res.pretty").mkdir()
    (stock / "Res.pretty" / "R_0603.kicad_mod").write_text(
        f'(footprint "R_0603"\n\t(version {V_FP})\n\t(layer "F.Cu")\n'
        '\t(property "Value" "R_0603" (at 0 0 0))\n)\n',
        encoding="utf-8",
    )
    (stock / "Res.pretty" / "R_Back.kicad_mod").write_text(
        f'(footprint "R_Back"\n\t(version {V_FP})\n\t(layer "B.Cu")\n)\n', encoding="utf-8"
    )

    def row(name, uri):
        uri = str(uri).replace("\\", "/")
        return f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr ""))\n'

    (config / "sym-lib-table").write_text(
        f"(sym_lib_table\n\t(version 7)\n{row('Amp', stock / 'Amp.kicad_sym')})\n", "utf-8"
    )
    (config / "fp-lib-table").write_text(
        f"(fp_lib_table\n\t(version 7)\n{row('Res', stock / 'Res.pretty')})\n", "utf-8"
    )


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "board"
    folder.mkdir()
    (folder / "board.kicad_pro").write_text("{}", encoding="utf-8")
    (folder / "Mine.kicad_sym").write_text(library(symbol("R")), encoding="utf-8")
    (folder / "Mine.pretty").mkdir()
    (folder / "sym-lib-table").write_text(
        '(sym_lib_table\n\t(version 7)\n\t(lib (name "Mine") (type "KiCad") '
        '(uri "${KIPRJMOD}/Mine.kicad_sym") (options "") (descr ""))\n)\n',
        encoding="utf-8",
    )
    (folder / "fp-lib-table").write_text(
        '(fp_lib_table\n\t(version 7)\n\t(lib (name "Mine") (type "KiCad") '
        '(uri "${KIPRJMOD}/Mine.pretty") (options "") (descr ""))\n)\n',
        encoding="utf-8",
    )
    return folder / "board.kicad_pro"


class TestCopyTools:
    async def test_symbol_with_its_parent(self, project):
        out = await copy_symbol_to_library(str(project), "Amp:AD797", "Mine")
        assert out.startswith("✅ The symbol 'AD797' is in the library 'Mine'."), out
        assert "Symbols written: NE5534, AD797" in out
        names = read_library(project.parent / "Mine.kicad_sym").keys()
        assert sorted(names) == ["AD797", "NE5534", "R"]
        again = await copy_symbol_to_library(str(project), "Amp:AD797", "Mine")
        assert "already exist in library 'Mine': NE5534, AD797" in again
        out = await copy_symbol_to_library(str(project), "Amp:AD797", "Mine", overwrite=True)
        assert "Replaced: NE5534, AD797" in out, out

    @pytest.mark.file_versions
    async def test_older_target_and_unknown_items(self, project):
        (project.parent / "Mine.kicad_sym").write_text(
            library(symbol("R"), version=20241209), encoding="utf-8"
        )
        out = await copy_symbol_to_library(str(project), "Amp:AD797", "Mine")
        assert "convert it first with upgrade_project_libraries" in out, out
        out = await copy_symbol_to_library(str(project), "Amp:Nope", "Mine")
        assert out.startswith("❌") and "no symbol" in out
        out = await copy_symbol_to_library(str(project), "AD797", "Mine")
        assert out.startswith("❌ The library ID")
        out = await copy_symbol_to_library(str(project), "Amp:AD797", "Other")
        assert out.startswith("❌") and "no library 'Other'" in out

    async def test_footprint(self, project):
        out = await copy_footprint_to_library(str(project), "Res:R_0603", "Mine", "R 0603 Mine")
        assert out.startswith("✅ The footprint 'R 0603 Mine'"), out
        text = (project.parent / "Mine.pretty" / "R 0603 Mine.kicad_mod").read_text("utf-8")
        assert '(property "Value" "R 0603 Mine"' in text
        again = await copy_footprint_to_library(str(project), "Res:R_0603", "Mine", "R 0603 Mine")
        assert again.startswith("❌ Footprint R 0603 Mine already exists in Mine.")

    @pytest.mark.parametrize(
        ("lib_id", "name", "message"),
        [
            ("Res:R_Back", None, "is not on F.Cu"),
            ("Res:Nope", None, 'has no footprint "Nope"'),
            ("Res:R_0603", "a*b", "file name"),
        ],
    )
    async def test_refused_footprints(self, project, lib_id, name, message):
        out = await copy_footprint_to_library(str(project), lib_id, "Mine", name)
        assert out.startswith("❌") and message in out, out
        assert list((project.parent / "Mine.pretty").iterdir()) == []

    async def test_footprint_onto_itself_and_a_disabled_target(self, project):
        await copy_footprint_to_library(str(project), "Res:R_0603", "Mine")
        out = await copy_footprint_to_library(str(project), "Mine:R_0603", "Mine")
        assert out.startswith("❌ Footprint R_0603 already exists in Mine."), out
        out = await copy_footprint_to_library(str(project), "Mine:R_0603", "Mine", overwrite=True)
        assert out.startswith("✅") and "(replaced)" in out, out
        table = project.parent / "fp-lib-table"
        table.write_text(
            table.read_text("utf-8").replace('(descr ""))', '(descr "") (disabled))'), "utf-8"
        )
        out = await copy_footprint_to_library(str(project), "Res:R_0603", "Mine", "R2")
        assert out.startswith("✅") and "is disabled" in out, out

    async def test_open_project(self, project):
        (project.parent / "~board.kicad_pro.lck").write_text("{}", encoding="utf-8")
        out = await copy_symbol_to_library(str(project), "Amp:AD797", "Mine")
        assert out.startswith("❌"), out
        assert sorted(read_library(project.parent / "Mine.kicad_sym")) == ["R"]
