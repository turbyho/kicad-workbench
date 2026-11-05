"""Tests for the tool check_symbol_fields (tools/field_check.py).

spike/kicad-versions/field_check_check.py compares the tool with kicad-cli of
KiCad 8.0.9, 9.0.9 and 10.0.6, and with project_check of datasheet-lens.
"""

import subprocess

import pytest

from kicad_workbench.tools import bom, field_check
from kicad_workbench.utils import kicad_cli as kicad_cli_module


def placed(reference, unit, mpn, variant_mpn=""):
    variant = (
        f'(variant (name "Lite") (field (name "MPN") (value "{variant_mpn}")))'
        if variant_mpn
        else ""
    )
    return f"""  (symbol (lib_id "Amplifier_Operational:LM358") (at 0 0 0) (unit {unit})
    (property "Reference" "{reference}" (at 0 0 0))
    (property "Value" "LM358" (at 0 0 0))
    (property "MPN" "{mpn}" (at 0 0 0))
    (instances (project "board"
      (path "/00000000-0000-0000-0000-000000000001" (reference "{reference}") (unit {unit})
        {variant}))))
"""


def schematic(*symbols):
    return (
        '(kicad_sch (version 20250922) (generator "eeschema")\n'
        '  (uuid "00000000-0000-0000-0000-000000000001")\n' + "".join(symbols) + ")\n"
    )


HEADER = '"Reference","Value","Footprint","MPN","Value_Confirmed"\n'


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "board.kicad_sch").write_text(schematic(), encoding="utf-8")
    (tmp_path / "board.kicad_pro").write_text(
        '{"schematic": {"variants": [{"name": "Lite"}]}}', encoding="utf-8"
    )
    state = {"calls": [], "version": "10.0.6", "csv": {"": HEADER, "Lite": HEADER}}

    def kicad_cli(args, timeout, english):
        state["calls"].append(args)
        output = args[args.index("--output") + 1]
        variant = args[args.index("--variant") + 1] if "--variant" in args else ""
        with open(output, "w", encoding="utf-8") as file:
            file.write(state["csv"][variant])
        stdout = state.get("warning", "") + f"Wrote bill of materials to '{output}'."
        return subprocess.CompletedProcess(args, 0, stdout.encode(), b"")

    async def version():
        return state["version"]

    monkeypatch.setattr(kicad_cli_module, "run_kicad_cli_sync", kicad_cli)
    monkeypatch.setattr(field_check, "get_kicad_version", version)
    monkeypatch.setattr(bom, "get_kicad_version", version)
    return {"directory": tmp_path, "state": state}


async def check(project):
    return await field_check.check_symbol_fields(str(project["directory"] / "board.kicad_sch"))


class TestCheckSymbolFields:
    async def test_all_confirmed(self, project):
        line = '"Q1","2N7002 (2N7002LT1G)","","2N7002LT1G","2N7002 (2N7002LT1G)"\n'
        project["state"]["csv"] = {"": HEADER + line, "Lite": HEADER + line}
        out = await check(project)
        calls = project["state"]["calls"]
        # One BOM of each variant, with each field and no DNP symbols.
        assert [args[args.index("--fields") + 1] for args in calls] == ["Reference,Value,*"] * 2
        assert all("--exclude-dnp" in args for args in calls)
        assert "--variant" not in calls[0] and calls[1][calls[1].index("--variant") + 1] == "Lite"
        assert (
            "the lines of the default variant: 1, the variant 'Lite': 1. Symbols with a part: 1."
            in out
        )
        assert (
            "✅ The fields Value, MPN and Value_Confirmed of all BOM lines (all symbols that "
            "are not DNP, in each variant) follow the convention" in out
        )

    async def test_messages_and_writes(self, project):
        project["state"]["csv"] = {
            "": HEADER
            + '"Q1","2N7002 (2N7002LT1G, BSS138LT1G)","","2N7002LT1G","2N7002 (2N7002LT1G)"\n'
            + '"R5","10k","","RC0603FR-0710KL","10k"\n'
            + '"R1","10k","","",""\n',
            "Lite": HEADER
            + '"Q1","2N7002 (2N7002LT1G, BSS138LT1G)","","2N7002LT1G","2N7002 (2N7002LT1G)"\n'
            + '"R5","10k","","RC0603FR-0710KL","10k"\n'
            + '"R1","1k","","",""\n',
        }
        out = await check(project)
        assert (
            "- ⚠️ change in the CAD project: Q1: Value '2N7002 (2N7002LT1G, BSS138LT1G)', "
            "Value_Confirmed '2N7002 (2N7002LT1G)': the confirmed MPN '2N7002LT1G' can be wrong "
            'now. New: BSS138LT1G. Tell the user. When the user confirms the change: Write `{"'
            'reference": "Q1", "fields": {"MPN": "2N7002LT1G, BSS138LT1G", "Value_Confirmed": '
            '"2N7002 (2N7002LT1G, BSS138LT1G)"}}`. Else the Value must change back.'
        ) in out
        assert (
            "- complete the fields: R5: the MPN is not in Value (the convention: the MPNs in "
            'parentheses in Value). The parts do not change. Write `{"reference": "R5", '
            '"fields": {"Value": "10k (RC0603FR-0710KL)", "MPN": "RC0603FR-0710KL", '
            '"Value_Confirmed": "10k (RC0603FR-0710KL)"}}`.'
        ) in out
        # The variant gives only its own messages: "no part selected" one time for R1, as
        # check_parts of datasheet-lens.
        assert "no part selected (a general value and no MPN): R1. A part" in out
        assert out.count('"reference": "Q1"') == 1 and out.count('"reference": "R5"') == 1
        assert "Give all changes in one call" in out
        assert "✅" not in out

    async def test_variant_write(self, project):
        line = '"Q1","2N7002 (2N7002LT1G)","","2N7002LT1G","2N7002 (2N7002LT1G)"\n'
        project["state"]["csv"] = {
            "": HEADER + line,
            "Lite": HEADER + '"Q1","BSS138 (BSS138LT1G)","","2N7002LT1G","2N7002 (2N7002LT1G)"\n',
        }
        out = await check(project)
        assert (
            '"fields": {"MPN": "BSS138LT1G", "Value_Confirmed": "BSS138 (BSS138LT1G)"}, '
            '"variant": "Lite"}' in out
        )
        assert "change in the CAD project: Q1 (variant Lite):" in out

    async def test_units_with_different_mpns(self, project):
        """The units of one package with different MPNs: the error of the design, and a
        conflict of the line, with no proposal (the joined MPNs look like a list)."""
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "LM358DR"), placed("U1", 2, "LM358DT")), encoding="utf-8"
        )
        line = '"U1","LM358","","LM358DR,LM358DT",""\n'
        project["state"]["csv"] = {"": HEADER + line, "Lite": HEADER + line}
        out = await check(project)
        assert out.index("❌ ERROR IN THE DESIGN: the units of one package") < out.index(
            "## Conflicts"
        )
        assert "- U1: MPN: A = 'LM358DR', B = 'LM358DT'" in out  # the error gives the values
        assert (
            "- ❌ conflict in the CAD project: U1: the units of one package have different "
            "fields (see the error of the design above). Tell the user: one of the fields is "
            "wrong."
        ) in out
        assert out.count("U1: the units of one package") == 1  # not U1 (variant Lite)
        assert "## Complete the fields" not in out and "Write `" not in out

    async def test_variant_repairs_the_units(self, project):
        """A variant that makes the MPNs of the units equal has no conflict."""
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "LM358DR", "LM358DT"), placed("U1", 2, "LM358DT")),
            encoding="utf-8",
        )
        project["state"]["csv"] = {
            "": HEADER + '"U1","LM358","","LM358DR,LM358DT",""\n',
            "Lite": HEADER + '"U1","LM358","","LM358DT",""\n',
        }
        out = await check(project)
        assert "- ❌ conflict in the CAD project: U1: the units" in out
        assert (
            "complete the fields: U1 (variant Lite): the MPN is not in Value (the convention: "
            "the MPNs in parentheses in Value). The parts do not change. Write "
            '`{"reference": "U1", "fields": {"Value": "LM358 (LM358DT)", "MPN": "LM358DT", '
            '"Value_Confirmed": "LM358 (LM358DT)"}, "variant": "Lite"}`.'
        ) in out

    async def test_no_field_value_confirmed(self, project):
        header = '"Reference","Value","MPN"\n'
        project["state"]["version"] = "9.0.9"
        project["state"]["csv"] = {"": header + '"R1","10k (A)",""\n'}
        out = await check(project)
        assert len(project["state"]["calls"]) == 1  # KiCad 9: no variants
        assert (
            '- not confirmed: R1: When the user agrees: Write `{"reference": "R1", "fields": {"MPN": "A"}}`.'
            in out
        )
        assert "⚠️ no field Value_Confirmed: 1 symbols." in out

    async def test_no_check_mark_with_errors_or_warnings(self, project):
        """No ✅ when the units of a symbol that is not in the BOM (DNP) have different
        fields, or when kicad-cli gave a warning."""
        directory = project["directory"]
        line = '"Q1","2N7002 (2N7002LT1G)","","2N7002LT1G","2N7002 (2N7002LT1G)"\n'
        project["state"]["csv"] = {"": HEADER + line, "Lite": HEADER + line}
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "LM358DR"), placed("U1", 2, "LM358DT")), encoding="utf-8"
        )
        out = await check(project)
        assert "❌ ERROR IN THE DESIGN" in out and "✅" not in out
        assert "⚠️ The BOM lines give no message, but the tool did not check the full" in out
        (directory / "board.kicad_sch").write_text(schematic(), encoding="utf-8")
        project["state"]["warning"] = "Warning: a library is not found\n"
        out = await check(project)
        assert "⚠️ kicad-cli: Warning: a library is not found" in out and "✅" not in out
        project["state"]["warning"] = ""
        assert "✅" in await check(project)

    async def test_duplicate_reference(self, project):
        """KiCad joins the fields of the symbols of a duplicate reference in one BOM
        line: a conflict, and no proposal."""
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "A"), placed("U1", 1, "B")), encoding="utf-8"
        )
        line = '"U1","LM358 (A)","","A,B",""\n'
        project["state"]["csv"] = {"": HEADER + line, "Lite": HEADER + line}
        out = await check(project)
        assert "U1: a duplicate reference (the same unit two times" in out
        assert (
            "- ❌ conflict in the CAD project: U1: a duplicate reference: KiCad joins the fields "
            "of its symbols in one BOM line. Annotate the schematic in KiCad first." in out
        )
        assert "Write `" not in out and "✅" not in out

    async def test_sheet_hierarchy_that_the_tool_cannot_read(self, project):
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            '(kicad_sch (version 20250922) (generator "eeschema")\n'
            '  (uuid "00000000-0000-0000-0000-000000000001")\n'
            "  (sheet (at 0 0) (size 10 10)\n"
            '    (property "Sheetname" "sub" (at 0 0 0))\n'
            '    (property "Sheetfile" "sub.kicad_sch" (at 0 0 0))))\n',
            encoding="utf-8",
        )
        out = await check(project)
        assert out.startswith("❌ The tool cannot read the full sheet hierarchy")
        assert project["state"]["calls"] == []

    async def test_unit_that_is_not_a_number(self, project):
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", "x", "A"), placed("U1", 2, "B")), encoding="utf-8"
        )
        out = await check(project)
        assert out.startswith("❌ The schematic files of the project")

    async def test_errors(self, project):
        out = await field_check.check_symbol_fields(str(project["directory"] / "none.kicad_sch"))
        assert out.startswith("❌ The schematic") and "does not exist" in out
        project["state"]["version"] = None
        out = await check(project)
        assert out.startswith("❌")
