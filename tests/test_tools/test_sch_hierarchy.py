"""Tests for the sheet hierarchy of KiCad projects."""

import asyncio
import json
import os
import sys
import uuid as uuidlib
from pathlib import Path

import pytest

from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.utils.kicad_project import (
    NIL_UUID,
    TopLevelSheet,
    top_level_sheets,
)
from kicad_workbench.utils.sch_hierarchy import (
    AmbiguousRootError,
    find_root_schematic,
    is_project_root,
    roots_containing,
    schematic_project_file,
    sheet_files,
    sheet_paths,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def sch(*sheet_files: str, key: str = "Sheetfile") -> str:
    """Return a schematic with one sheet symbol for each sheet file."""
    sheets = "".join(
        f"""\t(sheet
\t\t(at 100 50)
\t\t(size 20 10)
\t\t(uuid "00000000-0000-4000-8000-{index:012d}")
\t\t(property "Sheetname" "S{index}" (at 100 49 0))
\t\t(property "{key}" "{name}" (at 100 61 0))
\t)
"""
        for index, name in enumerate(sheet_files)
    )
    return (
        f'(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n'
        f'\t(uuid "{uuidlib.uuid4()}")\n{sheets})\n'
    )


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or sch(), encoding="utf-8")
    return path


def project(directory: Path, name: str, *sheet_names: str) -> Path:
    """Write a .kicad_pro and its root schematic, and return the root."""
    write(directory / f"{name}.kicad_pro", "{}")
    return write(directory / f"{name}.kicad_sch", sch(*sheet_names))


class TestSheetFields:
    """The sheet fields, read with the field rules of the file version."""

    def test_a_file_name_that_is_empty_after_the_expansion(self, tmp_path):
        # KiCad shows an empty sheet (loadHierarchy, measured: sheet_fields_check.py),
        # as sheet_list: no file of the parent directory, no problem.
        root = project(tmp_path, "board", "${SUB}")
        (tmp_path / "board.kicad_pro").write_text(
            '{"text_variables": {"SUB": ""}}', encoding="utf-8"
        )
        assert sheet_files(root) == [root]
        _, problems = sheet_paths(tmp_path / "board.kicad_pro")
        assert problems == []

    @pytest.mark.parametrize("key", ["Sheetfile", "Sheet file"])
    def test_kicad_7_and_kicad_6_property_names(self, tmp_path, key):
        root = project(tmp_path, "board")
        root.write_text(sch("a.kicad_sch", key=key), encoding="utf-8")
        a = write(tmp_path / "a.kicad_sch")
        assert sheet_files(root) == [root, a]

    def test_the_file_tree_keeps_the_other_sheets(self, tmp_path):
        # KiCad 10 does not load a root with a sheet with no name (a parse error,
        # spike/kicad-versions/sheet_fields_check.py; build_sheet_list gives an
        # error). The file tree of sch_hierarchy is for the lock check and the
        # project of a file: it keeps the other sheets (more files, never fewer) and
        # gives the problem.
        root = project(tmp_path, "board", "a.kicad_sch")
        text = root.read_text(encoding="utf-8").replace("20250114", "20260306")
        bad = '\t(sheet (at 1 1) (size 2 2) (uuid "u9"))\n'
        root.write_text(text.replace("\t(sheet", bad + "\t(sheet", 1), encoding="utf-8")
        a = write(tmp_path / "a.kicad_sch")
        assert sheet_files(root) == [root, a]
        _, problems = sheet_paths(tmp_path / "board.kicad_pro")
        assert len(problems) == 1 and "Missing sheet name property" in problems[0]

    def test_loop_of_symbolic_links(self, tmp_path):
        root = project(tmp_path, "board", "loop.kicad_sch", "a.kicad_sch")
        try:
            (tmp_path / "loop.kicad_sch").symlink_to(tmp_path / "loop.kicad_sch")
        except OSError:
            pytest.skip("no permission to make a symbolic link")
        a = write(tmp_path / "a.kicad_sch")
        assert sheet_files(root) == [root, a]

    @pytest.mark.parametrize("version", [20211123, 20230121, 20990101])
    def test_sheet_of_an_old_or_newer_version(self, tmp_path, version):
        # KiCad loads such a file: the files below it are in the hierarchy.
        root = project(tmp_path, "board", "a.kicad_sch")
        a = write(tmp_path / "a.kicad_sch", sch("b.kicad_sch", key="Sheet file"))
        a.write_text(
            a.read_text(encoding="utf-8").replace("20250114", str(version)), encoding="utf-8"
        )
        b = write(tmp_path / "b.kicad_sch")
        assert sheet_files(root) == [root, a, b]
        assert find_root_schematic(b) == root

    def test_old_sheet_has_the_files_of_both_rule_sets(self, tmp_path):
        # KiCad 8 and 9 use the field with the ID of the file field (a), KiCad 10
        # ignores the IDs and uses the first file field (b). The installed KiCad
        # is not known.
        root = project(tmp_path, "board")
        root.write_text(
            '(kicad_sch (version 20230121) (generator "eeschema") (uuid "r")\n'
            '\t(sheet (at 1 1) (size 2 2) (uuid "s1")\n'
            '\t\t(property "Sheetname" "A" (id 0)) (property "Sheetfile" "b.kicad_sch" (id 5))\n'
            '\t\t(property "Sheetfile" "a.kicad_sch" (id 1)))\n)\n',
            encoding="utf-8",
        )
        a = write(tmp_path / "a.kicad_sch")
        b = write(tmp_path / "b.kicad_sch")
        assert sorted(sheet_files(root)) == sorted([root, a, b])

    def test_old_sheet_with_two_names_has_one_place(self, tmp_path):
        # The two rule sets give the sheet two names, KiCad gives it one place.
        project(tmp_path, "board")
        (tmp_path / "board.kicad_sch").write_text(
            '(kicad_sch (version 20230121) (generator "eeschema") (uuid "aaaaaaaa-0000-0000-0000-000000000001")\n'
            '\t(sheet (at 1 1) (size 2 2) (uuid "bbbbbbbb-0000-0000-0000-000000000001")\n'
            '\t\t(property "Sheetname" "B" (id 5)) (property "Sheetname" "A" (id 0))\n'
            '\t\t(property "Sheetfile" "a.kicad_sch" (id 1)))\n)\n',
            encoding="utf-8",
        )
        write(tmp_path / "a.kicad_sch")
        paths, _ = sheet_paths(tmp_path / "board.kicad_pro")
        assert [(p.file.name, p.path) for p in paths] == [
            ("board.kicad_sch", "/aaaaaaaa-0000-0000-0000-000000000001"),
            (
                "a.kicad_sch",
                "/aaaaaaaa-0000-0000-0000-000000000001/bbbbbbbb-0000-0000-0000-000000000001",
            ),
        ]

    def test_kicad_10_finds_the_field_with_no_difference_of_case(self, tmp_path):
        root = project(tmp_path, "board")
        text = sch("a.kicad_sch", key="sheetfile").replace("20250114", "20260306")
        root.write_text(text, encoding="utf-8")
        a = write(tmp_path / "a.kicad_sch")
        assert sheet_files(root) == [root, a]

    def test_symbol_field_named_sheetfile_is_not_a_sheet(self, tmp_path):
        root = project(tmp_path, "board")
        root.write_text(
            '(kicad_sch (version 20250114) (generator "eeschema")\n\t(symbol (lib_id "Device:R")\n'
            '\t\t(property "Sheetfile" "fake.kicad_sch" (at 0 0 0))\n\t)\n)\n',
            encoding="utf-8",
        )
        write(tmp_path / "fake.kicad_sch")
        assert sheet_files(root) == [root]

    @pytest.mark.skipif(sys.platform == "win32", reason='Windows does not allow " in a file name')
    def test_escaped_quote_and_parenthesis_in_strings(self, tmp_path):
        root = project(tmp_path, "board")
        root.write_text(sch('dir (1)/a \\"b\\".kicad_sch'), encoding="utf-8")
        a = write(tmp_path / "dir (1)" / 'a "b".kicad_sch')
        assert sheet_files(root) == [root, a]

    @pytest.mark.parametrize("content", ["(kicad_sch))", "(kicad_sch (sheet)"])
    def test_root_that_kicad_cannot_load(self, tmp_path, content):
        root = write(tmp_path / "board.kicad_sch", content)
        assert sheet_files(root) == []

    def test_kicad_fixture(self):
        root = FIXTURES / "hier" / "root.kicad_sch"
        assert [path.name for path in sheet_files(root)] == ["root.kicad_sch", "child.kicad_sch"]


class TestSheetFiles:
    def test_names_are_relative_to_the_parent_sheet(self, tmp_path):
        # KiCad makes a sheet file name absolute with the directory of the
        # parent sheet, not of the root (SCH_IO_KICAD_SEXPR::loadHierarchy)
        root = project(tmp_path, "board", "power/power.kicad_sch")
        power = write(tmp_path / "power" / "power.kicad_sch", sch("ldo.kicad_sch"))
        ldo = write(tmp_path / "power" / "ldo.kicad_sch")
        assert sheet_files(root) == [root, power, ldo]

    def test_each_file_one_time_and_no_endless_loop(self, tmp_path):
        root = project(tmp_path, "board", "a.kicad_sch", "a.kicad_sch")
        a = write(tmp_path / "a.kicad_sch", sch("board.kicad_sch"))
        assert sheet_files(root) == [root, a]

    def test_text_variable_that_is_not_a_string(self, tmp_path):
        root = project(tmp_path, "board", "${REV}.kicad_sch")
        (tmp_path / "board.kicad_pro").write_text(
            json.dumps({"text_variables": {"REV": 2}}), encoding="utf-8"
        )
        assert sheet_files(root) == [root]

    def test_missing_file_is_not_in_the_list(self, tmp_path):
        root = project(tmp_path, "board", "missing.kicad_sch")
        assert sheet_files(root) == [root]

    def test_sheet_that_is_not_valid_utf8_is_in_the_list(self, tmp_path):
        # KiCad loads such a sheet: only the string with the invalid byte is
        # empty. The sheets below it stay in the hierarchy.
        root = project(tmp_path, "board", "a.kicad_sch")
        a = tmp_path / "a.kicad_sch"
        content = sch("b.kicad_sch").replace("(generator ", '(label "\udcff") (generator ')
        a.write_bytes(content.encode("utf-8", errors="surrogateescape"))
        b = write(tmp_path / "b.kicad_sch")
        assert sheet_files(root) == [root, a, b]
        assert find_root_schematic(b) == root

    def test_sheet_file_name_that_is_not_valid_utf8_gives_no_file(self, tmp_path):
        root = tmp_path / "board.kicad_sch"
        write(tmp_path / "board.kicad_pro", "{}")
        root.write_bytes(sch("\udcff.kicad_sch").encode("utf-8", errors="surrogateescape"))
        assert sheet_files(root) == [root]

    def test_project_text_variables_and_no_environment(self, tmp_path, monkeypatch):
        root = project(
            tmp_path,
            "board",
            "${PROJECTNAME}_io.kicad_sch",
            "${BLOCK}.kicad_sch",
            "${HOME}.kicad_sch",
        )
        (tmp_path / "board.kicad_pro").write_text(
            json.dumps({"text_variables": {"BLOCK": "usb"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", "env")
        io = write(tmp_path / "board_io.kicad_sch")
        usb = write(tmp_path / "usb.kicad_sch")
        write(tmp_path / "env.kicad_sch")
        assert sheet_files(root) == [root, io, usb]


class TestTopLevelSheets:
    def test_project_of_kicad_9(self, tmp_path):
        (tmp_path / "board.kicad_pro").write_text("{}", encoding="utf-8")
        assert top_level_sheets(tmp_path / "board.kicad_pro") == [
            TopLevelSheet(tmp_path / "board.kicad_sch", None, "board")
        ]

    def test_project_of_kicad_10(self, tmp_path):
        write(tmp_path / "board.kicad_sch")
        write(tmp_path / "second.kicad_sch")
        entries = [
            {"filename": "board.kicad_sch", "name": "Root", "uuid": NIL_UUID},
            {"filename": "second.kicad_sch", "name": "Second", "uuid": "c1"},
            # A project from a template: KiCad uses the schematic of the project name.
            {"filename": "template.kicad_sch", "name": "T", "uuid": "c2"},
        ]
        pro = tmp_path / "board.kicad_pro"
        settings = {"meta": {"version": 3}, "schematic": {"top_level_sheets": entries}}
        pro.write_text(json.dumps(settings), encoding="utf-8")
        assert top_level_sheets(pro) == [
            TopLevelSheet(tmp_path / "board.kicad_sch", NIL_UUID, "Root"),
            TopLevelSheet(tmp_path / "second.kicad_sch", "c1", "Second"),
            TopLevelSheet(tmp_path / "board.kicad_sch", "c2", "board"),
        ]
        # No number meta.version: JSON_SETTINGS::LoadFromFile fails, PROJECT_FILE does
        # not change the entries (measured with kicad-cli 10.0.7).
        pro.write_text(json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8")
        assert top_level_sheets(pro)[2] == TopLevelSheet(tmp_path / "template.kicad_sch", "c2", "T")

    @pytest.mark.parametrize(
        "entry, sheet",
        [
            # The directory of filename goes (wxFileName with a path and a name).
            (
                {"filename": "sub/second.kicad_sch", "name": "S", "uuid": "c1"},
                ("second", "c1", "S"),
            ),
            # No uuid: a random UUID (KIID()); no name: an empty name.
            ({"filename": "second.kicad_sch"}, ("second", "", "")),
            # Not an object: an entry with no fields, thus the schematic of the project.
            ("x", ("board", "", "board")),
        ],
    )
    def test_entries_as_kicad_reads_them(self, tmp_path, entry, sheet):
        write(tmp_path / "board.kicad_sch")
        write(tmp_path / "second.kicad_sch")
        write(tmp_path / "sub" / "second.kicad_sch")
        entries = [{"filename": "board.kicad_sch", "name": "Root", "uuid": "r"}, entry]
        pro = tmp_path / "board.kicad_pro"
        settings = {"meta": {"version": 3}, "schematic": {"top_level_sheets": entries}}
        pro.write_text(json.dumps(settings), encoding="utf-8")
        stem, uuid, name = sheet
        assert top_level_sheets(pro)[1] == TopLevelSheet(tmp_path / f"{stem}.kicad_sch", uuid, name)

    @pytest.mark.parametrize("bad", [{"uuid": 5}, {"name": None}, {"filename": []}])
    def test_a_type_error_ignores_the_list(self, tmp_path, bad):
        # PARAM_LIST::Load throws: KiCad has no top-level sheets of the list, thus the
        # schematic of the project name (measured with kicad-cli 10.0.7).
        write(tmp_path / "board.kicad_sch")
        entries = [{"filename": "second.kicad_sch", "name": "S", "uuid": "c1"}, bad]
        pro = tmp_path / "board.kicad_pro"
        settings = {"meta": {"version": 3}, "schematic": {"top_level_sheets": entries}}
        pro.write_text(json.dumps(settings), encoding="utf-8")
        assert top_level_sheets(pro) == [TopLevelSheet(tmp_path / "board.kicad_sch", None, "board")]

    def test_project_of_an_other_top_level_sheet(self, tmp_path):
        # sheet_files gives the tree of one root. The project of a file below
        # an other top-level sheet comes from the top-level sheets.
        root = project(tmp_path, "board")
        second = write(tmp_path / "second.kicad_sch", sch("sub.kicad_sch"))
        sub = write(tmp_path / "sub.kicad_sch")
        entries = [
            {"filename": "board.kicad_sch", "name": "Root", "uuid": "r"},
            {"filename": "second.kicad_sch", "name": "Second", "uuid": "s"},
        ]
        (tmp_path / "board.kicad_pro").write_text(
            json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8"
        )
        assert sheet_files(root) == [root]
        assert find_root_schematic(sub) is None
        for path in (second, sub):
            assert schematic_project_file(path) == tmp_path / "board.kicad_pro"

    @pytest.fixture
    def nested(self, tmp_path):
        """A KiCad 10 project whose second top-level sheet holds the sub-sheet of an other
        project. (A top-level sheet in a directory is not possible: KiCad uses only the
        last component of its filename.)"""
        project(tmp_path, "top")
        inner = tmp_path / "inner"
        root = project(inner, "board", "sub.kicad_sch")
        sub = write(inner / "sub.kicad_sch")
        write(tmp_path / "second.kicad_sch", sch("inner/sub.kicad_sch"))
        entries = [
            {"filename": "top.kicad_sch", "name": "Top", "uuid": "t"},
            {"filename": "second.kicad_sch", "name": "Second", "uuid": "b"},
        ]
        (tmp_path / "top.kicad_pro").write_text(
            json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8"
        )
        return root, sub

    def test_one_top_level_sheet_with_an_other_name(self, tmp_path):
        main = write(tmp_path / "main.kicad_sch", sch("sub.kicad_sch"))
        sub = write(tmp_path / "sub.kicad_sch")
        entries = [{"filename": "main.kicad_sch", "name": "Main", "uuid": "m"}]
        (tmp_path / "foo.kicad_pro").write_text(
            json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8"
        )
        for path in (main, sub):
            assert schematic_project_file(path) == tmp_path / "foo.kicad_pro"

    def test_root_of_its_own_project(self, nested, tmp_path):
        root, _ = nested
        assert schematic_project_file(root) == tmp_path / "inner" / "board.kicad_pro"

    def test_sheet_in_two_projects(self, nested, tmp_path):
        _, sub = nested
        with pytest.raises(AmbiguousRootError) as error:
            schematic_project_file(sub)
        assert error.value.roots == [
            tmp_path / "inner" / "board.kicad_sch",
            tmp_path / "top.kicad_sch",
        ]


class TestSheetPaths:
    def test_paths_and_names(self, tmp_path):
        root = project(tmp_path, "board", "a.kicad_sch", "a.kicad_sch")
        write(tmp_path / "a.kicad_sch")
        top = parse(root.read_text(encoding="utf-8")).first("uuid").value(0)
        paths, problems = sheet_paths(tmp_path / "board.kicad_pro")
        assert problems == []
        assert [(p.file.name, p.path, p.shown) for p in paths] == [
            ("board.kicad_sch", f"/{top}", "/"),
            ("a.kicad_sch", f"/{top}/00000000-0000-4000-8000-000000000000", "/S0/"),
            ("a.kicad_sch", f"/{top}/00000000-0000-4000-8000-000000000001", "/S1/"),
        ]

    def test_sheet_that_holds_its_parent_is_not_loaded(self, tmp_path):
        project(tmp_path, "board", "a.kicad_sch")
        write(tmp_path / "a.kicad_sch", sch("board.kicad_sch"))
        paths, problems = sheet_paths(tmp_path / "board.kicad_pro")
        # KiCad has the path of the sheet, with an empty screen.
        assert [p.file and p.file.name for p in paths] == ["board.kicad_sch", "a.kicad_sch", None]
        assert [p.shown for p in paths] == ["/", "/S0/", "/S0/S0/"]
        assert len(problems) == 1 and "holds its own ancestor" in problems[0]

    def test_missing_sheet(self, tmp_path):
        project(tmp_path, "board", "gone.kicad_sch")
        paths, problems = sheet_paths(tmp_path / "board.kicad_pro")
        # KiCad has the path of the sheet, with an empty screen (and the netlist and
        # the SVG export have it).
        assert ([p.file and p.file.name for p in paths], problems) == (
            ["board.kicad_sch", None],
            [],
        )
        assert sheet_files(tmp_path / "board.kicad_sch") == [tmp_path / "board.kicad_sch"]

    def test_sheet_with_no_uuid(self, tmp_path):
        project(tmp_path, "board", "a.kicad_sch")
        text = (tmp_path / "board.kicad_sch").read_text(encoding="utf-8")
        (tmp_path / "board.kicad_sch").write_text(
            text.replace('(uuid "00000000-0000-4000-8000-000000000000")', ""), encoding="utf-8"
        )
        paths, problems = sheet_paths(tmp_path / "board.kicad_pro")
        # KiCad gives the sheet a new UUID at each load: no fixed path below it.
        # a.kicad_sch is not there: an empty sheet.
        assert [(p.file and p.file.name, p.uuids is None) for p in paths] == [
            ("board.kicad_sch", False),
            (None, True),
        ]
        assert problems == [
            f"The sheet 'S0' of {tmp_path / 'board.kicad_sch'} has no valid UUID: KiCad gives "
            "it a new UUID at each load, thus no instance in the files matches its sheet paths."
        ]

    def test_sheet_that_cannot_be_read(self, tmp_path):
        project(tmp_path, "board", "bad.kicad_sch", "a.kicad_sch")
        write(tmp_path / "bad.kicad_sch", "(kicad_sch (version 20250114)")
        write(tmp_path / "a.kicad_sch")
        paths, problems = sheet_paths(tmp_path / "board.kicad_pro")
        # KiCad keeps the items of bad.kicad_sch before the error, and loads the other
        # sheets: the tolerant load keeps the file, with no sheets.
        assert [p.file.name for p in paths] == ["board.kicad_sch", "bad.kicad_sch", "a.kicad_sch"]
        assert len(problems) == 1 and "bad.kicad_sch: the parser of KiCad stops" in problems[0]

    def test_top_level_sheets_with_no_schematic_of_the_project_name(self, tmp_path):
        a = write(tmp_path / "a.kicad_sch")
        write(tmp_path / "b.kicad_sch")
        entries = [
            {"filename": "a.kicad_sch", "name": "A", "uuid": "u1"},
            {"filename": "b.kicad_sch", "name": "B", "uuid": "u2"},
        ]
        (tmp_path / "board.kicad_pro").write_text(
            json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8"
        )
        assert schematic_project_file(a) == tmp_path / "board.kicad_pro"


class TestFindRootSchematic:
    def test_root_schematic_gives_none(self, tmp_path):
        root = project(tmp_path, "board", "sub.kicad_sch")
        assert is_project_root(root)
        assert find_root_schematic(root) is None

    def test_sub_sheet_gives_root(self, tmp_path):
        root = project(tmp_path, "board", "sub.kicad_sch")
        sub = write(tmp_path / "sub.kicad_sch")
        assert find_root_schematic(sub) == root

    def test_sub_sheet_in_a_sub_directory(self, tmp_path):
        root = project(tmp_path, "board", "sheets/sub.kicad_sch")
        sub = write(tmp_path / "sheets" / "sub.kicad_sch")
        assert find_root_schematic(sub) == root

    def test_file_that_no_root_holds_gives_none(self, tmp_path):
        project(tmp_path, "board")
        loose = write(tmp_path / "loose.kicad_sch")
        assert find_root_schematic(loose) is None

    def test_no_project(self, tmp_path):
        assert find_root_schematic(write(tmp_path / "sheet.kicad_sch")) is None

    def test_root_schematic_missing(self, tmp_path):
        write(tmp_path / "board.kicad_pro", "{}")
        assert find_root_schematic(write(tmp_path / "sub.kicad_sch")) is None

    def test_two_projects_in_one_directory(self, tmp_path):
        # Only the root that holds the sheet counts, not the first .kicad_pro
        project(tmp_path, "board")
        root = project(tmp_path, "board_variant", "sub.kicad_sch")
        assert find_root_schematic(write(tmp_path / "sub.kicad_sch")) == root

    def test_two_roots_hold_the_sheet(self, tmp_path):
        a = project(tmp_path, "a", "sub.kicad_sch")
        b = project(tmp_path, "b", "sub.kicad_sch")
        sub = write(tmp_path / "sub.kicad_sch")
        with pytest.raises(AmbiguousRootError) as error:
            find_root_schematic(sub)
        assert error.value.roots == [a, b]

    def test_nested_project_used_as_sub_sheet(self, tmp_path):
        main = project(tmp_path, "main", "usb/usb.kicad_sch")
        usb = project(tmp_path / "usb", "usb")
        # usb.kicad_sch is the root of its own project ...
        assert find_root_schematic(usb) is None
        # ... and a sub-sheet of main
        assert roots_containing(usb) == [usb, main]

    def test_project_through_a_symbolic_link_is_one_root(self, tmp_path):
        root = project(tmp_path / "real" / "proj", "proj", "sub/p.kicad_sch")
        write(tmp_path / "real" / "proj" / "sub" / "p.kicad_sch")
        link = tmp_path / "link"
        try:
            os.symlink(tmp_path / "real", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no permission to make a symbolic link")
        found = find_root_schematic(link / "proj" / "sub" / "p.kicad_sch")
        assert found is not None
        assert found.resolve() == root.resolve()

    def test_sub_sheet_opened_alone_is_not_a_root(self, tmp_path):
        # The schematic editor opened the sub-sheet alone: KiCad wrote the
        # lock file of a project with the name of the sub-sheet
        root = project(tmp_path, "board", "sub.kicad_sch")
        sub = write(tmp_path / "sub.kicad_sch")
        write(tmp_path / "~sub.kicad_pro.lck", "{}")
        assert not is_project_root(sub)
        assert find_root_schematic(sub) == root

    def test_file_that_kicad_cannot_load_is_not_a_root(self, tmp_path):
        write(tmp_path / "board.kicad_pro", "{}")
        write(
            tmp_path / "board.kicad_sch",
            '(kicad_sch (sheet (property "Sheetfile" "sub.kicad_sch"))))',
        )
        sub = write(tmp_path / "sub.kicad_sch")
        assert find_root_schematic(sub) is None

    def test_project_lock_with_no_project_file(self, tmp_path):
        # The schematic editor opened root.kicad_sch alone: KiCad writes the
        # lock file of the project, but no .kicad_pro file
        root = write(tmp_path / "root.kicad_sch", sch("child.kicad_sch"))
        child = write(tmp_path / "child.kicad_sch")
        assert find_root_schematic(child) is None
        write(tmp_path / "~root.kicad_pro.lck", "{}")
        assert find_root_schematic(child) == root


class TestToolsWithTwoRoots:
    """The tools tell the agent when more than one root holds a sub-sheet."""

    @pytest.fixture
    def sub(self, tmp_path):
        project(tmp_path, "a", "sub.kicad_sch")
        project(tmp_path, "b", "sub.kicad_sch")
        return write(tmp_path / "sub.kicad_sch")

    def test_generate_netlist(self, sub):
        from kicad_workbench.tools.netlist import generate_netlist

        result = asyncio.run(generate_netlist(str(sub)))
        assert result.startswith("❌") and "more than one root schematic" in result

    def test_run_erc(self, sub):
        from kicad_workbench.tools.validation import run_erc

        result = asyncio.run(run_erc(str(sub)))
        assert result.startswith("❌") and "more than one root schematic" in result

    def test_pin_conflicts(self, sub):
        from kicad_workbench.tools.pin_analysis import detect_pin_conflicts

        result = asyncio.run(detect_pin_conflicts(str(sub)))
        assert "more than one root schematic" in result
