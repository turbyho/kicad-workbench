"""Tests of the sheet file names as the loader of KiCad makes them (sheet_file_names),
and of the load of a hierarchy with them (sheet_list, sch_hierarchy).

Measured with kicad-cli 8.0.9, 9.0.9, 10.0.6 and 10.0.7 on Linux
(spike/kicad-versions/sheet_file_names_check.py): KiCad compares the texts of the
file names. A symbolic link and "/./" in an absolute name give a second screen,
"x/.." stays in an absolute name, "//" goes, "\\" is "/", and "~" is the home
directory. The rules for Windows come from the source of wxWidgets 3.2.
"""

import os
import sys
from pathlib import Path

import pytest

from kicad_workbench.errors import KicadFileError
from kicad_workbench.parsers.kicad_fields import FieldRules, sheet_fields
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.parsers.sheet_file_names import (
    SheetFileError,
    parse_file_name,
    sheet_file_name,
    user_home,
)
from kicad_workbench.parsers.sheet_list import build_sheet_list
from kicad_workbench.utils.kicad_project import TopLevelSheet, text_variables
from kicad_workbench.utils.sch_hierarchy import root_sheet_paths, sheet_files, sheet_paths

UNIX_PARENT = parse_file_name("/w/p/t.kicad_sch", False)
WINDOWS_PARENT = parse_file_name("C:\\w\\p\\t.kicad_sch", True)
needs_unix = pytest.mark.skipif(sys.platform == "win32", reason="the rules of Unix")


class TestUnixNames:
    @pytest.mark.parametrize(
        "name, text",
        [
            ("sub.kicad_sch", "/w/p/sub.kicad_sch"),
            ("d/../sub.kicad_sch", "/w/p/sub.kicad_sch"),  # MakeAbsolute removes "d/.."
            ("./a/./b/../c.kicad_sch", "/w/p/a/c.kicad_sch"),
            ("../../../../x.kicad_sch", "/x.kicad_sch"),  # an extra ".." of a rooted name
            ("a//b.kicad_sch", "/w/p/a/b.kicad_sch"),
            ("a~s/sub.kicad_sch", "/w/p/a~s/sub.kicad_sch"),  # not a first "~"
            ("~sub.kicad_sch", "/w/p/~sub.kicad_sch"),  # a file name, no directory
            ("sub/..", "/w/p/sub/.."),  # the last component stays as it is
            # An absolute name stays as it is, but "//" goes.
            ("/w/./sub.kicad_sch", "/w/./sub.kicad_sch"),
            ("/w/x/../t.kicad_sch", "/w/x/../t.kicad_sch"),
            ("/w//sub.kicad_sch", "/w/sub.kicad_sch"),
            ("sub\\x.kicad_sch", "/w/p/sub\\x.kicad_sch"),  # "\\" is no separator of Unix
        ],
    )
    def test_names(self, name, text):
        assert sheet_file_name(name, UNIX_PARENT).text == text

    def test_the_directory_of_an_absolute_parent_with_dots(self):
        # The children of "/w/x/../t.kicad_sch" are relative to "/w" (MakeAbsolute
        # removes "x/.." of the directory of the parent too).
        parent = parse_file_name("/w/x/../t.kicad_sch", False)
        assert sheet_file_name("sub.kicad_sch", parent).text == "/w/sub.kicad_sch"

    @needs_unix
    def test_the_home_directory(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/someone/")
        assert sheet_file_name("~/s/../sub.kicad_sch", UNIX_PARENT).text == (
            "/home/someone/sub.kicad_sch"
        )

    @needs_unix
    def test_the_home_directory_of_a_user(self):
        import pwd

        entry = pwd.getpwuid(os.getuid())
        name = sheet_file_name(f"~{entry.pw_name}/sub.kicad_sch", UNIX_PARENT)
        assert name.text == os.path.join(entry.pw_dir.rstrip("/") or "/", "sub.kicad_sch")

    @needs_unix
    def test_a_user_that_is_not_known(self):
        with pytest.raises(SheetFileError, match="relative to its working directory"):
            sheet_file_name("~nobody_kw_test/sub.kicad_sch", UNIX_PARENT)

    @needs_unix
    def test_the_home_of_the_process_with_no_home_variable(self, monkeypatch):
        import pwd

        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)
        assert user_home("") == pwd.getpwuid(os.getuid()).pw_dir


class TestWindowsNames:
    @pytest.mark.parametrize(
        "name, text",
        [
            ("sub.kicad_sch", "C:\\w\\p\\sub.kicad_sch"),
            ("a/b\\..\\c.kicad_sch", "C:\\w\\p\\a\\c.kicad_sch"),
            # A rooted name with no volume gets the volume of the parent.
            ("\\x\\.\\y.kicad_sch", "C:\\x\\y.kicad_sch"),
            # An absolute name (a volume and a root) stays as it is.
            ("D:\\a\\.\\b.kicad_sch", "D:\\a\\.\\b.kicad_sch"),
            ("D:/a//b.kicad_sch", "D:\\a\\b.kicad_sch"),
            # A volume with no root: the directories of the parent come in front.
            ("D:x.kicad_sch", "D:\\w\\p\\x.kicad_sch"),
            ("\\\\server\\share\\a.kicad_sch", "\\\\server\\share\\a.kicad_sch"),
            ("~/s.kicad_sch", "C:\\w\\p\\~\\s.kicad_sch"),  # no "~" rule on Windows
        ],
    )
    def test_names(self, name, text):
        assert sheet_file_name(name, WINDOWS_PARENT).text == text


@pytest.mark.parametrize("rules", [FieldRules(9, 20250114), FieldRules(10, 20260306)])
def test_the_file_field_is_in_the_notation_of_unix(rules):
    # SCH_SHEET::SetFields changes each "\\" to "/" (KiCad 8, 9 and 10).
    sheet = parse(
        '(sheet (property "Sheetname" "S" (at 0 0 0)) '
        '(property "Sheetfile" "sub\\\\leaf.kicad_sch" (at 0 0 0)))'
    )
    assert sheet_fields(sheet, rules)["Sheetfile"] == "sub/leaf.kicad_sch"


FONT = "(effects (font (size 1.27 1.27)))"


def sheet(index: int, name: str, file: str, x: int = 10) -> str:
    return (
        f'(sheet (at {x} 10) (size 20 20) (uuid "bbbbbbbb-0000-0000-0000-00000000000{index}") '
        f'(property "Sheetname" "{name}" (at {x} 9 0) {FONT}) '
        f'(property "Sheetfile" "{file}" (at {x} 31 0) {FONT}))\n'
    )


def schematic(path: Path, uuid: str, body: str = "", version: int = 20260306) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'(kicad_sch (version {version}) (generator "eeschema") (uuid "{uuid}") '
        f'(paper "A4")\n{body}(sheet_instances (path "/" (page "1"))))\n',
        encoding="utf-8",
    )
    return path


ROOT = "aaaaaaaa-0000-0000-0000-000000000000"
SUB = "cccccccc-0000-0000-0000-000000000000"


def load(root: Path, tolerant: bool = False):
    tops = [TopLevelSheet(root, None, root.stem)]
    return build_sheet_list(tops, root.parent, text_variables(root), 10, tolerant=tolerant)


def shown(sheet_list) -> list[tuple[str, str | None]]:
    return [(p.shown, p.screen.file.name if p.screen else None) for p in sheet_list.paths]


class TestLoad:
    def test_an_absolute_name_with_a_dot_is_a_second_screen(self, tmp_path):
        base = tmp_path.as_posix()
        root = schematic(
            tmp_path / "t.kicad_sch",
            ROOT,
            sheet(1, "A", f"{base}/sub.kicad_sch") + sheet(2, "B", f"{base}/./sub.kicad_sch", 40),
        )
        schematic(tmp_path / "sub.kicad_sch", SUB)
        sheets = load(root)
        assert shown(sheets) == [
            ("/", "t.kicad_sch"),
            ("/A/", "sub.kicad_sch"),
            ("/B/", "sub.kicad_sch"),
        ]
        assert len(sheets.screens) == 3  # pathlib would make the two names one Path
        assert sheet_files(root) == [root, tmp_path / "sub.kicad_sch"]  # one file of the disk

    def test_an_absolute_name_with_dot_dot_is_no_ancestor(self, tmp_path):
        base = tmp_path.as_posix()
        (tmp_path / "x").mkdir()
        root = schematic(tmp_path / "t.kicad_sch", ROOT, sheet(1, "A", "sub.kicad_sch"))
        schematic(tmp_path / "sub.kicad_sch", SUB, sheet(3, "Up", f"{base}/x/../t.kicad_sch"))
        assert [p.shown for p in load(root).paths] == ["/", "/A/", "/A/Up/", "/A/Up/A/"]

    @needs_unix
    def test_a_symbolic_link_to_the_root_loads_it_again(self, tmp_path):
        root = schematic(tmp_path / "t.kicad_sch", ROOT, sheet(1, "L", "link.kicad_sch"))
        os.symlink("t.kicad_sch", tmp_path / "link.kicad_sch")
        sheets = load(root)
        # The sheet "L" of the second screen is an ancestor: an empty sheet.
        assert shown(sheets) == [("/", "t.kicad_sch"), ("/L/", "link.kicad_sch")]
        assert [p.file and p.file.name for p in root_sheet_paths(root)[0]] == [
            "t.kicad_sch",
            "link.kicad_sch",
        ]

    def test_the_file_field_with_a_backslash(self, tmp_path):
        root = schematic(tmp_path / "t.kicad_sch", ROOT, sheet(1, "S", "sub\\\\leaf.kicad_sch"))
        schematic(tmp_path / "sub" / "leaf.kicad_sch", SUB)
        assert shown(load(root)) == [("/", "t.kicad_sch"), ("/S/", "leaf.kicad_sch")]

    @needs_unix
    def test_a_user_that_is_not_known(self, tmp_path):
        root = schematic(
            tmp_path / "t.kicad_sch", ROOT, sheet(1, "T", "~nobody_kw_test/s.kicad_sch")
        )
        with pytest.raises(SheetFileError, match=f"{root}: The sheet file name"):
            load(root)
        sheets = load(root, tolerant=True)
        assert [(p.shown, p.screen is not None) for p in sheets.paths] == [
            ("/", True),
            ("/T/", False),
        ]
        assert sheets.screens[str(root)].sheets[0].empty == "working directory"
        assert any("working directory" in problem for problem in sheets.problems)


class TestTolerantLoad:
    def test_a_parse_error_of_a_sub_sheet_keeps_the_file(self, tmp_path):
        root = schematic(tmp_path / "t.kicad_sch", ROOT, sheet(1, "A", "bad.kicad_sch"))
        (tmp_path / "bad.kicad_sch").write_text("(kicad_sch (version 20260306)", encoding="utf-8")
        with pytest.raises(KicadFileError, match="The parser of KiCad stops in a sub-sheet"):
            load(root)
        sheets = load(root, tolerant=True)
        assert shown(sheets) == [("/", "t.kicad_sch"), ("/A/", "bad.kicad_sch")]
        assert sheets.screens[str(tmp_path / "bad.kicad_sch")].partial
        assert sheet_files(root) == [root, tmp_path / "bad.kicad_sch"]

    def test_a_parse_error_of_the_root_gives_no_file(self, tmp_path):
        root = tmp_path / "t.kicad_sch"
        root.write_text("(kicad_sch (version 20260306)", encoding="utf-8")
        sheets = load(root, tolerant=True)
        assert (sheets.paths, sheet_files(root)) == ([], [])
        assert len(sheets.problems) == 1

    def test_a_file_of_an_unknown_version_gives_the_sheets_of_two_rule_sets(self, tmp_path):
        # KiCad 8 and 9 use the first field of each ID, KiCad 10 the first field of
        # each name: the two give different files.
        (tmp_path / "t.kicad_pro").write_text("{}", encoding="utf-8")
        root = schematic(
            tmp_path / "t.kicad_sch",
            ROOT,
            '(sheet (at 1 1) (size 2 2) (uuid "bbbbbbbb-0000-0000-0000-000000000001") '
            '(property "Sheetname" "A" (id 0)) (property "Sheetfile" "b.kicad_sch" (id 5)) '
            '(property "Sheetfile" "a.kicad_sch" (id 1)))\n',
            version=20230121,
        )
        a = schematic(tmp_path / "a.kicad_sch", SUB)
        b = schematic(tmp_path / "b.kicad_sch", SUB)
        with pytest.raises(KicadFileError):
            load(root)
        assert sorted(sheet_files(root)) == sorted([root, a, b])
        paths, problems = sheet_paths(tmp_path / "t.kicad_pro")
        assert len(paths) == 3 and len(problems) == 1


class TestReviewFindings:
    def test_a_parse_error_names_each_file_of_the_same_bytes(self, tmp_path):
        # The cache keeps no parse error: its message names the file.
        root = schematic(
            tmp_path / "t.kicad_sch",
            ROOT,
            sheet(1, "A", "a.kicad_sch") + sheet(2, "B", "b.kicad_sch", 40),
        )
        for name in ("a.kicad_sch", "b.kicad_sch"):
            (tmp_path / name).write_bytes(b"(kicad_sch (version 20260306) \xff)\n")
        problems = load(root, tolerant=True).problems
        assert len(problems) == 2
        a_problem, b_problem = problems
        assert a_problem.count(str(tmp_path / "a.kicad_sch")) == 1, a_problem
        assert b_problem.count(str(tmp_path / "b.kicad_sch")) == 1, b_problem
        assert "a.kicad_sch" not in b_problem

    @needs_unix
    def test_a_home_directory_gives_a_note(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        root = schematic(tmp_path / "t.kicad_sch", ROOT, sheet(1, "T", "~/sub.kicad_sch"))
        schematic(tmp_path / "home" / "sub.kicad_sch", SUB)
        sheets = load(root)
        assert shown(sheets) == [("/", "t.kicad_sch"), ("/T/", "sub.kicad_sch")]
        assert len(sheets.problems) == 1
        assert "KiCad replaces its first directory with the home directory" in sheets.problems[0]

    def test_the_kiids_of_the_kicad_that_loads_the_files(self, tmp_path):
        # A KiCad 9 file with a table of the UUID of the sheet: KiCad 10 reads the
        # UUID of the table too (parseKIID), thus the sheet gets the next KIID.
        same = "bbbbbbbb-0000-0000-0000-000000000001"
        root = schematic(
            tmp_path / "t.kicad_sch",
            ROOT,
            f'(table (uuid "{same}"))\n' + sheet(1, "A", "sub.kicad_sch"),
            version=20250114,
        )
        schematic(tmp_path / "sub.kicad_sch", SUB, version=20250114)
        kicad9 = [p.uuids for p in root_sheet_paths(root, 9)[0]]
        kicad10 = [p.uuids for p in root_sheet_paths(root, 10)[0]]
        assert kicad9[1] == (ROOT, same)
        assert kicad10[1] == (ROOT, "bbbbbbbb-0000-0000-0000-000000000002")

    def test_a_root_of_an_unknown_version_with_no_uuid(self, tmp_path):
        root = tmp_path / "t.kicad_sch"
        root.write_text('(kicad_sch (version 20990101) (generator "eeschema"))\n', "utf-8")
        paths, problems = root_sheet_paths(root)
        assert [p.file for p in paths] == [root]
        paths, problems = sheet_paths(tmp_path / "t.kicad_pro")
        assert paths[0].uuids is None
        assert any("has no UUID" in problem for problem in problems), problems


def test_the_info_of_two_screens_with_different_sheets(tmp_path, monkeypatch):
    # sub.kicad_sch has two names. Under the first, its sheet "Up" loads
    # "<dir>/x/../t.kicad_sch" (no ancestor of t.kicad_sch: a text of its own); under
    # the second, "Up" is an ancestor. Each screen gets its list.
    import asyncio

    from kicad_workbench.tools import schematic as schematic_tool

    async def version():
        return "10.0.7"

    monkeypatch.setattr(schematic_tool, "get_kicad_version", version)
    base = tmp_path.as_posix()
    (tmp_path / "x").mkdir()
    (tmp_path / "t.kicad_pro").write_text("{}", encoding="utf-8")
    schematic(
        tmp_path / "t.kicad_sch",
        ROOT,
        sheet(1, "A", f"{base}/sub.kicad_sch") + sheet(2, "B", f"{base}/./sub.kicad_sch", 40),
    )
    sub = schematic(tmp_path / "sub.kicad_sch", SUB, sheet(3, "Up", f"{base}/x/../t.kicad_sch"))
    result = asyncio.run(schematic_tool.get_schematic_info(str(sub)))
    assert "⚠️ KiCad loads this file under 2 names" in result, result
    assert f"## Sheet symbols of the screen {base}/sub.kicad_sch\n- Up:" in result
    assert f"## Sheet symbols of the screen {base}/./sub.kicad_sch\n- Up:" in result
    assert result.count("(the file holds its own ancestor: an empty sheet in KiCad)") == 1
    # The message names the screen with the file name of KiCad ("/./" stays).
    assert f"The sheet 'Up' of {base}/./sub.kicad_sch holds its own ancestor" in result


def test_a_parse_error_names_the_screen_with_the_file_name_of_kicad(tmp_path):
    # The messages give the file name of KiCad: Path removes "/./".
    base = tmp_path.as_posix()
    root = schematic(tmp_path / "t.kicad_sch", ROOT, sheet(1, "B", f"{base}/./sub.kicad_sch"))
    (tmp_path / "sub.kicad_sch").write_bytes(b"(kicad_sch (version 20260306) \xff)\n")
    name = f"{base}/./sub.kicad_sch"
    with pytest.raises(KicadFileError) as error:
        load(root)
    assert str(error.value).count(name) == 1, error.value
    assert f"{base}/sub.kicad_sch" not in str(error.value)
    problems = load(root, tolerant=True).problems
    assert len(problems) == 1 and problems[0].count(name) == 1, problems
    assert f"{base}/sub.kicad_sch" not in problems[0]


class TestTopLevelSheetFiles:
    """The top-level sheets of KiCad 10 (measured with kicad-cli 10.0.7:
    spike/kicad-versions/top_level_sheets_check.py)."""

    def project(self, tmp_path, entries):
        import json

        schematic(tmp_path / "t.kicad_sch", ROOT)
        schematic(tmp_path / "s.kicad_sch", SUB)
        (tmp_path / "t.kicad_pro").write_text(
            json.dumps(
                {
                    "meta": {"version": 3},
                    "schematic": {"top_level_sheets": entries},
                    "text_variables": {"N": "s"},
                }
            ),
            encoding="utf-8",
        )
        return tmp_path / "t.kicad_pro"

    def test_an_entry_with_no_uuid_has_a_random_uuid(self, tmp_path):
        pro = self.project(
            tmp_path,
            [{"filename": "t.kicad_sch", "name": "T", "uuid": ROOT}, {"filename": "s.kicad_sch"}],
        )
        paths, problems = sheet_paths(pro)
        assert [(p.shown, p.uuids is None) for p in paths] == [("/T/", False), ("//", True)]
        assert problems == [
            "The entry '' (s.kicad_sch) of top_level_sheets has no valid UUID: KiCad gives it a "
            "new UUID at each load, thus no instance in the files matches its sheet paths."
        ]

    def test_the_nil_uuid_is_the_uuid_of_the_file(self, tmp_path):
        nil = "00000000-0000-0000-0000-000000000000"
        pro = self.project(
            tmp_path,
            [
                {"filename": "t.kicad_sch", "name": "T", "uuid": ROOT},
                {"filename": "s.kicad_sch", "name": "S", "uuid": nil},
            ],
        )
        paths, problems = sheet_paths(pro)
        assert [p.uuids for p in paths] == [(ROOT,), (SUB,)] and problems == []

    def test_a_file_name_with_a_text_variable(self, tmp_path):
        # KiCad checks the file "${N}.kicad_sch", and loadHierarchy loads s.kicad_sch.
        pro = self.project(
            tmp_path,
            [
                {"filename": "t.kicad_sch", "name": "T", "uuid": ROOT},
                {"filename": "${N}.kicad_sch", "name": "S", "uuid": SUB},
            ],
        )
        schematic(tmp_path / "${N}.kicad_sch", "dddddddd-0000-0000-0000-000000000000")
        paths, _problems = sheet_paths(pro)
        assert [p.file.name for p in paths] == ["t.kicad_sch", "s.kicad_sch"]

    def test_one_loaded_top_level_sheet_has_no_name_in_the_paths(self, tmp_path):
        # PathHumanReadable: the name of the top-level sheet only when KiCad loaded more
        # than one (GetTopLevelSheets). No meta.version: gone.kicad_sch stays.
        import json

        pro = self.project(
            tmp_path,
            [
                {"filename": "t.kicad_sch", "name": "T", "uuid": ROOT},
                {"filename": "gone.kicad_sch", "name": "G", "uuid": SUB},
            ],
        )
        settings = json.loads(pro.read_text(encoding="utf-8"))
        del settings["meta"]
        pro.write_text(json.dumps(settings), encoding="utf-8")
        paths, _problems = sheet_paths(pro)
        assert [p.shown for p in paths] == ["/"]

    def test_a_type_error_and_comments_as_kicad_reads_them(self, tmp_path):
        # nlohmann::json reads a byte order mark and comments; a boolean meta.version is
        # a number for Get<int> (measured with kicad-cli 10.0.7).
        from kicad_workbench.utils.kicad_project import top_level_sheets

        pro = self.project(tmp_path, [])
        text = (
            '\ufeff// a comment\n{"meta": {"version": true}, /* c */ "schematic": '
            '{"top_level_sheets": [{"filename": "gone.kicad_sch", "name": "G", "uuid": "'
            + SUB
            + '"}, {"filename": "s.kicad_sch", "name": "S", "uuid": "'
            + SUB
            + '"}]}}'
        )
        pro.write_text(text, encoding="utf-8")
        files = [top.file.name for top in top_level_sheets(pro)]
        assert files == ["t.kicad_sch", "s.kicad_sch"]  # gone.kicad_sch: the template change
