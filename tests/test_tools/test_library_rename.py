"""Tests of the rename and the delete of library items (utils/library_rename.py,
utils/library_uses.py and the tools rename_library_symbol, delete_library_symbol,
rename_library_footprint and delete_library_footprint), with the rules of the library
tree of the symbol editor and of the footprint editor of KiCad 10.0.6. kicad-cli is
replaced by a copy of the file; the check with KiCad is
spike/kicad-versions/library_rename_check.py."""

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.parsers.lib_symbols import read_library
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import project_libraries
from kicad_workbench.tools.project_libraries import (
    delete_library_footprint,
    delete_library_symbol,
    rename_library_footprint,
    rename_library_symbol,
)
from kicad_workbench.utils.file_save import SaveFilesError
from kicad_workbench.utils.library_rename import (
    deleted_library,
    derived_symbols,
    descendants,
    footprint_rename_problem,
    renamed_library,
    renamed_symbol_name,
    symbol_in_use,
)

from . import test_library_copy as copy_tests
from .test_library_copy import V_FP, library, symbol

# The fixtures of the copy tests: kicad-cli replaced by a copy, a project with the
# libraries Mine (symbols and footprints).
kicad_10 = copy_tests.kicad_10
project = copy_tests.project

# A parent, its child and grandchild, and an other child.
FAMILY = library(
    symbol("A"),
    symbol("B", value="B", extends="A"),
    symbol("C", value="x", extends="B"),
    symbol("D", value="D", extends="A"),
    symbol("E"),
)


def _read(tmp_path, text):
    path = tmp_path / "lib.kicad_sym"
    path.write_text(text, encoding="utf-8")
    return read_library(path), parse(text)


def _footprint(name, value):
    return (
        f'(footprint "{name}"\n\t(version {V_FP})\n\t(layer "F.Cu")\n'
        f'\t(property "Value" "{value}" (at 0 0 0))\n)\n'
    )


def _schematic(folder, items):
    (folder / "board.kicad_sch").write_text(
        '(kicad_sch (version 20260306) (generator "eeschema") (uuid "a0")\n'
        f"  (lib_symbols)\n{items})\n",
        encoding="utf-8",
    )


def _placed(lib_id, reference, footprint="", uuid="b1"):
    return (
        f'  (symbol (lib_id "{lib_id}") (at 0 0 0) (unit 1) (uuid "{uuid}")\n'
        f'    (property "Reference" "{reference}" (at 0 0 0))\n'
        '    (property "Value" "1k" (at 0 0 0))\n'
        f'    (property "Footprint" "{footprint}" (at 0 0 0))\n'
        f'    (instances (project "board" (path "/a0" (reference "{reference}") (unit 1)))))\n'
    )


class TestRules:
    @pytest.mark.parametrize(
        ("text", "name"),
        [
            (" LM358 B\t", "LM358 B"),  # trim of the two ends; a space stays
            ('a:b"c', "a{colon}b{dblquote}c"),
        ],
    )
    def test_the_name(self, text, name):
        assert renamed_symbol_name(text) == name

    def test_name_in_use(self, tmp_path):
        symbols, _root = _read(tmp_path, FAMILY)
        # SymbolNameInUse: CmpNoCase; the symbol itself is not a conflict.
        assert symbol_in_use(symbols, "A", "e") == "E"
        assert symbol_in_use(symbols, "A", "a") is None
        assert symbol_in_use(symbols, "A", "F") is None

    def test_descendants(self, tmp_path):
        symbols, _root = _read(tmp_path, FAMILY)
        # GetDerivedSymbolNames: each child, then its descendants.
        children = derived_symbols(symbols)
        assert descendants(children, "A") == ["B", "C", "D"]
        assert descendants(children, "C") == []

    def test_rename_a_parent(self, tmp_path):
        symbols, root = _read(tmp_path, FAMILY)
        text, follows = renamed_library(root, symbols, "A", "A2")
        assert follows
        renamed = read_library_text(tmp_path, text)
        assert sorted(renamed) == ["A2", "B", "C", "D", "E"]
        assert parse(text).first("symbol").value(0) == "A2"
        assert '(symbol "A2_1_1"' in text and '(property "Value" "A2"' in text
        # The children extend the new name; the grandchild keeps its parent.
        assert text.count('(extends "A2")') == 2 and '(extends "B")' in text

    def test_rename_keeps_an_other_value(self, tmp_path):
        symbols, root = _read(tmp_path, FAMILY)
        text, follows = renamed_library(root, symbols, "C", "C2")
        assert not follows and '(property "Value" "x"' in text

    def test_a_duplicate_name_goes(self, tmp_path):
        # KiCad loads only the last (symbol "E") of the file.
        symbols, root = _read(tmp_path, library(symbol("E", value="1"), symbol("E", value="2")))
        text, _follows = renamed_library(root, symbols, "E", "F")
        assert '(symbol "E"' not in text and '(property "Value" "2"' in text

    def test_delete(self, tmp_path):
        _symbols, root = _read(tmp_path, FAMILY)
        text = deleted_library(root, {"A", "B", "C", "D"})
        assert sorted(read_library_text(tmp_path, text)) == ["E"]

    @pytest.mark.parametrize(
        ("name", "problem"),
        [
            ("", "Footprint must have a name."),
            ("a\nb", "line break"),
            ("a/b", "KiCad changes them"),
            ("a:b", "KiCad changes them"),
            ("a%b $c", None),  # the rename dialog has no validator
        ],
    )
    def test_footprint_name(self, name, problem):
        result = footprint_rename_problem(name)
        assert result is None if problem is None else problem in result


def read_library_text(tmp_path, text):
    path = tmp_path / "result.kicad_sym"
    path.write_text(text, encoding="utf-8")
    return read_library(path)


class TestSymbolTools:
    @pytest.fixture
    def family(self, project):
        (project.parent / "Mine.kicad_sym").write_text(FAMILY, encoding="utf-8")
        return project

    async def test_rename(self, family):
        out = await rename_library_symbol(str(family), "Mine:A", " A 2 ")
        assert out.startswith("✅ The symbol 'A' of the library 'Mine' has the new name 'A 2'."), (
            out
        )
        assert "- The derived symbols B, D extend 'A 2'." in out
        assert "- The Value was the old name" in out
        assert "no symbol with the lib_id Mine:A." in out
        text = (family.parent / "Mine.kicad_sym").read_text("utf-8")
        assert '(symbol "A 2"' in text and text.count('(extends "A 2")') == 2

    async def test_rename_errors(self, family):
        out = await rename_library_symbol(str(family), "Mine:A", "e")
        assert out.startswith(
            "❌ Symbol 'e' already exists in library 'Mine'. (The library has 'E'"
        ), out
        assert await rename_library_symbol(str(family), "Mine:A", "  ") == (
            "❌ Symbol must have a name."
        )
        out = await rename_library_symbol(str(family), "Mine:Nope", "Y")
        assert "has no symbol 'Nope'" in out
        before = (family.parent / "Mine.kicad_sym").read_bytes()
        out = await rename_library_symbol(str(family), "Mine:A", "A")
        assert out.endswith("has this name: no file written."), out
        assert (family.parent / "Mine.kicad_sym").read_bytes() == before

    async def test_a_change_of_case(self, family):
        out = await rename_library_symbol(str(family), "Mine:E", "e")
        assert out.startswith("✅"), out
        assert "e" in read_library(family.parent / "Mine.kicad_sym")

    async def test_delete(self, family):
        out = await delete_library_symbol(str(family), "Mine:A")
        assert out.startswith(
            "❌ Deleting a base symbol will delete all symbols derived from it: B, C, D."
        ), out
        out = await delete_library_symbol(str(family), "Mine:A", delete_derived=True)
        assert out.startswith("✅ The symbol 'A' is deleted from the library 'Mine'."), out
        assert "deleted too (as KiCad does): B, C, D." in out
        assert sorted(read_library(family.parent / "Mine.kicad_sym")) == ["E"]

    async def test_the_uses_in_the_schematic(self, family):
        _schematic(
            family.parent,
            _placed("Mine:B", "U2", uuid="b1") + _placed("Mine:B", "U10", uuid="b2")
            + _placed("Mine:E", "U1", uuid="b3"),
        )  # fmt: skip
        out = await delete_library_symbol(str(family), "Mine:A", delete_derived=True)
        assert "⚠️ The project uses Mine:B. KiCad does not change" in out, out
        # StrNumCmp order of the references.
        assert "- Schematic: the symbols U2, U10 have the lib_id Mine:B." in out
        out = await rename_library_symbol(str(family), "Mine:E", "F")
        assert "- Schematic: the symbol U1 has the lib_id Mine:E." in out, out


class TestFootprintTools:
    @pytest.fixture
    def footprints(self, project):
        folder = project.parent / "Mine.pretty"
        (folder / "R_0603.kicad_mod").write_text(_footprint("R_0603", "R_0603"), "utf-8")
        (folder / "C_0603.kicad_mod").write_text(_footprint("C_0603", "C"), "utf-8")
        return project

    async def test_rename(self, footprints):
        folder = footprints.parent / "Mine.pretty"
        out = await rename_library_footprint(str(footprints), "Mine:R_0603", " R 0603 ")
        assert out.startswith(
            "✅ The footprint 'R_0603' of the library 'Mine' has the new name 'R 0603'."
        ), out
        assert "- The Value was the old name" in out
        assert not (folder / "R_0603.kicad_mod").exists()
        assert '(property "Value" "R 0603"' in (folder / "R 0603.kicad_mod").read_text("utf-8")
        out = await rename_library_footprint(str(footprints), "Mine:C_0603", "C1")
        assert "- The Value was the old name" not in out, out
        assert '(property "Value" "C"' in (folder / "C1.kicad_mod").read_text("utf-8")

    async def test_rename_errors(self, footprints):
        out = await rename_library_footprint(str(footprints), "Mine:R_0603", "C_0603")
        assert out.startswith("❌ Footprint 'C_0603' already exists in library 'Mine'."), out
        out = await rename_library_footprint(str(footprints), "Mine:R_0603", "a/b")
        assert "KiCad changes them to '_' in the file name" in out
        out = await rename_library_footprint(str(footprints), "Mine:Nope", "X")
        assert "has no footprint 'Nope'" in out
        out = await rename_library_footprint(str(footprints), "Mine:R_0603", "R_0603")
        assert out.endswith("has this name: no file written."), out

    async def test_delete_and_the_uses(self, footprints):
        folder = footprints.parent
        _schematic(folder, _placed("Mine:R", "R1", footprint="Mine:R_0603"))
        (folder / "board.kicad_pcb").write_text(
            '(kicad_pcb (version 20260206)\n  (footprint "Mine:R_0603" (layer "F.Cu")\n'
            '    (property "Reference" "R1" (at 0 0 0))))\n',
            encoding="utf-8",
        )
        (folder / "Mine.kicad_sym").write_text(
            library(symbol("R").replace(
                '(property "Value" "R" (at 0 0 0))',
                '(property "Value" "R" (at 0 0 0))\n\t\t(property "Footprint" "Mine:R_0603" (at 0 0 0))',
            )),
            encoding="utf-8",
        )  # fmt: skip
        out = await delete_library_footprint(str(footprints), "Mine:R_0603")
        assert out.startswith("✅ The footprint 'R_0603' is deleted from the library 'Mine'."), out
        assert not (folder / "Mine.pretty" / "R_0603.kicad_mod").exists()
        assert "- Schematic: the symbol R1 has the Footprint field Mine:R_0603." in out
        assert "- Board: the footprint R1 has the library ID Mine:R_0603." in out
        assert "- Symbol library 'Mine': the symbol R has the Footprint field Mine:R_0603." in out

    async def test_no_uses(self, footprints):
        out = await delete_library_footprint(str(footprints), "Mine:C_0603")
        assert "- The project has no use of Mine:C_0603" in out, out

    async def test_the_lock_of_the_project(self, footprints):
        (footprints.parent / "~board.kicad_pro.lck").write_text("{}", encoding="utf-8")
        out = await delete_library_footprint(str(footprints), "Mine:C_0603")
        assert out.startswith("❌"), out
        assert (footprints.parent / "Mine.pretty" / "C_0603.kicad_mod").exists()


class TestOneFile:
    """A change of only the case on a file system with no difference of case: a hard
    link gives the two names one file, as such a file system does."""

    @pytest.fixture
    def one_file(self, footprints_project, monkeypatch):
        folder = footprints_project.parent / "Mine.pretty"
        (folder / "r_0603.kicad_mod").hardlink_to(folder / "R_0603.kicad_mod")
        # The folder shows one entry, as such a file system does.
        monkeypatch.setattr(
            project_libraries,
            "_entry_names",
            lambda path: {entry.name for entry in path.iterdir()} - {"r_0603.kicad_mod"},
        )
        return footprints_project

    @pytest.fixture
    def footprints_project(self, project):
        folder = project.parent / "Mine.pretty"
        (folder / "R_0603.kicad_mod").write_text(_footprint("R_0603", "R_0603"), "utf-8")
        return project

    async def test_kicad_10_0_6_refuses(self, one_file):
        out = await rename_library_footprint(str(one_file), "Mine:R_0603", "r_0603")
        assert out.startswith("❌ The file system of") and "KiCad 10.0.7" in out, out
        assert (one_file.parent / "Mine.pretty" / "R_0603.kicad_mod").exists()

    async def test_kicad_10_0_7_deletes_first(self, one_file, monkeypatch):
        async def version():
            return "10.0.7"

        monkeypatch.setattr(project_libraries, "get_kicad_version", version)
        folder = one_file.parent / "Mine.pretty"
        out = await rename_library_footprint(str(one_file), "Mine:R_0603", "r_0603")
        assert out.startswith("✅"), out
        assert "- Removed:" not in out
        assert sorted(p.name for p in folder.iterdir()) == ["r_0603.kicad_mod"]
        assert '(property "Value" "r_0603"' in (folder / "r_0603.kicad_mod").read_text("utf-8")

    async def test_the_old_file_comes_back(self, one_file, monkeypatch):
        async def version():
            return "10.0.7"

        def failed(files):
            raise SaveFilesError("The tool wrote no file. Disk full.")

        monkeypatch.setattr(project_libraries, "get_kicad_version", version)
        monkeypatch.setattr(project_libraries, "save_files", failed)
        folder = one_file.parent / "Mine.pretty"
        before = (folder / "R_0603.kicad_mod").read_bytes()
        out = await rename_library_footprint(str(one_file), "Mine:R_0603", "r_0603")
        assert out.startswith("❌ The tool wrote no file. Disk full.") and "again" in out, out
        assert (folder / "R_0603.kicad_mod").read_bytes() == before

    async def test_a_link_with_an_other_name(self, footprints_project):
        folder = footprints_project.parent / "Mine.pretty"
        (folder / "X.kicad_mod").symlink_to(folder / "R_0603.kicad_mod")
        out = await rename_library_footprint(str(footprints_project), "Mine:R_0603", "X")
        assert out.startswith("❌ Footprint 'X' already exists"), out

    async def test_a_hard_link_with_an_other_name(self, footprints_project):
        # Two entries of one file: an other footprint for KiCad.
        folder = footprints_project.parent / "Mine.pretty"
        (folder / "r_0603.kicad_mod").hardlink_to(folder / "R_0603.kicad_mod")
        out = await rename_library_footprint(str(footprints_project), "Mine:R_0603", "r_0603")
        assert out.startswith("❌ Footprint 'r_0603' already exists"), out


class TestUseProblems:
    async def test_rows_that_the_tool_does_not_read(self, project):
        folder = project.parent
        (folder / "Mine.pretty" / "F.kicad_mod").write_text(_footprint("F", "F"), "utf-8")
        (folder / "Dir").mkdir()
        (folder / "Dir" / "S.kicad_sym").write_text(
            library(symbol("S").replace(
                '(property "Value" "S" (at 0 0 0))',
                '(property "Value" "S" (at 0 0 0))\n\t\t(property "Footprint" "Mine:F" (at 0 0 0))',
            )),
            encoding="utf-8",
        )  # fmt: skip
        (folder / "sym-lib-table").write_text(
            "(sym_lib_table\n\t(version 7)\n"
            '\t(lib (name "Dir") (type "KiCad") (uri "${KIPRJMOD}/Dir") (options "") (descr ""))\n'
            '\t(lib (name "Old") (type "Legacy") (uri "${KIPRJMOD}/old.lib") (options "") (descr ""))\n'
            '\t(lib (name "Gone") (type "KiCad") (uri "${KIPRJMOD}/gone.kicad_sym") (options "") (descr ""))\n'
            ")\n",
            encoding="utf-8",
        )  # fmt: skip
        out = await delete_library_footprint(str(project), "Mine:F")
        # A folder of symbol files is a library too.
        assert "- Symbol library 'Dir': the symbol S has the Footprint field Mine:F." in out, out
        assert "'Old' is not of the KiCad format" in out
        assert "'Gone'" in out and "is not there" in out

    async def test_a_problem_of_the_hierarchy(self, project):
        (project.parent / "Mine.kicad_sym").write_text(library(symbol("R")), "utf-8")
        # A sheet with no valid UUID (sheet_list gives it as a problem).
        _schematic(
            project.parent,
            _placed("Mine:R", "R1") + '  (sheet (at 0 0) (size 10 10) (uuid "no-uuid")\n'
            '    (property "Sheetname" "S" (at 0 0 0))\n'
            '    (property "Sheetfile" "missing.kicad_sch" (at 0 0 0)))\n',
        )
        out = await rename_library_symbol(str(project), "Mine:R", "R2")
        assert "the symbol R1 has the lib_id Mine:R." in out, out
        assert "⚠️ The schematic has a problem: The sheet 'S'" in out, out


def test_a_symbol_that_extends_itself(tmp_path):
    symbols, root = _read(tmp_path, library(symbol("A"), symbol("B", extends="B")))
    with pytest.raises(InputError, match="extends itself"):
        renamed_library(root, symbols, "B", "C")
