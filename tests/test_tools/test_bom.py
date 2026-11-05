"""Tests for the tool export_bom (tools/bom.py).

spike/kicad-versions/bom_check.py compares the tool with kicad-cli of KiCad
8.0.9, 9.0.9 and 10.0.6.
"""

import json
import os
import subprocess
import unicodedata

import pytest

from kicad_workbench.tools import bom
from kicad_workbench.utils import kicad_cli as kicad_cli_module

ROOT = """(kicad_sch (version 20250114) (generator "eeschema")
  (uuid "00000000-0000-0000-0000-000000000001")
  (symbol (lib_id "Device:R") (at 0 0 0) (unit 1)
    (property "Reference" "R1" (at 0 0 0))
    (instances (project "board"
      (path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1)
        (variant (name "Lite") (dnp yes))))))
  (sheet (at 0 0) (size 10 10)
    (property "Sheetname" "sub" (at 0 0 0))
    (property "Sheetfile" "sub.kicad_sch" (at 0 0 0))
    (instances (project "board"
      (path "/00000000-0000-0000-0000-000000000001" (page "2")
        (variant (name "Big") (dnp yes))))))
)
"""
SUB = """(kicad_sch (version 20250114) (generator "eeschema")
  (uuid "00000000-0000-0000-0000-000000000002")
  (symbol (lib_id "Device:C") (at 0 0 0) (unit 1)
    (property "Reference" "C1" (at 0 0 0))
    (instances (project "board"
      (path "/x/y" (reference "C1") (unit 1)
        (variant (name "Sub only") (field (name "MPN") (value "X")))))))
)
"""
CSV = '"Reference","Value","MPN","DNP"\n"R1","10k","RC0603, ""A""","DNP"\n"C1","100n","",""\n'


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "board.kicad_sch").write_text(ROOT, encoding="utf-8")
    (tmp_path / "sub.kicad_sch").write_text(SUB, encoding="utf-8")
    (tmp_path / "board.kicad_pro").write_text(
        '{"schematic": {"variants": [{"name": "Empty", "description": "no change"}]}}',
        encoding="utf-8",
    )
    state = {"calls": [], "version": "10.0.6", "code": 0, "csv": CSV}

    def kicad_cli(args, timeout, english):
        state["calls"].append(args)
        output = args[args.index("--output") + 1]
        code = 255 if state.get("fail") in args else state["code"]
        if code == 0:
            with open(output, "w", encoding="utf-8") as file:
                file.write(state["csv"])
        stdout = f"Warning: schematic has annotation errors\nWrote bill of materials to '{output}'."
        return subprocess.CompletedProcess(args, code, stdout.encode(), b"")

    async def version():
        return state["version"]

    monkeypatch.setattr(kicad_cli_module, "run_kicad_cli_sync", kicad_cli)
    monkeypatch.setattr(bom, "get_kicad_version", version)
    return {"directory": tmp_path, "state": state}


class TestBomArgs:
    def test_fields_and_options(self):
        args = bom.bom_args(["Reference", "${DNP}"], ["Value"], True, "Lite")
        assert args == [
            "--fields",
            "Reference,${DNP}",
            "--group-by",
            "Value",
            "--exclude-dnp",
            "--variant",
            "Lite",
        ]

    @pytest.mark.parametrize(
        ("fields", "group_by", "text"),
        [
            ([], [], "fields is empty"),
            (["Reference", ""], [], "An item of fields is empty"),
            (["Mfr, PN"], [], "has a comma"),
            (["Reference"], ["a,b"], "of group_by has a comma"),
        ],
    )
    def test_refused(self, fields, group_by, text):
        with pytest.raises(ValueError, match=text):
            bom.bom_args(fields, group_by, False, "")


class TestReadBom:
    def test_quotes_and_commas(self):
        rows = bom.read_bom(CSV.encode())
        assert rows[1] == ["R1", "10k", 'RC0603, "A"', "DNP"]

    def test_not_utf8(self):
        with pytest.raises(ValueError, match="not UTF-8"):
            bom.read_bom(b'"R1","\xff"\n')

    def test_empty(self):
        with pytest.raises(ValueError, match="no header line"):
            bom.read_bom(b"")


class TestBomFields:
    def test_names_of_each_version(self):
        fields = ["Reference", "DNP", "__Hidden", "Reference", "", "*", "MPN"]
        k10 = bom.bom_fields(fields, [], 10)
        assert [(f.name, f.shown) for f in k10.before] == [
            ("Reference", True),
            ("${DNP}", True),  # normalizeFieldName of KiCad 10
            ("Hidden", False),
        ]
        assert [f.name for f in k10.after] == ["MPN"] and k10.star
        k9 = bom.bom_fields(fields, [], 9)
        assert [(f.name, f.shown) for f in k9.before][1:] == [("DNP", True), ("Hidden", False)]
        k8 = bom.bom_fields(fields, [], 8)
        assert [(f.name, f.shown) for f in k8.before][1:] == [("DNP", True), ("__Hidden", True)]

    def test_groups(self):
        fields = bom.bom_fields(["Reference", "Value", "DNP", "*"], ["Value", "DNP"], 10)
        assert fields.grouped("Value") and fields.grouped("${DNP}")
        # KiCad does not read a --group-by name as a generated field: "DNP" groups
        # by ${DNP} only when fields has the item "DNP".
        fields = bom.bom_fields(["Reference", "${DNP}", "Value"], ["Value"], 10)
        assert not fields.grouped("${DNP}")
        with pytest.raises(ValueError, match="'DNP' is not one"):
            bom.bom_fields(["Reference", "${DNP}"], ["DNP"], 10)

    @pytest.mark.parametrize(
        ("fields", "group_by", "major", "text"),
        [
            (list(bom.DEFAULT_FIELDS), ["MPN"], 10, "'MPN' is not one"),  # only "*" adds MPN
            (["Reference", "Value"], ["Valeu"], 9, "'Valeu' is not one"),
            (["Reference", "__X"], ["__X"], 9, "'__X' is not one"),  # KiCad 9: the name "X"
            (["Reference", "*", "__MPN"], ["MPN"], 10, "after '\\*' cannot group"),
        ],
    )
    def test_group_refused(self, fields, group_by, major, text):
        with pytest.raises(ValueError, match=text):
            bom.bom_fields(fields, group_by, major)

    def test_hidden_group_kicad10(self):
        assert bom.bom_fields(["Reference", "__X"], ["__X"], 10).grouped("X")


class TestColumns:
    ROWS = [
        ["Reference", "MPN", "DNP", "Description", "Nope", "QUANTITY"],
        ["R1", "", "", "", "", "1"],
    ]

    def test_before_and_after_star(self):
        fields = bom.bom_fields(["Reference", "MPN", "${DNP}", "*", "Nope", "${QUANTITY}"], [], 10)
        columns = bom.field_columns(self.ROWS[0], fields)
        assert columns == {"Reference": 0, "MPN": 1, "${DNP}": 2, "Nope": 4, "${QUANTITY}": 5}
        assert bom.empty_columns(self.ROWS, columns) == ["MPN", "Nope"]

    def test_same_label(self):
        # KiCad 9: the label of ${QUANTITY} and of a field QUANTITY is "QUANTITY".
        rows = [["Reference", "QUANTITY", "QUANTITY"], ["R1", "1", ""]]
        fields = bom.bom_fields(["Reference", "${QUANTITY}", "QUANTITY"], [], 9)
        columns = bom.field_columns(rows[0], fields)
        assert columns == {"Reference": 0, "${QUANTITY}": 1, "QUANTITY": 2}
        assert bom.empty_columns(rows, columns) == ["QUANTITY"]

    def test_header_does_not_agree(self):
        fields = bom.bom_fields(["Reference", "MPN"], [], 10)
        assert bom.field_columns(["Reference"], fields) is None
        assert bom.field_columns(["Reference", "Value"], fields) is None


class TestBomRules:
    @pytest.mark.parametrize(("kicad", "major"), [("8.0.9", 8), ("9.0.9", 9), ("10.0.6", 10)])
    def test_known(self, kicad, major):
        assert bom.bom_rules(kicad) == major

    @pytest.mark.parametrize("kicad", ["10.99.0", "11.0.0", "7.0.11"])
    def test_unknown(self, kicad):
        with pytest.raises(ValueError, match="knows the BOM rules"):
            bom.bom_rules(kicad)


class TestVariantNames:
    def test_files_and_project(self, project):
        names = bom.variant_names(project["directory"] / "board.kicad_pro")
        assert names == {"Lite", "Big", "Sub only", "Empty"}

    def test_project_file_not_json(self, project):
        (project["directory"] / "board.kicad_pro").write_text("{", encoding="utf-8")
        names = bom.variant_names(project["directory"] / "board.kicad_pro")
        assert names == {"Lite", "Big", "Sub only"}


class TestExportBom:
    async def test_table_and_file(self, project):
        directory = project["directory"]
        out = await bom.export_bom(str(directory / "sub.kicad_sch"), output_path="out/bom.csv")
        args = project["state"]["calls"][0]
        # The BOM of the full project: kicad-cli reads the root schematic.
        assert args[-1] == str(directory / "board.kicad_sch")
        assert args[3:5] == ["--fields", ",".join(bom.DEFAULT_FIELDS)]
        assert (directory / "out" / "bom.csv").read_text(encoding="utf-8") == CSV
        assert f"The CSV file of kicad-cli is written: `{directory / 'out' / 'bom.csv'}`" in out
        assert "2 lines" in out
        # Each line of kicad-cli is a warning, but not the line of the output file.
        assert "⚠️ kicad-cli: Warning: schematic has annotation errors" in out
        assert "Wrote bill of materials" not in out
        assert '| R1 | 10k | RC0603, "A" | DNP |' in out
        assert "No line has a value" not in out

    async def test_replaced_file(self, project):
        directory = project["directory"]
        (directory / "bom.csv").write_text("old", encoding="utf-8")
        out = await bom.export_bom(str(directory / "board.kicad_sch"), output_path="bom.csv")
        assert "is replaced" in out
        assert (directory / "bom.csv").read_text(encoding="utf-8") == CSV

    async def test_no_file(self, project):
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"))
        assert "No CSV file is kept" in out
        assert sorted(p.name for p in project["directory"].iterdir()) == [
            "board.kicad_pro",
            "board.kicad_sch",
            "sub.kicad_sch",
        ]

    async def test_nightly(self, project):
        project["state"]["version"] = "10.99.0"
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"))
        assert out.startswith("❌ The installed KiCad is 10.99.0")
        assert project["state"]["calls"] == []

    async def test_empty_column(self, project):
        out = await bom.export_bom(
            str(project["directory"] / "board.kicad_sch"), fields=["Reference", "Value", "MPN"]
        )
        assert "⚠️ No line has a value in these columns: `MPN`" not in out
        project["state"]["csv"] = '"Reference","MPN"\n"R1",""\n'
        out = await bom.export_bom(
            str(project["directory"] / "board.kicad_sch"), fields=["Reference", "MPN"]
        )
        assert "⚠️ No line has a value in these columns: `MPN`" in out

    @pytest.mark.parametrize(
        ("group_by", "dnp_text"), [(["Value"], True), (["Value", "${DNP}"], False)]
    )
    async def test_groups(self, project, group_by, dnp_text):
        project["state"]["csv"] = '"Reference","Value","DNP"\n"R1,R2","10k","DNP"\n'
        out = await bom.export_bom(
            str(project["directory"] / "board.kicad_sch"),
            fields=["Reference", "Value", "${DNP}"],
            group_by=group_by,
        )
        assert "⚠️ The lines are groups of symbols" in out
        assert ("a line can show DNP" in out) is dnp_text

    async def test_variant(self, project):
        directory = project["directory"]
        out = await bom.export_bom(str(directory / "board.kicad_sch"), variant="Empty")
        assert project["state"]["calls"][0][-5:-3] == ["--variant", "Empty"]
        assert "variant 'Empty'" in out

    async def test_unknown_variant(self, project):
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"), variant="Nope")
        assert out.startswith("❌") and "Its variants: 'Big', 'Empty', 'Lite', 'Sub only'" in out
        assert project["state"]["calls"] == []

    async def test_variant_kicad9(self, project):
        project["state"]["version"] = "9.0.9"
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"), variant="Lite")
        assert out == "❌ kicad-cli of KiCad 9.0.9 has no variants. KiCad 10 and later have them."

    @pytest.mark.parametrize("name", ["x.kicad_sch", "Board.KICAD_PCB", "FP-Lib-Table", "link.csv"])
    async def test_output_is_kicad_file(self, project, name):
        (project["directory"] / "link.csv").symlink_to(project["directory"] / "board.kicad_sch")
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"), output_path=name)
        assert out.startswith("❌") and "is a KiCad file" in out
        assert project["state"]["calls"] == []
        assert (project["directory"] / "board.kicad_sch").read_text(encoding="utf-8") == ROOT

    async def test_output_with_variable(self, project):
        out = await bom.export_bom(
            str(project["directory"] / "board.kicad_sch"), output_path="${PROJECTNAME}.csv"
        )
        assert out.startswith("❌") and "does not expand variables" in out

    async def test_group_by_not_in_fields(self, project):
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"), group_by=["MPN"])
        assert out.startswith("❌ KiCad groups the symbols only by an item of fields")
        assert project["state"]["calls"] == []

    async def test_kicad_cli_error(self, project):
        project["state"]["code"] = 255
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"))
        assert out.startswith("❌ kicad-cli did not write the BOM of")
        assert "exit code 255: Warning: schematic has annotation errors" in out

    async def test_not_a_schematic(self, project):
        out = await bom.export_bom(str(project["directory"] / "board.kicad_pro"))
        assert out.startswith("❌") and "is not a .kicad_sch file" in out


class TestAllVariants:
    async def test_one_file_for_each_variant(self, project):
        directory = project["directory"]
        (directory / "bom.csv").write_text("old", encoding="utf-8")
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        calls = project["state"]["calls"]
        # The default variant first, with no --variant, then each variant.
        assert ["--variant" in args for args in calls] == [False, True, True, True, True]
        assert [args[args.index("--variant") + 1] for args in calls[1:]] == [
            "Big",
            "Empty",
            "Lite",
            "Sub only",
        ]
        files = ["bom.csv", "bom-Big.csv", "bom-Empty.csv", "bom-Lite.csv", "bom-Sub only.csv"]
        for name in files:
            assert (directory / name).read_text(encoding="utf-8") == CSV
        assert (
            "one run and one CSV file for each variant: the default variant and 4 variants." in out
        )
        assert (
            f"## Default variant\n\n2 lines. The CSV file is replaced: `{directory / 'bom.csv'}`"
            in out
        )
        assert (
            f"## Variant 'Lite'\n\n2 lines. The CSV file is written: `{directory / 'bom-Lite.csv'}`"
            in out
        )
        paths = ", ".join(json.dumps(str(directory / name)) for name in files)
        assert f"bom=[{paths}] (the default variant first)" in out
        assert "| R1 |" not in out  # no tables
        assert out.count("⚠️ kicad-cli: Warning: schematic has annotation errors") == 5

    async def test_no_variants(self, project):
        directory = project["directory"]
        (directory / "board.kicad_pro").write_text("{}", encoding="utf-8")
        for name in ("board.kicad_sch", "sub.kicad_sch"):
            text = (directory / name).read_text(encoding="utf-8")
            (directory / name).write_text(text.replace("(variant", "(other"), encoding="utf-8")
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert "the project has no variants, thus one BOM file." in out
        assert len(project["state"]["calls"]) == 1

    async def test_failure_writes_no_file(self, project):
        project["state"]["fail"] = "Lite"
        directory = project["directory"]
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert out.startswith("❌ kicad-cli did not write the BOM of the variant 'Lite'")
        assert (
            "exit code 255: Warning: schematic has annotation errors. The tool changed no file."
            in out
        )
        assert not list(directory.glob("bom*.csv"))

    @pytest.mark.parametrize(
        ("variants", "text"),
        [
            (["A/B"], "has the character '/'"),
            (["A", "a"], "have the same path"),
        ],
    )
    async def test_file_names(self, project, variants, text):
        directory = project["directory"]
        names = ", ".join(f'{{"name": "{v}"}}' for v in variants)
        (directory / "board.kicad_pro").write_text(
            '{"schematic": {"variants": [' + names + "]}}", encoding="utf-8"
        )
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert out.startswith("❌") and text in out
        assert project["state"]["calls"] == []

    @pytest.mark.parametrize(
        ("arguments", "text"),
        [
            ({"variant": "Lite"}, "Give variant or all_variants, not the two."),
            ({"output_path": ""}, "give output_path"),
        ],
    )
    async def test_refused(self, project, arguments, text):
        args = {"output_path": "bom.csv", "all_variants": True, **arguments}
        out = await bom.export_bom(str(project["directory"] / "board.kicad_sch"), **args)
        assert out.startswith("❌") and text in out
        assert project["state"]["calls"] == []

    def write_variants(self, directory, variants):
        """A project with only these variants (in the project file)."""
        for name in ("board.kicad_sch", "sub.kicad_sch"):
            text = (directory / name).read_text(encoding="utf-8")
            (directory / name).write_text(text.replace("(variant", "(other"), encoding="utf-8")
        (directory / "board.kicad_pro").write_text(
            json.dumps({"schematic": {"variants": [{"name": v} for v in variants]}}),
            encoding="utf-8",
        )

    async def test_group_by_gives_no_project_check(self, project):
        directory = project["directory"]
        self.write_variants(directory, ["Lite"])
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"),
            output_path="bom.csv",
            all_variants=True,
            fields=["Reference", "Value"],
            group_by=["Value"],
        )
        assert "⚠️ The lines are groups of symbols" in out
        assert (
            "For project_check" not in out and "variant: the default variant and 1 variant." in out
        )

    async def test_empty_variant_name(self, project):
        directory = project["directory"]
        self.write_variants(directory, ["", "Lite"])
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert len(project["state"]["calls"]) == 2 and "and 1 variant." in out
        assert not (directory / "bom-.csv").exists()

    async def test_unicode_forms(self, project):
        directory = project["directory"]
        self.write_variants(
            directory, [unicodedata.normalize("NFC", "Café"), unicodedata.normalize("NFD", "Café")]
        )
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert out.startswith("❌") and "have the same path" in out

    async def test_variant_file_is_link_to_kicad_file(self, project):
        directory = project["directory"]
        self.write_variants(directory, ["Lite"])
        (directory / "bom-Lite.csv").symlink_to(directory / "board.kicad_sch")
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert out.startswith("❌") and "is a KiCad file" in out
        assert project["state"]["calls"] == []

    async def test_write_failure_changes_no_file(self, project, monkeypatch):
        directory = project["directory"]
        self.write_variants(directory, ["Lite"])
        (directory / "bom.csv").write_text("old", encoding="utf-8")
        calls = []
        fdopen = os.fdopen

        def failing(handle, mode):
            calls.append(handle)
            if len(calls) == 2:
                os.close(handle)
                raise OSError("disk full")
            return fdopen(handle, mode)

        monkeypatch.setattr(bom.os, "fdopen", failing)
        out = await bom.export_bom(
            str(directory / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert out == "❌ The BOM files cannot be written: disk full. The tool changed no file."
        assert (directory / "bom.csv").read_text(encoding="utf-8") == "old"
        assert sorted(p.name for p in directory.glob("*bom*")) == ["bom.csv"]

    async def test_kicad9(self, project):
        project["state"]["version"] = "9.0.9"
        out = await bom.export_bom(
            str(project["directory"] / "board.kicad_sch"), output_path="bom.csv", all_variants=True
        )
        assert out == "❌ kicad-cli of KiCad 9.0.9 has no variants. KiCad 10 and later have them."
