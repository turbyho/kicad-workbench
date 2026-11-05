"""Tests for the check of the annotation (parsers/annotation.py, parsers/sheet_list.py).

spike/kicad-versions/annotation_check.py compares the check with kicad-cli of
KiCad 8, 9 and 10. These tests give the details that kicad-cli does not show:
the messages and the rules of each KiCad version.
"""

import json
from pathlib import Path

import pytest

from kicad_workbench.parsers.annotation import (
    _merged,
    _orders,
    _Ref,
    check_annotation,
    cmp_no_case,
    erc_violations,
    marker_text,
    ref_des_prefix,
)
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.parsers.sheet_list import (
    RandomKiid,
    build_sheet_list,
    compare_page_num,
    kiid,
    page_text,
    parsed_kiids,
    sort_by_page_numbers,
    str_num_cmp,
    to_long,
)
from kicad_workbench.utils.sch_hierarchy import TopLevelSheet
from kicad_workbench.utils.schematic_settings import erc_exclusions, subpart_settings

# The KiCad releases of the rules: the major version gives the version text.
RELEASE = {8: "8.0.9", 9: "9.0.9", 10: "10.0.6"}
VERSION_10 = "20260306"
VERSION_9 = "20250114"
VERSION_8 = "20231120"
ROOT_UUID = "11111111-1111-1111-1111-111111111111"
SHEET_UUIDS = ("22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333")
LIBRARY = """  (lib_symbols
    (symbol "Device:R" (symbol "R_1_1"))
    (symbol "Amp:LM358" (symbol "LM358_1_1" {name_a}) (symbol "LM358_2_1" {name_b}))
    (symbol "power:GND" (power) (symbol "GND_0_1")))
"""


def symbol(
    reference,
    path=f"/{ROOT_UUID}",
    lib="Device:R",
    unit=1,
    value="10k",
    at=(0, 0),
    uuid=None,
    instances=None,
):
    """A placed symbol. instances: the text of its (instances ...) (None: one instance)."""
    if instances is None:
        instances = (
            f'(instances (project "board" (path "{path}" (reference "{reference}") (unit {unit}))))'
        )
    uuid_text = f'(uuid "{uuid}")' if uuid else ""
    return (
        f'  (symbol (lib_id "{lib}") (at {at[0]} {at[1]} 0) (unit {unit}) {uuid_text}\n'
        f'    (property "Reference" "{reference}" (at 0 0 0))\n'
        f'    (property "Value" "{value}" (at 0 0 0))\n'
        f"    {instances})\n"
    )


def write_project(
    directory: Path,
    root_symbols: str,
    sub_symbols: str | None = None,
    version=VERSION_10,
    pages=("2", "3"),
    unit_names=("", ""),
    settings=None,
) -> Path:
    library = LIBRARY.format(
        name_a=f'(unit_name "{unit_names[0]}")' if unit_names[0] else "",
        name_b=f'(unit_name "{unit_names[1]}")' if unit_names[1] else "",
    )
    sheets = ""
    if sub_symbols is not None:
        for number, (uuid, page) in enumerate(zip(SHEET_UUIDS, pages, strict=True)):
            sheets += (
                f'  (sheet (at {10 * number} 0) (size 2 2) (uuid "{uuid}")\n'
                f'    (property "Sheetname" "S{number}" (at 0 0 0))\n'
                f'    (property "Sheetfile" "sub.kicad_sch" (at 0 0 0))\n'
                f'    (instances (project "board" (path "/{ROOT_UUID}" (page "{page}")))))\n'
            )
        (directory / "sub.kicad_sch").write_text(
            f'(kicad_sch (version {version}) (generator "eeschema") '
            f'(uuid "44444444-4444-4444-4444-444444444444")\n{library}{sub_symbols})\n',
            encoding="utf-8",
        )
    (directory / "board.kicad_sch").write_text(
        f'(kicad_sch (version {version}) (generator "eeschema") (uuid "{ROOT_UUID}")\n'
        f"{library}{root_symbols}{sheets}"
        '  (sheet_instances (path "/" (page "1"))))\n',
        encoding="utf-8",
    )
    project = directory / "board.kicad_pro"
    project.write_text(json.dumps({"schematic": settings or {}}), encoding="utf-8")
    return project


def messages(project: Path, kicad=10, view="erc", symbols="all") -> list[str]:
    check = check_annotation(project, RELEASE[kicad], view, symbols)
    assert check.problems == []
    return [error.message for error in check.errors]


def sub_path(index: int) -> str:
    return f"/{ROOT_UUID}/{SHEET_UUIDS[index]}"


class TestSplit:
    @pytest.mark.parametrize(
        "text, new, prefix, number, number_text",
        [
            ("U?", True, "U", -1, ""),
            ("U", True, "U", -1, ""),
            ("U12", False, "U", 12, "12"),
            ("U012", False, "U", 12, "012"),
            ("IC1A2", False, "IC1A", 2, "2"),
            ("123", False, "123", -1, ""),  # no prefix: the loop ends with no split
            ("U 1", False, "U", -1, " 1"),  # a space is part of the number text
            ("Ω1", False, "Ω1", -1, ""),  # the bytes of "Ω" are like spaces
            ("RΩ1", False, "R", -1, "Ω1"),
        ],
    )
    def test_split(self, text, new, prefix, number, number_text):
        ref = _Ref(None, None, text, 1, "", 0)  # type: ignore[arg-type]
        ref.split()
        assert (ref.is_new, ref.ref, ref.num, ref.num_str) == (new, prefix, number, number_text)

    def test_prefix_and_case(self):
        assert ref_des_prefix("R12?") == "R" and ref_des_prefix("12") == ""
        assert cmp_no_case("u1", "U1") == 0 and cmp_no_case("U1", "U10") == -1


class TestPages:
    def test_page_text_and_integer(self):
        assert page_text("") == "#" and page_text(" 1 \t2\n") == "12"
        assert to_long(" 12") == 12 and to_long("12 ") is None and to_long("+3") == 3
        assert to_long("") is None and to_long("1e3") is None

    def test_compare_page_num(self):
        assert compare_page_num("2", "10") == -1
        assert compare_page_num("10", "A") == -1  # integers first
        assert compare_page_num("A2", "A10") == -1  # StrNumCmp: numbers by value
        assert compare_page_num("a", "B") == 1  # with case
        assert compare_page_num("1", "01") is None  # "greater" in the two directions
        assert str_num_cmp("A01", "A1") == 0

    def test_kiid(self):
        assert kiid("ABC") == bytes(14) + bytes([0x0A, 0xBC])  # a legacy time stamp
        assert kiid("{" + ROOT_UUID + "}") == kiid(ROOT_UUID.upper())
        assert kiid(ROOT_UUID.replace("-", "")) == kiid(ROOT_UUID)
        assert isinstance(kiid("r"), RandomKiid) and kiid("r") != kiid("r")


class TestSheetList:
    def test_dfs_order_page_and_sort(self, tmp_path):
        project = write_project(tmp_path, "", "", pages=("B", "A"))
        root = project.with_suffix(".kicad_sch")
        sheets = build_sheet_list([TopLevelSheet(root, None, "board")], tmp_path, {}, 10)
        assert [(p.shown, p.dfs, sheets.page(p)) for p in sheets.paths] == [
            ("/", 1, "1"),
            ("/S0/", 2, "B"),
            ("/S1/", 3, "A"),
        ]
        ordered = sort_by_page_numbers(sheets.paths, sheets.page, lambda path: 0)
        assert [p.shown for p in ordered] == ["/", "/S1/", "/S0/"]
        assert sheets.hash_ties() == []


class TestMessages:
    def test_each_error(self, tmp_path):
        symbols = symbol("R1") + symbol("R1", value="1k", at=(5, 0))
        symbols += symbol("U1", lib="Amp:LM358", unit=1, value="LM358")
        symbols += symbol("U1", lib="Amp:LM358", unit=2, value="LM358P")
        symbols += symbol("R2", unit=2)
        symbols += symbol("U?", lib="Amp:LM358", unit=2, value="LM358")
        assert messages(write_project(tmp_path, symbols)) == [
            # Loop 1 gives only the first error.
            "Error: symbol R2B (unit 2) exceeds units defined (1)",
            "Duplicate items R1",
            "Different values for U1A (LM358) and U1B (LM358P)",
        ]

    def test_not_annotated_with_unit(self, tmp_path):
        symbols = symbol("U?", lib="Amp:LM358", unit=2, value="LM358") + symbol("R?")
        assert messages(write_project(tmp_path, symbols)) == [
            "Item not annotated: R?",
        ]
        symbols = symbol("U?", lib="Amp:LM358", unit=2, value="LM358")
        assert messages(write_project(tmp_path, symbols)) == ["Item not annotated: U? (unit 2)"]

    def test_duplicate_units_and_unit_counts(self, tmp_path):
        symbols = symbol("U1", lib="Amp:LM358", unit=2, value="LM358")
        symbols += symbol("U1", lib="Amp:LM358", unit=2, value="LM358", at=(5, 0))
        symbols += symbol("U2", lib="Amp:LM358", unit=2, value="LM358")
        symbols += symbol("U2", value="LM358")
        assert messages(write_project(tmp_path, symbols)) == [
            "Duplicate items U1B",
            "Differing unit counts for item U2A and U2B",
        ]

    def test_sort_uses_the_full_reference(self, tmp_path):
        # U01, U02, U1: U01 and U1 are no neighbours, thus KiCad does not find them.
        symbols = symbol("U01") + symbol("U02") + symbol("U1")
        assert messages(write_project(tmp_path, symbols)) == []
        assert messages(write_project(tmp_path, symbol("U01") + symbol("U1"))) == [
            "Duplicate items U01"
        ]

    def test_unit_names_of_kicad_10_and_settings(self, tmp_path):
        symbols = symbol("U1", lib="Amp:LM358", unit=1, value="A")
        symbols += symbol("U1", lib="Amp:LM358", unit=2, value="B")
        names = ("In", "")
        project = write_project(tmp_path, symbols, unit_names=names)
        assert messages(project, 10) == ["Different values for U1In (A) and U1B (B)"]
        project = write_project(tmp_path, symbols, unit_names=names, version=VERSION_9)
        assert messages(project, 9) == ["Different values for U1A (A) and U1B (B)"]
        settings = {"subpart_id_separator": 46, "subpart_first_id": 97}
        project = write_project(tmp_path, symbols, settings=settings)
        assert messages(project, 10) == ["Different values for U1.a (A) and U1.b (B)"]
        settings = {"subpart_id_separator": 200, "subpart_first_id": 49}
        project = write_project(tmp_path, symbols, settings=settings)
        assert messages(project, 10) == ["Different values for U11 (A) and U12 (B)"]

    def test_not_verified_versions(self, tmp_path):
        project = write_project(tmp_path, symbol("R1"))
        with pytest.raises(ValueError, match="no verified rules"):
            check_annotation(project, "10.99.0")


class TestLoadChanges:
    def test_missing_instance(self, tmp_path):
        # The symbol has only an instance of an other path: R5 sets the Reference field.
        other = (
            '(instances (project "other" (path "/00000000-0000-0000-0000-000000000001" '
            '(reference "R5"))) (project "board" '
            '(path "/00000000-0000-0000-0000-000000000002" (reference "R1"))))'
        )
        symbols = symbol("R1") + symbol("R9", instances=other, at=(5, 0))
        project = write_project(tmp_path, symbols, version=VERSION_9)
        assert messages(project, 9, "cli") == []  # the Reference field: R5
        assert messages(project, 9, "erc") == ["Item not annotated: R?"]
        project = write_project(tmp_path, symbols)
        # KiCad 10 copies the instance of the project.
        assert messages(project, 10, "cli") == ["Duplicate items R1"]
        assert messages(project, 10, "erc") == ["Duplicate items R1"]

    def test_power_symbols(self, tmp_path):
        symbols = symbol("#PWR01", lib="power:GND", value="GND")
        symbols += symbol("#PWR01", lib="power:GND", value="GND", at=(5, 0))
        symbols += symbol("#PWR?", lib="power:GND", value="GND", at=(9, 0))
        project = write_project(tmp_path, symbols, version=VERSION_8)
        # KiCad 8 gives the numbers before the check, KiCad 9 after it.
        assert messages(project, 8, "erc") == []
        assert messages(project, 8, "cli") == []
        project = write_project(tmp_path, symbols, version=VERSION_9)
        assert messages(project, 9, "erc") == [
            "Item not annotated: #PWR?",
            "Duplicate items #PWR01",
        ]
        assert messages(project, 9, "cli") == []
        project = write_project(tmp_path, symbols)
        assert messages(project, 10, "cli") == [
            "Item not annotated: #PWR?",
            "Duplicate items #PWR01",
        ]
        assert messages(project, 10, "cli", "non_power") == []

    def test_shared_sheet_and_page_repair(self, tmp_path):
        # The sheet is used two times. KiCad 10 gives the second page "2" a new page.
        sub = symbol(
            "R1",
            instances=(
                f'(instances (project "board" (path "{sub_path(0)}" (reference "R1")) '
                f'(path "{sub_path(1)}" (reference "R1"))))'
            ),
        )
        project = write_project(tmp_path, "", sub, pages=("2", "2"))
        check = check_annotation(project, "10.0.6")
        assert [e.message for e in check.errors] == ["Duplicate items R1"]
        assert [e.first.sheet for e in check.errors] == ["/S0/"]


class TestSettings:
    def test_subpart_settings(self, tmp_path):
        project = tmp_path / "p.kicad_pro"
        settings = subpart_settings(project)  # no project file: the defaults
        assert (settings.separator, settings.first_id) == (0, ord("A"))
        project.write_text('{"schematic": {"subpart_id_separator": true, "subpart_first_id": "a"}}')
        settings = subpart_settings(project)
        assert (settings.separator, settings.first_id) == (1, ord("A"))


class TestTopLevelSheets:
    def write(self, directory: Path, pages: tuple[str, str], first="top0.kicad_sch") -> Path:
        uuids = ("55555555-5555-5555-5555-555555555555", "66666666-6666-6666-6666-666666666666")
        library = LIBRARY.format(name_a="", name_b="")
        entries = []
        for number, (uuid, page) in enumerate(zip(uuids, pages, strict=True)):
            name = first if number == 0 else f"top{number}.kicad_sch"
            (directory / name).write_text(
                f'(kicad_sch (version {VERSION_10}) (generator "eeschema") (uuid "{uuid}")\n'
                f"{library}{symbol('R1', path=f'/{uuid}')}"
                f'  (sheet_instances (path "/" (page "{page}"))))\n',
                encoding="utf-8",
            )
            entries.append({"filename": name, "name": f"Top{number}", "uuid": uuid})
        project = directory / "board.kicad_pro"
        project.write_text(json.dumps({"schematic": {"top_level_sheets": entries}}))
        return project

    def test_order_by_hash(self, tmp_path):
        # The two top-level sheets have page "1" and DFS count 1: KiCad 10 repairs the
        # page of one of them, in the order of a hash of the path.
        check = check_annotation(self.write(tmp_path, ("1", "1")), "10.0.6")
        assert [e.message for e in check.errors] == ["Duplicate items R1"]
        assert any("cannot know" in problem for problem in check.problems)
        # kicad-cli loads only the root schematic: this project has none.
        with pytest.raises(ValueError, match="cannot load the top-level sheet"):
            check_annotation(self.write(tmp_path, ("1", "1")), "10.0.6", "cli")

    def test_kicad_cli_loads_all_top_level_sheets_from_10_0_7(self, tmp_path):
        project = self.write(tmp_path, ("1", "2"), first="board.kicad_sch")
        assert check_annotation(project, "10.0.6", "cli").errors == []  # only board.kicad_sch
        check = check_annotation(project, "10.0.7", "cli")
        assert [e.message for e in check.errors] == ["Duplicate items R1"]

    def test_known_order(self, tmp_path):
        check = check_annotation(self.write(tmp_path, ("1", "2")), "10.0.6")
        assert [e.message for e in check.errors] == ["Duplicate items R1"]
        assert [e.first.sheet for e in check.errors] == ["/Top0/"]
        assert check.problems == []


class TestReviewFindings:
    def test_path_with_no_items(self, tmp_path):
        # KiCad 8 and 9 add an instance after each item of a (path ...): an empty
        # path gives none, thus GetRef uses the Reference field. KiCad 10 adds an
        # instance with no reference, and it also clears the Reference field.
        empty = f'(instances (project "board" (path "/{ROOT_UUID}")))'
        project = write_project(tmp_path, symbol("R1", instances=empty), version=VERSION_9)
        assert messages(project, 9, "cli") == []
        project = write_project(tmp_path, symbol("R1", instances=empty))
        assert messages(project, 10, "cli") == ["Item not annotated: R?"]

    def test_duplicate_uuid_is_incremented(self):
        # parseKIID: a UUID that the file used before gets 1 more.
        root = parse(
            f'(kicad_sch (uuid "{ROOT_UUID}") (junction (uuid "{SHEET_UUIDS[0]}"))'
            f' (sheet (uuid "{SHEET_UUIDS[0]}") (pin "a" (uuid "{SHEET_UUIDS[0]}"))))'
        )
        kiids = sorted(parsed_kiids(root, 10).values())
        first = kiid(SHEET_UUIDS[0])
        assert kiids[1:] == [first, first[:-1] + b"\x23", first[:-1] + b"\x24"]
        assert kiids[0] == kiid(ROOT_UUID)

    def test_symbols_in_an_unknown_order(self, tmp_path):
        # Two symbols with no UUID at one position: std::sort gives them in any order.
        project = write_project(tmp_path, symbol("R1") + symbol("R1"))
        check = check_annotation(project, "10.0.6")
        assert [e.message for e in check.errors] == ["Duplicate items R1"]
        assert any("symbols in an order" in problem for problem in check.problems)

    def test_groups_and_orders(self):
        a, b, c, d = (object() for _ in range(4))
        merged = _merged([[a, b], [c, d], [b, c]])  # type: ignore[list-item]
        assert [{id(x) for x in group} for group in merged] == [{id(a), id(b), id(c), id(d)}]
        # The orders come one at a time: 11 paths do not make 11! orders first.
        big = [[object() for _ in range(11)]]
        assert len(next(_orders(big))) == 11  # type: ignore[arg-type]
        assert sum(1 for _ in _orders([[a, b, c]])) == 6  # type: ignore[list-item]


class TestErcDialog:
    UUIDS = ("aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002")

    def project(self, directory: Path, erc: dict) -> Path:
        symbols = symbol("R1", uuid=self.UUIDS[0]) + symbol("R1", uuid=self.UUIDS[1], at=(5, 0))
        project = write_project(directory, symbols)
        project.write_text(json.dumps({"erc": erc}), encoding="utf-8")
        return project

    def test_marker_text(self, tmp_path):
        project = self.project(tmp_path, {})
        (error,) = check_annotation(project, "10.0.6").errors
        assert marker_text(error) == f"duplicate_reference|0|0|{self.UUIDS[0]}|{self.UUIDS[1]}|||"

    @pytest.mark.parametrize(
        "severity, expected", [(None, "error"), ("warning", "warning"), ("ignore", None)]
    )
    def test_severity(self, tmp_path, severity, expected):
        erc = {"rule_severities": {"duplicate_reference": severity}} if severity else {}
        violations, notes = erc_violations(self.project(tmp_path, erc), "10.0.6")
        assert [v.severity for v in violations] == ([expected] if expected else [])
        if violations:
            assert violations[0].description == "Duplicate items R1"
            assert [i.description for i in violations[0].items] == ["Symbol R1 [R]"] * 2
            assert violations[0].items[1].x_mm == 5.0
        assert notes == []

    def test_exclusion(self, tmp_path):
        # kicad-cli gives an exclusion as an excluded violation: the tool does not add it.
        text = f"duplicate_reference|0|0|{self.UUIDS[0]}|{self.UUIDS[1]}|||"
        project = self.project(tmp_path, {"erc_exclusions": [[text, "a comment"]]})
        assert erc_violations(project, "10.0.6") == ([], [])
        # KiCad 8 reads only texts: it stops at the list, and ignores the exclusion.
        project = write_project(tmp_path, "", version=VERSION_8)
        project.write_text(json.dumps({"erc": {"erc_exclusions": [None, "x", [text, ""]]}}))
        exclusions = erc_exclusions(project, 8)
        assert exclusions.comments == {"x": ""} and "cannot read" in (exclusions.problem or "")

    @pytest.mark.parametrize(
        "kicad, entries, comments, stopped",
        [
            # KiCad 8 skips null, [] and {}.
            (8, [None, [], {}, "a"], {"a": ""}, False),
            (8, ["a", 1, "b"], {"a": ""}, True),
            # KiCad 9 and 10 skip each entry that is not a text or an array.
            (10, [None, 1, {}, "a", ["b", "c"]], {"a": "", "b": "c"}, False),
            # They keep the text of [text, <not a text>], then stop.
            (10, [["a", 1], "b"], {"a": ""}, True),
            (9, [[1, "x"], "b"], {}, True),
            (9, [["a"], "b"], {}, True),
        ],
    )
    def test_exclusion_entries(self, tmp_path, kicad, entries, comments, stopped):
        project = tmp_path / "p.kicad_pro"
        project.write_text(json.dumps({"erc": {"erc_exclusions": entries}}))
        exclusions = erc_exclusions(project, kicad)
        assert exclusions.comments == comments
        assert (exclusions.problem is not None) == stopped
