"""Tests of the project library tables (utils/project_library_tables.py) and of the
tools that change them (tools/project_libraries.py), with the rules of KiCad 10.0.6."""

import json
import os
import re
from pathlib import Path

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.tools import project_libraries
from kicad_workbench.tools.project_libraries import (
    add_library_to_table,
    create_project_library,
    remove_library_from_table,
    set_library_table_row,
)
from kicad_workbench.utils import library_tables
from kicad_workbench.utils.library_tables import LibraryTableError
from kicad_workbench.utils.project_library_tables import (
    NEW_SYMBOL_LIBRARY,
    ProjectTable,
    TableRow,
    default_nickname,
    escape_nickname,
    fix_illegal_chars,
    format_table,
    library_is_kicad,
    nickname_problem,
    normalize_path,
    read_project_table,
    table_problems,
    value_problem,
)

# The global symbol table that KiCad 10.0.6 wrote on a machine with a Czech user
# interface (LIBRARY_MANAGER::CreateGlobalTable, then a save of the table dialog).
KICAD_WRITTEN = (
    "(sym_lib_table\n\t(version 7)\n"
    '\t(lib (name "KiCad") (type "Table") (uri "/usr/share/kicad/template/sym-lib-table") '
    '(options "") (descr "Výchozí knihovny KiCad"))\n'
    ")\n"
).encode()


# A project symbol table that the table dialog of KiCad 10.0.6 wrote (2026-11-16):
# a row with a description, Active and Visible off.
KICAD_WRITTEN_FLAGS = (
    b"(sym_lib_table\n\t(version 7)\n"
    b'\t(lib (name "BIRDIE-M1") (type "KiCad") (uri "${KIPRJMOD}/BIRDIE-M1.kicad_sym") '
    b'(options "") (descr "test") (disabled) (hidden))\n'
    b")\n"
)


def row_line(name, uri, descr="", flags=""):
    return (
        f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") '
        f'(descr "{descr}"){flags})\n'
    )


def table_bytes(kind, *lines):
    head = {"symbol": "sym_lib_table", "footprint": "fp_lib_table"}[kind]
    return (f"({head}\n\t(version 7)\n" + "".join(lines) + ")\n").encode()


class TestTableFormat:
    def test_round_trip_of_a_table_that_kicad_wrote(self, tmp_path):
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "sym-lib-table").write_bytes(KICAD_WRITTEN)
        table = read_project_table(tmp_path / "board.kicad_pro", "symbol")
        assert format_table("symbol", table.rows) == KICAD_WRITTEN

    def test_flags_that_kicad_wrote(self, tmp_path):
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "sym-lib-table").write_bytes(KICAD_WRITTEN_FLAGS)
        table = read_project_table(tmp_path / "board.kicad_pro", "symbol")
        assert (table.rows[0].disabled, table.rows[0].hidden) == (True, True)
        assert format_table("symbol", table.rows) == KICAD_WRITTEN_FLAGS
        row = TableRow(
            b"BIRDIE-M1", b"KiCad", b"${KIPRJMOD}/BIRDIE-M1.kicad_sym", b"", b"test", True, True
        )
        assert format_table("symbol", [row]) == KICAD_WRITTEN_FLAGS

    def test_flags_and_raw_values(self):
        # A value keeps its bytes: KiCad 10.0 reads "a \\\\ b" as two backslashes.
        rows = [
            TableRow(b"A", b"KiCad", b"/a", b"", b"a \\\\ b", disabled=True, hidden=True),
            TableRow(b"B\xff", b"KiCad", b"/b", b"x=1", b""),
        ]
        assert format_table("footprint", rows) == (
            b"(fp_lib_table\n\t(version 7)\n"
            b'\t(lib (name "A") (type "KiCad") (uri "/a") (options "") (descr "a \\\\ b") '
            b"(disabled) (hidden))\n"
            b'\t(lib (name "B\xff") (type "KiCad") (uri "/b") (options "x=1") (descr ""))\n'
            b")\n"
        )

    def test_empty_table(self):
        assert format_table("symbol", []) == b"(sym_lib_table\n\t(version 7)\n)\n"

    def test_quote_in_a_token_value(self):
        with pytest.raises(InputError, match="has a quote"):
            format_table("symbol", [TableRow(b"A", b"KiCad", b"/a", b"", b'a"b')])

    def test_new_symbol_library(self):
        # The bytes of `kicad-cli sym upgrade --force` of an empty library, KiCad 10.0.6.
        assert NEW_SYMBOL_LIBRARY == (
            '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "kicad_symbol_editor")\n'
            '\t(generator_version "10.0")\n)\n'
        )


class TestReadProjectTable:
    def project(self, tmp_path, data=None):
        project = tmp_path / "board.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        if data is not None:
            (tmp_path / "sym-lib-table").write_bytes(data)
        return project

    @pytest.mark.parametrize("data", [None, b"", b"\n"])
    def test_no_rows(self, tmp_path, data):
        table = read_project_table(self.project(tmp_path, data), "symbol")
        assert table.rows == []
        assert (table.stamp is None) == (data is None)

    @pytest.mark.parametrize(
        "data",
        [table_bytes("symbol", row_line("A", "/a", 'x \\"y\\"')), table_bytes("footprint")],
    )
    def test_table_that_kicad_cannot_use(self, tmp_path, data):
        with pytest.raises(LibraryTableError):
            read_project_table(self.project(tmp_path, data), "symbol")


class TestNicknames:
    @pytest.mark.parametrize(
        ("nickname", "problem"),
        [
            ("Lib", None),
            ("Lib <1>", None),
            ("", "Library must have a nickname."),
            ("  ", "Library must have a nickname."),
            (" Lib", "white space"),
            ("a:b", "Illegal character ':'"),
            ("a\\b", "Illegal character '\\\\\\\\'"),
            ("a\tb", "Illegal character '\\\\t'"),
        ],
    )
    def test_nickname_problem(self, nickname, problem):
        result = nickname_problem(nickname)
        assert result is None if problem is None else re.search(problem, result)

    def test_fix_escape_and_default(self):
        assert fix_illegal_chars("a:b\\c\td é") == "a_b_c_d é"
        assert escape_nickname('A<B>"C"\n') == "A{lt}B{gt}{dblquote}C{dblquote}"
        assert default_nickname(Path("/x/My.lib.kicad_sym"), "symbol") == "My.lib"
        assert default_nickname(Path("/x/fp.pretty"), "footprint") == "fp"
        # A KiCad footprint folder with an other extension keeps it.
        assert default_nickname(Path("/x/fp.lib"), "footprint") == "fp.lib"
        assert default_nickname(Path("/x/a:b.kicad_sym"), "symbol") == "a_b"

    @pytest.mark.parametrize("value", ['a"b', "a\\b", "a\nb", "a\rb"])
    def test_value_problem(self, value):
        assert "KiCad 10.0 writes them with an escape" in value_problem(value, "description")
        assert value_problem("a\tb é", "description") is None

    def test_table_problems(self):
        rows = [
            TableRow(b"A", b"KiCad", b"/a", b"", b""),
            TableRow(b"A", b"KiCad", b"/b", b"", b""),
            TableRow(b"B", b"KiCad", b" ", b"", b""),
            TableRow(b"a:b", b"KiCad", b"/c", b"", b""),
        ]
        assert table_problems(rows) == [
            "The row 'B' has no URI: the table dialog removes it.",
            "Illegal character ':' in nickname 'a:b'.",
            "Multiple libraries cannot share the same nickname ('A').",
        ]


class TestNormalizePath:
    def test_rules(self, tmp_path):
        libs = tmp_path / "libs"
        deep = libs / "sub"
        deep.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        variables = {
            "B_LIBS": str(libs),
            "A_LIBS": str(libs) + "/",
            "DEEP": str(deep),
            "NO_FOLDER": str(tmp_path / "none"),
            "RELATIVE": "libs",
        }
        # The variable with the most folder levels wins.
        assert normalize_path(deep / "x" / "a.kicad_sym", variables, project) == (
            "${DEEP}/x/a.kicad_sym"
        )
        # An equal count: the first name in the sequence of the names.
        assert normalize_path(libs / "a.kicad_sym", variables, project) == "${A_LIBS}/a.kicad_sym"
        # No variable: the project folder, else the absolute path.
        assert normalize_path(project / "l" / "a.pretty", variables, project) == (
            "${KIPRJMOD}/l/a.pretty"
        )
        other = tmp_path / "other" / "a.kicad_sym"
        assert normalize_path(other, variables, project) == str(other).replace("\\", "/")


class TestLibraryIsKicad:
    def test_symbol(self, tmp_path):
        (tmp_path / "a.KICAD_SYM").write_text("x", encoding="utf-8")
        assert library_is_kicad(tmp_path / "a.KICAD_SYM", "symbol")
        assert not library_is_kicad(tmp_path / "missing.kicad_sym", "symbol")
        folder = tmp_path / "dir"
        folder.mkdir()
        assert not library_is_kicad(folder, "symbol")
        (folder / ".hidden.kicad_sym").write_text("x", encoding="utf-8")
        assert not library_is_kicad(folder, "symbol")
        (folder / "b.kicad_sym").write_text("x", encoding="utf-8")
        assert library_is_kicad(folder, "symbol")

    def test_footprint(self, tmp_path):
        folder = tmp_path / "a.pretty"
        folder.mkdir()
        assert library_is_kicad(folder, "footprint")  # empty: a new library
        (folder / "readme.txt").write_text("x", encoding="utf-8")
        assert not library_is_kicad(folder, "footprint")
        (folder / "R.Kicad_Mod").write_text("x", encoding="utf-8")
        assert library_is_kicad(folder, "footprint")
        assert not library_is_kicad(folder / "R.Kicad_Mod", "footprint")


# The tools.


@pytest.fixture(autouse=True)
def kicad_10(monkeypatch, tmp_path):
    """KiCad 10.0.6 with a global symbol table in tmp_path, and no path variables of the
    developer."""
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)

    async def version():
        return "10.0.6"

    monkeypatch.setattr(project_libraries, "get_kicad_version", version)
    config = tmp_path / "config"
    (config / "10.0").mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
    global_libs = tmp_path / "global"
    global_libs.mkdir()
    (global_libs / "Device.kicad_sym").write_text(NEW_SYMBOL_LIBRARY, encoding="utf-8")
    (global_libs / "Global_FP.pretty").mkdir()
    (config / "10.0" / "sym-lib-table").write_bytes(
        table_bytes(
            "symbol",
            row_line("Device", global_libs / "Device.kicad_sym"),
            row_line("Off", global_libs / "Device.kicad_sym", flags=" (disabled)"),
            row_line("Gone", global_libs / "Gone.kicad_sym"),
        )
    )
    (config / "10.0" / "fp-lib-table").write_bytes(
        table_bytes("footprint", row_line("Global_FP", global_libs / "Global_FP.pretty"))
    )
    return config / "10.0"


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "board"
    folder.mkdir()
    path = folder / "board.kicad_pro"
    path.write_text("{}", encoding="utf-8")
    return path


def table_of(project, kind="symbol"):
    name = "sym-lib-table" if kind == "symbol" else "fp-lib-table"
    return (project.parent / name).read_bytes()


class TestCreateProjectLibrary:
    async def test_symbol_library(self, project):
        out = await create_project_library(str(project), "symbol", "MyLib")
        assert out.startswith("✅ New symbol library 'MyLib'"), out
        library = project.parent / "MyLib.kicad_sym"
        assert library.read_text(encoding="utf-8") == NEW_SYMBOL_LIBRARY
        assert table_of(project) == table_bytes(
            "symbol", row_line("MyLib", "${KIPRJMOD}/MyLib.kicad_sym")
        )
        assert "open the project in KiCad again" in out

    async def test_footprint_library_in_an_other_folder(self, project, tmp_path):
        folder = tmp_path / "shared"
        folder.mkdir()
        out = await create_project_library(str(project), "footprint", "Parts", str(folder))
        assert out.startswith("✅ New footprint library (an empty folder) 'Parts'"), out
        assert list((folder / "Parts.pretty").iterdir()) == []
        uri = str(folder / "Parts.pretty").replace("\\", "/")
        assert table_of(project, "footprint") == table_bytes("footprint", row_line("Parts", uri))

    async def test_path_variable_of_kicad(self, project, tmp_path, kicad_10):
        folder = tmp_path / "mylibs"
        folder.mkdir()
        (kicad_10 / "kicad_common.json").write_text(
            json.dumps({"environment": {"vars": {"MYLIBS": str(folder)}}}), encoding="utf-8"
        )
        await create_project_library(str(project), "symbol", "A", str(folder))
        assert b'(uri "${MYLIBS}/A.kicad_sym")' in table_of(project)

    async def test_rows_of_the_table_stay(self, project):
        before = table_bytes("symbol", row_line("Old", "/x/old.kicad_sym", "a \\\\ b"))
        (project.parent / "sym-lib-table").write_bytes(before)
        await create_project_library(str(project), "symbol", "New")
        assert (
            table_of(project)
            == before[:-2] + row_line("New", "${KIPRJMOD}/New.kicad_sym").encode() + b")\n"
        )

    async def test_library_that_exists(self, project):
        (project.parent / "MyLib.kicad_sym").write_text("x", encoding="utf-8")
        out = await create_project_library(str(project), "symbol", "MyLib")
        assert out.startswith("❌ The library") and "add_library_to_table" in out
        assert not (project.parent / "sym-lib-table").exists()

    async def test_symbol_nickname_of_the_global_table(self, project):
        out = await create_project_library(str(project), "symbol", "Device")
        assert out.startswith("❌ Library 'Device' already exists."), out
        assert not (project.parent / "Device.kicad_sym").exists()
        # A disabled row, or a row whose library does not exist: KiCad did not load it.
        for name in ("Off", "Gone"):
            out = await create_project_library(str(project), "symbol", name)
            assert out.startswith("✅"), out

    async def test_footprint_nickname_of_the_global_table(self, project):
        out = await create_project_library(str(project), "footprint", "Global_FP")
        assert out.startswith("✅"), out
        assert "the global library is not available in this project" in out

    @pytest.mark.parametrize(
        ("name", "error"),
        [("a:b", "Illegal character"), ('a"b', "has the characters"), ("a/b", "not a file")],
    )
    async def test_bad_name(self, project, name, error):
        out = await create_project_library(str(project), "symbol", name)
        assert out.startswith("❌") and error in out

    async def test_table_that_kicad_cannot_read(self, project):
        bad = table_bytes("symbol", row_line("A", "/a", 'x \\"y\\"'))
        (project.parent / "sym-lib-table").write_bytes(bad)
        out = await create_project_library(str(project), "symbol", "New")
        assert out.startswith("❌") and "repair the table first" in out
        assert table_of(project) == bad
        assert not (project.parent / "New.kicad_sym").exists()

    async def test_project_open_in_kicad(self, project):
        (project.parent / "~board.kicad_pro.lck").write_text("{}", encoding="utf-8")
        out = await create_project_library(str(project), "symbol", "New")
        assert out.startswith("❌"), out
        assert not (project.parent / "New.kicad_sym").exists()

    @pytest.mark.parametrize("version", ["9.0.9", "10.99.0"])
    async def test_kicad_with_no_rules(self, project, monkeypatch, version):
        async def other():
            return version

        monkeypatch.setattr(project_libraries, "get_kicad_version", other)
        out = await create_project_library(str(project), "symbol", "New")
        assert "rules of KiCad 10.0 only" in out


class TestAddLibraryToTable:
    async def test_symbol_file(self, project, tmp_path):
        library = tmp_path / "x" / "Sensors.kicad_sym"
        library.parent.mkdir()
        library.write_text(NEW_SYMBOL_LIBRARY, encoding="utf-8")
        out = await add_library_to_table(str(project), "symbol", str(library), None, "My parts")
        assert out.startswith("✅ The library 'Sensors'"), out
        uri = str(library).replace("\\", "/")
        assert table_of(project) == table_bytes("symbol", row_line("Sensors", uri, "My parts"))

    async def test_escaped_nickname(self, project):
        library = project.parent / "a.kicad_sym"
        library.write_text(NEW_SYMBOL_LIBRARY, encoding="utf-8")
        await add_library_to_table(str(project), "symbol", str(library), "A<1>")
        assert b'(name "A{lt}1{gt}")' in table_of(project)
        out = await add_library_to_table(str(project), "symbol", str(library), "A<1>")
        assert out.startswith("❌ Multiple libraries cannot share the same nickname ('A<1>')")

    @pytest.mark.file_versions
    async def test_older_library(self, project):
        library = project.parent / "old.kicad_sym"
        library.write_text(
            '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor"))\n',
            encoding="utf-8",
        )
        out = await add_library_to_table(str(project), "symbol", str(library))
        assert out.startswith("✅"), out
        assert "older file version" in out and "kicad-cli sym upgrade" in out

    @pytest.mark.file_versions
    async def test_newer_library(self, project):
        folder = project.parent / "new.pretty"
        folder.mkdir()
        (folder / "R.kicad_mod").write_text('(footprint "R" (version 20991231))\n', "utf-8")
        # A hidden file of a symbol folder: wxDir of KiCad does not see it.
        symbols = project.parent / "symbols"
        symbols.mkdir()
        (symbols / "a.kicad_sym").write_text(NEW_SYMBOL_LIBRARY, encoding="utf-8")
        (symbols / ".b.kicad_sym").write_text(
            '(kicad_symbol_lib (version 20991231) (generator "x"))\n', encoding="utf-8"
        )
        out = await add_library_to_table(str(project), "footprint", str(folder))
        assert out.startswith("❌") and "The tool added no row." in out, out
        assert not (project.parent / "fp-lib-table").exists()
        out = await add_library_to_table(str(project), "symbol", "symbols")
        assert out.startswith("✅"), out

    async def test_relative_paths(self, project, monkeypatch, tmp_path):
        # A relative path is relative to the project folder, also for a relative project.
        monkeypatch.chdir(tmp_path)
        (project.parent / "libs").mkdir()
        relative = str(project.relative_to(tmp_path))
        out = await create_project_library(relative, "footprint", "Parts", "libs")
        assert out.startswith("✅"), out
        assert (project.parent / "libs" / "Parts.pretty").is_dir()
        assert b'(uri "${KIPRJMOD}/libs/Parts.pretty")' in table_of(project, "footprint")

    async def test_nickname_with_white_space_in_the_table(self, project):
        (project.parent / "sym-lib-table").write_bytes(table_bytes("symbol", row_line(" A", "/a")))
        library = project.parent / "A.kicad_sym"
        library.write_text(NEW_SYMBOL_LIBRARY, encoding="utf-8")
        out = await add_library_to_table(str(project), "symbol", str(library))
        assert out.startswith("❌ Multiple libraries cannot share the same nickname ('A')"), out

    async def test_footprint_folder(self, project):
        folder = project.parent / "Parts.lib"
        folder.mkdir()
        (folder / "R.kicad_mod").write_text('(footprint "R" (version 20260206))\n', "utf-8")
        await add_library_to_table(str(project), "footprint", str(folder))
        assert b'(name "Parts.lib")' in table_of(project, "footprint")

    @pytest.mark.parametrize(
        ("kind", "name", "error"),
        [
            ("symbol", "a.lib", "not a KiCad symbol library"),
            ("footprint", "a.kicad_sym", "not a KiCad footprint library"),
            ("symbol", "missing.kicad_sym", "does not exist"),
        ],
    )
    async def test_refused_library(self, project, kind, name, error):
        for file in ("a.lib", "a.kicad_sym"):
            (project.parent / file).write_text("x", encoding="utf-8")
        out = await add_library_to_table(str(project), kind, str(project.parent / name))
        assert out.startswith("❌") and error in out

    async def test_description_with_a_quote(self, project):
        library = project.parent / "a.kicad_sym"
        library.write_text(NEW_SYMBOL_LIBRARY, encoding="utf-8")
        out = await add_library_to_table(str(project), "symbol", str(library), None, 'a "b"')
        assert (
            out.startswith("❌ The description") and not (project.parent / "sym-lib-table").exists()
        )


class TestRemoveAndFlags:
    @pytest.fixture
    def rows(self, project):
        (project.parent / "sym-lib-table").write_bytes(
            table_bytes("symbol", row_line("A", "/a"), row_line("B{lt}1{gt}", "/b"))
        )
        (project.parent / "fp-lib-table").write_bytes(table_bytes("footprint", row_line("F", "/f")))
        return project

    async def test_remove(self, rows):
        out = await remove_library_from_table(str(rows), "symbol", "B<1>")
        assert out.startswith("✅ The library 'B<1>' is not in the project table now."), out
        assert table_of(rows) == table_bytes("symbol", row_line("A", "/a"))

    async def test_remove_unknown_and_duplicate(self, rows):
        out = await remove_library_from_table(str(rows), "symbol", "C")
        assert out.startswith("❌") and "'A', 'B{lt}1{gt}'" in out
        (rows.parent / "sym-lib-table").write_bytes(
            table_bytes("symbol", row_line("A", "/a"), row_line("A", "/a2"))
        )
        out = await remove_library_from_table(str(rows), "symbol", "A")
        assert out.startswith("❌") and "2 rows" in out

    async def test_flags(self, rows):
        out = await set_library_table_row(str(rows), "symbol", "A", enabled=False, visible=False)
        assert out.startswith("✅ The row of 'A' has new flags."), out
        assert table_of(rows) == table_bytes(
            "symbol",
            row_line("A", "/a", flags=" (disabled) (hidden)"),
            row_line("B{lt}1{gt}", "/b"),
        )
        out = await set_library_table_row(str(rows), "symbol", "A", enabled=False)
        assert "has these flags already: the tool wrote no file" in out
        out = await set_library_table_row(str(rows), "symbol", "A", enabled=True, visible=True)
        assert table_of(rows) == table_bytes(
            "symbol", row_line("A", "/a"), row_line("B{lt}1{gt}", "/b")
        )

    async def test_footprint_has_no_visible_flag(self, rows):
        out = await set_library_table_row(str(rows), "footprint", "F", visible=False)
        assert out.startswith("❌ A footprint table has no visible flag")
        out = await set_library_table_row(str(rows), "footprint", "F", enabled=False)
        assert out.startswith("✅")
        assert await set_library_table_row(str(rows), "footprint", "F") == (
            "❌ Give enabled, visible or the two."
        )


def test_table_that_changed_after_the_read(project):
    (project.parent / "sym-lib-table").write_bytes(table_bytes("symbol"))
    table = read_project_table(project, "symbol")
    (project.parent / "sym-lib-table").write_bytes(table_bytes("symbol", row_line("X", "/x")))
    with pytest.raises(InputError, match="changed after this tool read it"):
        project_libraries._write(project_libraries._Context(project, "10.0.6"), table, [])
    assert isinstance(table, ProjectTable)


@pytest.mark.parametrize(("version", "backup"), [("10.0.6", False), ("10.0.7", True)])
async def test_copy_of_the_old_table(project, monkeypatch, version, backup):
    # LIBRARY_TABLE::Save of KiCad 10.0.7 and later keeps the old table as ".bak".
    async def installed():
        return version

    monkeypatch.setattr(project_libraries, "get_kicad_version", installed)
    old = table_bytes("symbol", row_line("A", "/a"))
    (project.parent / "sym-lib-table").write_bytes(old)
    out = await set_library_table_row(str(project), "symbol", "A", enabled=False)
    assert out.startswith("✅"), out
    copy = project.parent / "sym-lib-table.bak"
    assert copy.exists() == backup
    if backup:
        assert copy.read_bytes() == old
    # A new table: no copy.
    await create_project_library(str(project), "footprint", "F")
    assert not (project.parent / "fp-lib-table.bak").exists()
