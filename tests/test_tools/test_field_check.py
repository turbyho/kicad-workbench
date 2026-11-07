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


HEADER = '"Reference","Value","Footprint","MPN","Value_Confirmed","MPN_Confirmed"\n'
# A confirmed symbol: Value_Confirmed and MPN_Confirmed are the copies of Value and MPN.
CONFIRMED = '"Q1","2N7002","","2N7002LT1G","2N7002","2N7002LT1G"\n'


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
        # The lines of the DNP symbols: only with no --exclude-dnp.
        dnp = "" if "--exclude-dnp" in args else state.get("dnp", "")
        with open(output, "w", encoding="utf-8") as file:
            file.write(state["csv"][variant] + dnp)
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
        project["state"]["csv"] = {"": HEADER + CONFIRMED, "Lite": HEADER + CONFIRMED}
        out = await check(project)
        calls = project["state"]["calls"]
        # One BOM of each variant, with each field and also the DNP symbols (datasheet-lens
        # checks the fields of a DNP symbol too).
        assert [args[args.index("--fields") + 1] for args in calls] == ["Reference,Value,*"] * 2
        assert not any("--exclude-dnp" in args for args in calls)
        assert "--variant" not in calls[0] and calls[1][calls[1].index("--variant") + 1] == "Lite"
        assert (
            "the lines of the default variant: 1, the variant 'Lite': 1. Symbols with a part: 1."
            in out
        )
        assert (
            "✅ The fields Value, MPN, Value_Confirmed and MPN_Confirmed of all BOM lines (also "
            "the DNP symbols, in each variant) follow the convention" in out
        )

    async def test_a_dnp_symbol_gets_the_check_of_a_fitted_symbol(self, project):
        # datasheet-lens checks the fields of a DNP symbol too (its commit 65168da): the
        # user can fit it at any time.
        project["state"]["csv"] = {"": HEADER + CONFIRMED, "Lite": HEADER + CONFIRMED}
        project["state"]["dnp"] = '"D1","BAT54","","BAT54C","BAT54","BAT54A"\n'
        out = await check(project)
        assert "⚠️ change in the CAD project: D1:" in out, out
        assert "✅" not in out

    async def test_messages_and_writes(self, project):
        lines = (
            '"Q1","2N7002","","2N7002LT1G, BSS138LT1G","2N7002","2N7002LT1G"\n'
            + '"R5","10k (RC0603FR-0710KL)","","RC0603FR-0710KL","10k (RC0603FR-0710KL)",""\n'
            + '"C5","220nF","","GRM1","100nF","GRM1"\n'
            + '"R7","10k","","","10k","A1"\n'
        )
        project["state"]["csv"] = {
            "": HEADER + lines + '"R1","10k","","","",""\n',
            "Lite": HEADER + lines + '"R1","1k","","","",""\n',
        }
        out = await check(project)
        assert (
            "- ⚠️ change in the CAD project: Q1: MPN '2N7002LT1G, BSS138LT1G', MPN_Confirmed "
            "'2N7002LT1G': the MPN is not the confirmed MPN. New: BSS138LT1G. Find the datasheet "
            "of each new MPN now (this changes no project), and give it to the user with the "
            'change. Tell the user. When the user confirms the change: Write `{"reference": '
            '"Q1", "fields": {"MPN_Confirmed": "2N7002LT1G, BSS138LT1G", "Value_Confirmed": '
            '"2N7002"}}`. Else the Value and the MPN must change back.'
        ) in out
        assert (
            "- ⚠️ change in the CAD project: C5: Value '220nF', Value_Confirmed '100nF': the MPN "
            "'GRM1' can be wrong now. Ask the user: a new part for the new value, or the same "
            "part. A new part: select it with the user, write its MPN into the field MPN, then "
            'run check_symbol_fields again. The same part: Write `{"reference": "C5", '
            '"fields": {"MPN_Confirmed": "GRM1", "Value_Confirmed": "220nF"}}`. Else the Value '
            "must change back."
        ) in out
        assert (
            "- the old convention: R5: the MPNs are in the parentheses of Value. Move them into "
            "the field MPN (Value holds the general value, MPN the MPNs, the hidden fields "
            "Value_Confirmed and MPN_Confirmed the confirmed copies). A confirmed symbol stays "
            "confirmed, and the parts do not change. Tell the user (a text in parentheses that is "
            "a part of the general value stays), and when the user agrees: Write "
            '`{"reference": "R5", "fields": {"Value": "10k", "MPN": "RC0603FR-0710KL", '
            '"MPN_Confirmed": "RC0603FR-0710KL", "Value_Confirmed": "10k"}}`.'
        ) in out
        # A change with no new MPN gives no step for a datasheet.
        assert (
            "- ⚠️ change in the CAD project: R7: MPN '(empty)', MPN_Confirmed 'A1': the MPN is "
            "not the confirmed MPN. Not in MPN now: A1. Tell the user."
        ) in out
        # The variant gives only its own messages: "no part selected" one time for R1, as
        # check_parts of datasheet-lens.
        assert "no part selected (a general value and no MPN): R1. A part" in out
        for reference in ("Q1", "R5", "C5", "R7"):
            assert out.count(f'"reference": "{reference}"') == 1
        assert "Give all changes in one call" in out
        assert "✅" not in out

    async def test_variant_write(self, project):
        project["state"]["csv"] = {
            "": HEADER + CONFIRMED,
            "Lite": HEADER + '"Q1","BSS138","","BSS138LT1G","2N7002","2N7002LT1G"\n',
        }
        out = await check(project)
        assert (
            '"fields": {"MPN_Confirmed": "BSS138LT1G", "Value_Confirmed": "BSS138"}, '
            '"variant": "Lite"}' in out
        )
        assert (
            "change in the CAD project: Q1 (variant Lite): Value 'BSS138', Value_Confirmed "
            "'2N7002'; MPN 'BSS138LT1G', MPN_Confirmed '2N7002LT1G': the value and the part are "
            "not the confirmed ones. New: BSS138LT1G. Not in MPN now: 2N7002LT1G." in out
        )

    async def test_units_with_different_mpns(self, project):
        """The units of one package with different MPNs: the error of the design, and a
        conflict of the line, with no proposal (the joined MPNs look like a list)."""
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "LM358DR"), placed("U1", 2, "LM358DT")), encoding="utf-8"
        )
        line = '"U1","LM358","","LM358DR,LM358DT","",""\n'
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
        assert "## Not confirmed" not in out and "Write `" not in out

    async def test_variant_repairs_the_units(self, project):
        """A variant that makes the MPNs of the units equal has no conflict."""
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "LM358DR", "LM358DT"), placed("U1", 2, "LM358DT")),
            encoding="utf-8",
        )
        project["state"]["csv"] = {
            "": HEADER + '"U1","LM358","","LM358DR,LM358DT","",""\n',
            "Lite": HEADER + '"U1","LM358","","LM358DT","",""\n',
        }
        out = await check(project)
        assert "- ❌ conflict in the CAD project: U1: the units" in out
        assert (
            "- not confirmed: U1 (variant Lite): Not confirmed: LM358DT. When the user agrees: "
            'Write `{"reference": "U1", "fields": {"MPN_Confirmed": "LM358DT", "Value_Confirmed": '
            '"LM358"}, "variant": "Lite"}`. The symbol has no field MPN_Confirmed, '
            "Value_Confirmed: first write them into the symbol (a change with no variant)."
        ) in out

    async def test_no_confirmed_fields(self, project):
        header = '"Reference","Value","MPN"\n'
        project["state"]["version"] = "9.0.9"
        project["state"]["csv"] = {"": header + '"R1","10k (A)",""\n"R2","10k","B"\n'}
        out = await check(project)
        assert len(project["state"]["calls"]) == 1  # KiCad 9: no variants
        # The move writes the new fields: MPN_Confirmed, but no Value_Confirmed (the
        # project has no such field: the user decides).
        assert (
            'when the user agrees: Write `{"reference": "R1", "fields": {"Value": "10k", "MPN": '
            '"A", "MPN_Confirmed": ""}}`.' in out
        )
        assert "⚠️ no field Value_Confirmed: 2 symbols." in out
        assert "⚠️ no field MPN_Confirmed: 1 symbols. A change of a part (an other MPN)" in out

    async def test_no_check_mark_with_errors_or_warnings(self, project):
        """No ✅ when the units of a symbol that the BOM does not have (here the BOM of
        the test has no line of U1; in KiCad: Exclude from BOM) have different fields,
        or when kicad-cli gave a warning."""
        directory = project["directory"]
        project["state"]["csv"] = {"": HEADER + CONFIRMED, "Lite": HEADER + CONFIRMED}
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

    async def test_fields_of_the_symbol(self, project):
        """A BOM line with no MPN: the write goes into the MPN field that the symbol has
        (also an empty one). A symbol with no MPN field in a project with more names:
        the user selects the field."""
        directory = project["directory"]
        mfr = placed("U1", 1, "").replace('"MPN"', '"Manufacturer Part Number"')
        other = placed("U2", 1, "").replace('(property "MPN" "" (at 0 0 0))', "")
        (directory / "board.kicad_sch").write_text(
            schematic(mfr, other, placed("U3", 1, "A")), encoding="utf-8"
        )
        header = '"Reference","Value","MPN","Manufacturer Part Number"\n'
        lines = '"U1","LM358 (LM358DR)","",""\n"U2","LM358 (LM358DT)","",""\n'
        project["state"]["csv"] = {"": header + lines, "Lite": header + lines}
        out = await check(project)
        assert (
            "the old convention: U1: the MPNs are in the parentheses of Value." in out
            and 'Write `{"reference": "U1", "fields": {"Value": "LM358", "Manufacturer Part '
            'Number": "LM358DR", "MPN_Confirmed": ""}}`.'
            in out
        )
        assert (
            'Write `{"reference": "U2", "fields": {"Value": "LM358", "MPN_Confirmed": "", '
            '"<the field for the MPN: MPN or Manufacturer Part Number>": "LM358DT"}}`. The '
            "field for the MPN is not known (MPN, Manufacturer Part Number): ask the user which "
            "one the symbol uses, and put its name in place of the name in angle brackets." in out
        )

    async def test_variant_field_that_the_symbol_does_not_have(self, project):
        """A variant can set only a field that the symbol has (set_symbol_fields)."""
        directory = project["directory"]
        no_mpn = placed("U1", 1, "").replace('(property "MPN" "" (at 0 0 0))', "")
        (directory / "board.kicad_sch").write_text(
            schematic(no_mpn, placed("U3", 1, "A")), encoding="utf-8"
        )
        header = '"Reference","Value","MPN"\n'
        project["state"]["csv"] = {
            "": header + '"U1","LM358",""\n',
            "Lite": header + '"U1","LM358 (LM358DT)",""\n',
        }
        out = await check(project)
        assert (
            '"fields": {"Value": "LM358", "MPN": "LM358DT", "MPN_Confirmed": ""}, "variant": '
            '"Lite"}`. The symbol has no field MPN, MPN_Confirmed: first write them into the '
            "symbol (a change with no variant)." in out
        )

    async def test_more_own_fields(self, project):
        """The units have empty MPN fields of two names: the user selects the field."""
        directory = project["directory"]
        unit_b = placed("U1", 2, "").replace('"MPN"', '"MP"')
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, ""), unit_b), encoding="utf-8"
        )
        line = '"U1","LM358 (LM358DT)","","","",""\n'
        project["state"]["csv"] = {"": HEADER + line, "Lite": HEADER + line}
        out = await check(project)
        assert '"<the field for the MPN: MPN or MP>": "LM358DT"' in out

    async def test_not_annotated(self, project):
        line = '"U?","LM358 (LM358DT)","","","",""\n'
        project["state"]["csv"] = {"": HEADER + line, "Lite": HEADER + line}
        out = await check(project)
        assert (
            "- ❌ conflict in the CAD project: U?: a reference that is not annotated: "
            "set_symbol_fields cannot find the symbol. Annotate the schematic in KiCad first."
            in out
        )
        assert "Write `" not in out

    async def test_duplicate_reference(self, project):
        """KiCad joins the fields of the symbols of a duplicate reference in one BOM
        line: a conflict, and no proposal."""
        directory = project["directory"]
        (directory / "board.kicad_sch").write_text(
            schematic(placed("U1", 1, "A"), placed("U1", 1, "B")), encoding="utf-8"
        )
        line = '"U1","LM358","","A,B","",""\n'
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
