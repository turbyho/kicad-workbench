"""Tests for the ERC/DRC JSON report parsers and the validation tools."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.parsers.check_report_parser import (
    ErcUnits,
    corrected_length_text,
    erc_report_units,
    parse_drc_report,
    parse_erc_item,
    parse_erc_report,
    read_erc_report,
)
from kicad_workbench.parsers.sch_geometry import iu
from kicad_workbench.tools import validation
from kicad_workbench.utils.kicad_cli import KicadCliError, find_kicad_cli

from .locale_env import can_name, non_utf8_env, run_python

FIXTURES = Path(__file__).parent.parent / "fixtures"
REPORTS = FIXTURES / "reports"
LABEL_SCH = REPORTS / "dangling_label.kicad_sch"


def _item(description: str, x: float = 0.0, y: float = 0.0) -> dict:
    """An item of a violation, as KiCad writes it (RC_JSON::AFFECTED_ITEM)."""
    return {"uuid": "00000000-0000-0000-0000-000000000001", "description": description,
            "pos": {"x": x, "y": y}}  # fmt: skip


def _load(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


class TestErcItem:
    def test_pin_with_name(self):
        item = parse_erc_item(_item("Symbol U1 Pin 3 [SDA, Bidirectional, Line]"))
        assert (item.reference, item.pin_number, item.pin_name, item.pin_type) == (
            "U1",
            "3",
            "SDA",
            "Bidirectional",
        )

    def test_pin_without_name(self):
        item = parse_erc_item(_item("Symbol #PWR01 Pin 1 [Power input, Line]"))
        assert (item.reference, item.pin_number, item.pin_name, item.pin_type) == (
            "#PWR01",
            "1",
            "",
            "Power input",
        )

    def test_pin_name_with_comma(self):
        item = parse_erc_item(_item("Symbol U2 Pin A1 [IN, OUT, Bidirectional, Line]"))
        assert item.pin_name == "IN, OUT"
        assert item.pin_type == "Bidirectional"

    def test_pin_of_missing_library_symbol(self):
        item = parse_erc_item(_item("Pin 1 [???, ?]"))
        assert (item.reference, item.pin_number) == ("", "1")

    @pytest.mark.parametrize("reference", ["C?", "#PWR?", "C"])
    def test_symbol_that_is_not_annotated(self, reference):
        # The English text has the Reference field as it is (SCH_SYMBOL::GetItemDescription):
        # also "C?" (UTIL::GetRefDesUnannotated) or "C" (no number: SCH_REFERENCE::Split).
        item = parse_erc_item(_item(f"Symbol {reference} [CP]"))
        assert (item.reference, item.value) == (reference, "CP")
        item = parse_erc_item(_item(f"Symbol {reference} Pin 1 [Passive, Line]"))
        assert (item.reference, item.pin_number) == (reference, "1")

    def test_localized_symbol_that_is_not_annotated(self):
        item = parse_erc_item(_item("Symbol R? Vývod 1 [~, Pasivní, Čára]"))
        assert (item.reference, item.pin_number) == ("R?", "1")

    def test_symbol(self):
        item = parse_erc_item(_item("Symbol U3 [ABC-123]"))
        assert (item.reference, item.value, item.pin_number) == ("U3", "ABC-123", "")

    def test_label(self):
        item = parse_erc_item(_item("Global Label 'NET_A'"))
        assert (item.label, item.reference) == ("NET_A", "")

    def test_localized_pin_keeps_structure(self):
        item = parse_erc_item(_item("Symbol U1 Vývod 3 [SDA, Obousměrný, Čára]"))
        assert (item.reference, item.pin_number, item.pin_name) == ("U1", "3", "SDA")
        # The localized pin type is not given as an English type.
        assert item.pin_type == ""

    def test_position_with_the_board_scale(self):
        # KiCad 8, 9 and 10.0.0 to 10.0.6 write IU / 1e6 with schematic IU (100 nm).
        item = parse_erc_item(_item("Global Label 'X'", 0.508, -0.635), True)
        assert (item.x_mm, item.y_mm) == (50.8, -63.5)
        item = parse_erc_item(_item("X", 50.8, -63.5))
        assert (item.x_mm, item.y_mm) == (50.8, -63.5)


class TestErcReport:
    def test_reads_all_sheets(self):
        errors = parse_erc_report(_load("erc_kicad10_en.json"))
        assert len(errors) == 8
        assert {e.sheet for e in errors} == {"/", "/Power/"}

    def test_a_report_that_is_not_the_report_of_kicad(self):
        # KiCad 8, 9 and 10 write sheets[].violations (RC_JSON::ERC_REPORT). The reader
        # accepted a top-level list of violations, a form that KiCad does not write.
        data = _load("erc_kicad10_en.json")
        flat = {"violations": data["sheets"][0]["violations"]}
        with pytest.raises(InputError, match="the ERC report has no 'sheets'"):
            parse_erc_report(flat)
        del data["sheets"][0]["violations"][0]["severity"]
        with pytest.raises(
            InputError,
            match="violation 1 of 'violations' of sheet 1 of the ERC report has no 'severity'",
        ):
            parse_erc_report(data)
        drc = _load("drc_kicad10_en.json")
        drc["violations"][0]["items"][0]["pos"] = {"x": 1.0}
        with pytest.raises(
            InputError,
            match="'pos' of item 1 of violation 1 of 'violations' of the DRC report has no 'y'",
        ):
            parse_drc_report(drc)
        drc = _load("drc_kicad10_en.json")
        del drc["unconnected_items"]
        with pytest.raises(InputError, match="the DRC report has no 'unconnected_items'"):
            parse_drc_report(drc)

    def test_exclusion(self):
        errors = parse_erc_report(_load("erc_kicad10_en.json"))
        excluded = [e for e in errors if e.excluded]
        assert len(excluded) == 1
        assert excluded[0].comment == "Not used in this variant"
        assert excluded[0].severity == "error"

    def test_components_and_items(self):
        errors = parse_erc_report(_load("erc_kicad10_en.json"))
        pin_to_pin = next(e for e in errors if e.type == "pin_to_pin")
        assert pin_to_pin.components == ["U4", "U5"]
        assert [i.pin_number for i in pin_to_pin.items] == ["5", "2"]

    def test_localized_report(self):
        # KiCad 10.0.6: the positions get the board scale.
        errors = parse_erc_report(_load("erc_kicad10_cs.json"))
        assert [e.components for e in errors] == [["U1"], ["U3"], []]
        assert errors[0].items[0].pin_number == "3"
        assert errors[2].items[0].label == "NET_A"
        assert (errors[2].items[0].x_mm, errors[2].items[0].y_mm) == (50.8, 63.5)


class TestErcUnits:
    @pytest.mark.parametrize(
        "version, pcb_scale, rules",
        [
            ("8.0.0", True, "8"),
            ("8.0.9", True, "8"),
            ("9.0.0", True, "9.0.0"),
            ("9.0.3", True, "9.0.0"),
            ("9.0.4", True, "9.0.4"),
            ("9.0.9", True, "9.0.4"),
            ("9.0.9.1", True, "9.0.4"),
            ("10.0.0", True, "10.0.0"),
            ("10.0.6", True, "10.0.0"),
            # "Fix erc json report using wrong unit scale".
            ("10.0.7", False, None),
        ],
    )
    def test_known_versions(self, version, pcb_scale, rules):
        units = erc_report_units({"kicad_version": version, "coordinate_units": "mm"})
        assert units == ErcUnits(rules)
        assert units.pcb_scale == pcb_scale

    @pytest.mark.parametrize("version", ["10.0.8", "11.0.0", "", "x"])
    def test_versions_with_no_rules(self, version):
        units = erc_report_units({"kicad_version": version, "coordinate_units": "mm"})
        assert (units.pcb_scale, units.positions) == (False, False)
        assert "The server has no rules for the ERC JSON report of KiCad" in units.note

    def test_nightly_build(self):
        units = erc_report_units({"kicad_version": "10.99.0-123-gabc", "coordinate_units": "mm"})
        assert (units.pcb_scale, units.value_rules) == (False, None)
        assert "rule of branch master" in units.note

    def test_report_not_in_mm(self):
        units = erc_report_units({"kicad_version": "10.0.6", "coordinate_units": "in"})
        assert not units.positions
        report = {
            "kicad_version": "10.0.6",
            "coordinate_units": "in",
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {
                            "type": "x",
                            "description": "X",
                            "severity": "error",
                            "items": [{"uuid": "u", "description": "X", "pos": {"x": 1, "y": 2}}],
                        }
                    ],
                }
            ],
        }
        (error,) = parse_erc_report(report)
        assert (error.items[0].x_mm, error.items[0].y_mm) == (None, None)


# The item texts of kicad-cli 8.0.9, 9.0.9 and 10.0.6 for wires from (0, 0) to these
# ends in mm (spike/kicad-versions/erc_units_check.py): the JSON report (board scale)
# and the text report (schematic scale), with the decimal point of the locale.
MEASURED = [
    ("8", (10, 0), "0.1000", "10.0000"),
    ("8", (3, 4), "0.0500", "5.0000"),
    ("8", (2.54, 2.54), "0.0359", "3.5921"),
    ("8", (0, 0.0001), "0.0000", "0.0001"),
    ("8", (0.005, 0), "0.0001", "0.0050"),
    ("8", (0, 11.235), "0.1124", "11.2350"),
    ("9.0.4", (10, 0), "0.1000", "10.00"),
    ("9.0.4", (3, 4), "0.0500", "5.00"),
    ("9.0.4", (2.54, 2.54), "0.0359", "3.592"),
    ("9.0.4", (0, 0.0001), "0.0000", "0.00"),
    ("9.0.4", (0.005, 0), "0.0001", "0.005"),
    ("9.0.4", (0, 11.235), "0.1124", "11.235"),
    ("10.0.0", (10, 0), "0,1000", "10,00"),
    ("10.0.0", (12.7, 0), "0,1270", "12,70"),
    ("10.0.0", (0, 0.0001), "1,000e-06", "1,000e-04"),
    ("10.0.0", (2.54, 2.54), "0.0359", "3.592"),
    # 9.0.0 to 9.0.3: "%.2f" with no trim (from the source).
    ("9.0.0", (10, 0), "0.1000", "10.00"),
    ("9.0.0", (2.54, 2.54), "0.0359", "3.59"),
]


class TestLengthText:
    @pytest.mark.parametrize("rules, end, wrong, right", MEASURED)
    def test_measured_texts(self, rules, end, wrong, right):
        units = ErcUnits(rules)
        start, stop = (0, 0), (iu(end[0]), iu(end[1]))
        text = corrected_length_text(f"Wire, length {wrong} mm", start, stop, units)
        assert text == f"Wire, length {right} mm"

    def test_text_that_is_not_the_text_of_kicad(self):
        units = ErcUnits("10.0.0")
        end = (iu(10), 0)
        assert corrected_length_text("Wire, length 10.00 mm", (0, 0), end, units) is None
        assert corrected_length_text("Wire, length 0.1000 mm", (0, 0), end, ErcUnits()) is None


class TestReadErcReport:
    def test_lines_of_the_schematic(self, tmp_path):
        sch = tmp_path / "a.kicad_sch"
        sch.write_text(
            '(kicad_sch (version 20260306) (generator "eeschema") (uuid "a0")\n'
            '  (wire (pts (xy 10 10) (xy 20 10)) (uuid "w1"))\n'
            '  (bus (pts (xy 0 0) (xy 3 4)) (uuid "b1"))\n'
            '  (wire (pts (xy 0 0) (xy 1 0)) (uuid "dup"))\n'
            '  (wire (pts (xy 0 0) (xy 2 0)) (uuid "dup"))\n'
            # KiCad cannot read "+5": the line is not known.
            '  (wire (pts (xy +5 0) (xy 2 0)) (uuid "bad")))\n',
            encoding="utf-8",
        )

        def item(uuid, text, x, y):
            return {"uuid": uuid, "description": text, "pos": {"x": x, "y": y}}

        report = {
            "kicad_version": "10.0.6",
            "coordinate_units": "mm",
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {
                            "type": "wire_dangling",
                            "description": "Wire not connected",
                            "severity": "error",
                            "items": [
                                item("w1", "Horizontal Wire, length 0.1000 mm", 0.1, 0.1),
                                item("b1", "Bus, length 0.0500 mm", 0.0, 0.0),
                                item("dup", "Horizontal Wire, length 0.0100 mm", 0.0, 0.0),
                                item("other", "Horizontal Wire, length 0.0100 mm", 0.0, 0.0),
                                item("bad", "Horizontal Wire, length 0.0100 mm", 0.0, 0.0),
                                item("l1", "Global Label 'NET_A'", 0.508, 0.635),
                            ],
                        }
                    ],
                }
            ],
        }
        result = read_erc_report(report, [sch])
        items = result.errors[0].items
        assert [i.description for i in items] == [
            "Horizontal Wire, length 10.00 mm",
            "Bus, length 5.00 mm",
            # Two lines with one UUID: the line is not known.
            "Horizontal Wire, length 0.0100 mm",
            # Not a line of the files, or a line that KiCad cannot read: the text stays.
            "Horizontal Wire, length 0.0100 mm",
            "Horizontal Wire, length 0.0100 mm",
            "Global Label 'NET_A'",
        ]
        assert (items[0].x_mm, items[0].y_mm) == (10.0, 10.0)
        assert (items[-1].x_mm, items[-1].y_mm) == (50.8, 63.5)
        assert result.notes == [
            "KiCad 10.0.6 writes the lengths of the item texts 100 times too small: the "
            "server could not correct 3 item text(s) (not a wire, a bus or a graphic line of "
            "the schematic files, two lines with one UUID, or a text that is not the text of "
            "KiCad). Their lengths stay 100 times too small."
        ]
        assert result.units == ErcUnits("10.0.0")

    def test_corrected_version(self):
        report = _load("erc_kicad10_en.json")
        report["kicad_version"] = "10.0.7"
        positions = [(i.x_mm, i.y_mm) for e in read_erc_report(report).errors for i in e.items]
        raw = [
            (float(i["pos"]["x"]), float(i["pos"]["y"]))
            for s in report["sheets"]
            for v in s["violations"]
            for i in v["items"]
        ]
        assert positions == raw


class TestDrcReport:
    def test_reads_all_lists(self):
        errors = parse_drc_report(_load("drc_kicad10_en.json"))
        assert len(errors) == 6
        assert [e.category for e in errors].count("unconnected") == 1
        assert [e.category for e in errors].count("schematic_parity") == 1

    def test_references(self):
        errors = {e.type: e for e in parse_drc_report(_load("drc_kicad10_en.json"))}
        assert errors["unconnected_items"].components == ["U1", "J1"]
        assert errors["shorting_items"].components == ["U1"]
        assert errors["courtyards_overlap"].components == ["L4", "C18"]
        # A net name with brackets and " of " is not read as a reference.
        assert errors["clearance"].components == ["L6"]
        assert errors["text_height"].components == ["L4"]

    def test_location_and_exclusion(self):
        errors = {e.type: e for e in parse_drc_report(_load("drc_kicad10_en.json"))}
        assert errors["shorting_items"].location == (97.18, 65.3)
        assert errors["text_height"].excluded
        assert errors["text_height"].comment == "Accepted for the prototype"


class TestValidationFormatting:
    def test_erc_row_has_pin_data(self):
        errors = parse_erc_report(_load("erc_kicad10_en.json"))
        row = validation._erc_row(errors[0])
        assert "U1.3 SDA (Bidirectional)" in row
        assert "(101.60, 76.20)" in row

    def test_excluded_is_marked_and_not_counted(self):
        errors = parse_erc_report(_load("erc_kicad10_en.json"))
        excluded = next(e for e in errors if e.excluded)
        assert "(excluded)" in validation._erc_row(excluded)
        counts = validation._count_text(errors)
        assert "- Errors: 5" in counts
        assert "- Excluded: 1" in counts

    def test_markdown_cell_is_escaped(self):
        assert validation._md("A|B\nC") == "A\\|B C"

    def test_report_path_is_keyed_by_path(self, tmp_path):
        a = validation._report_path(tmp_path / "a" / "main.kicad_sch", "erc")
        b = validation._report_path(tmp_path / "b" / "main.kicad_sch", "erc")
        assert a != b
        assert a.name.startswith("main_") and a.name.endswith("_erc.json")

    def test_stale_report(self, tmp_path):
        src = tmp_path / "main.kicad_sch"
        report = tmp_path / "report.json"
        assert validation._report_is_stale(report, [src])
        report.write_text("{}")
        src.write_text("")
        src_mtime = report.stat().st_mtime + 10
        os.utime(src, (src_mtime, src_mtime))
        assert validation._report_is_stale(report, [src])
        os.utime(src, (src_mtime - 20, src_mtime - 20))
        assert not validation._report_is_stale(report, [src])


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli is not installed")
class TestKicadCliIntegration:
    async def test_run_erc_gives_label_position_in_mm(self, tmp_path):
        sch = tmp_path / "dangling_label.kicad_sch"
        shutil.copy(LABEL_SCH, sch)
        out = await validation.run_erc(str(sch))
        assert "label_dangling" in out
        # kicad-cli localizes the item text ("Global Label" on English
        # systems, e.g. "全局标签" on Chinese Windows — LANG overrides have
        # no effect there), so only the quoted label text is stable
        assert "'NET_A'" in out
        assert "(50.80, 63.50)" in out

    async def test_no_annotation_check_gives_no_pass(self, tmp_path, monkeypatch):
        """With no annotation check (a nightly build), the tool does not give "✅"."""
        sch = tmp_path / "empty.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))\n', "utf-8")

        async def nightly():
            return "10.99.0"

        monkeypatch.setattr(validation, "get_kicad_version", nightly)
        out = await validation.run_erc(str(sch))
        assert out.startswith("⚠️ **ERC: no violation in the report of kicad-cli**")
        assert "no verified rules of KiCad 10.99.0 for the load of a schematic" in out
        out = await validation.get_erc_violations(str(sch))
        assert out.startswith("⚠️ **No violation in the report of kicad-cli**")

    async def test_symbols_that_are_not_annotated(self, tmp_path):
        """The ERC dialog gives an error only for the first symbol that is not annotated,
        and the info bar. The tool gives the two, and lists each symbol."""
        uuid = "11111111-1111-1111-1111-111111111111"
        symbols = "".join(
            f'  (symbol (lib_id "Device:R") (at {x} 50.8 0) (unit 1)\n'
            f'    (uuid "aaaaaaaa-0000-0000-0000-00000000000{n}")\n'
            '    (property "Reference" "R?" (at 0 0 0)) (property "Value" "1k" (at 0 0 0))\n'
            f'    (instances (project "r" (path "/{uuid}" (reference "R?") (unit 1)))))\n'
            for n, x in ((1, 50.8), (2, 76.2))
        )
        sch = tmp_path / "r.kicad_sch"
        sch.write_text(
            f'(kicad_sch (version 20231120) (generator "eeschema") (uuid "{uuid}")\n'
            '  (lib_symbols (symbol "Device:R" (symbol "R_1_1")))\n'
            f"{symbols})\n",
            encoding="utf-8",
        )
        out = await validation.run_erc(str(sch))
        assert "| ❌ error | unannotated | Item not annotated: R? |" in out
        assert (
            "⚠️ **KiCad**: Schematic is not fully annotated. ERC results will be incomplete." in out
        )
        assert "## Symbols that are not annotated (2)" in out
        assert "| R? | - | 1k | / | (50.80, 50.80) |" in out
        assert "| R? | - | 1k | / | (76.20, 50.80) |" in out
        out = await validation.get_erc_violations(str(sch), "warning")
        assert "## Symbols that are not annotated (2)" in out and "✅" not in out

    async def test_get_erc_violations_gives_table_not_run_summary(self, tmp_path):
        sch = tmp_path / "dangling_label.kicad_sch"
        shutil.copy(LABEL_SCH, sch)
        out = await validation.get_erc_violations(str(sch))
        assert out.startswith("# ERC Violations")
        assert "**Count:** 1" in out

    @pytest.mark.parametrize("check", ["erc", "drc"])
    async def test_settings_that_cannot_be_copied(self, tmp_path, monkeypatch, check):
        """An expected error of an English kicad-cli gives "❌ <message>", no traceback."""

        async def failing(*args, **kwargs):
            raise KicadCliError("The settings of KiCad cannot be copied for kicad-cli: x")

        monkeypatch.setattr(validation, "run_kicad_cli", failing)
        if check == "erc":
            sch = tmp_path / "dangling_label.kicad_sch"
            shutil.copy(LABEL_SCH, sch)
            out = await validation.run_erc(str(sch))
        else:
            pcb = tmp_path / "board.kicad_pcb"
            pcb.write_text(
                '(kicad_pcb (version 20240108) (generator "pcbnew"))\n', encoding="utf-8"
            )
            out = await validation.run_drc(str(pcb))
        assert out == "❌ The settings of KiCad cannot be copied for kicad-cli: x"

    @pytest.mark.parametrize("check", ["erc", "drc"])
    async def test_kicad_cli_that_writes_no_report(self, tmp_path, monkeypatch, check):
        """kicad-cli stops with 0 but writes no report: an error, with its output."""

        async def no_report(args, *rest, **kwargs):
            return subprocess.CompletedProcess(args, 0, b"", b"Loading...")

        monkeypatch.setattr(validation, "run_kicad_cli", no_report)
        if check == "erc":
            sch = tmp_path / "dangling_label.kicad_sch"
            shutil.copy(LABEL_SCH, sch)
            out = await validation.run_erc(str(sch))
        else:
            pcb = tmp_path / "board.kicad_pcb"
            pcb.write_text(
                '(kicad_pcb (version 20240108) (generator "pcbnew"))\n', encoding="utf-8"
            )
            out = await validation.run_drc(str(pcb))
        assert out.startswith(f"❌ **{check.upper()} Report Not Generated**"), out
        assert "kicad-cli stopped with the exit code 0, but it wrote no report file" in out
        assert "```\nLoading...\n```" in out

    async def test_export_erc_report_with_violations(self, tmp_path):
        sch = tmp_path / "dangling_label.kicad_sch"
        shutil.copy(LABEL_SCH, sch)
        out = await validation.export_erc_report(str(sch))
        assert "Exported" in out
        exported = tmp_path / "dangling_label_erc_report.json"
        assert json.loads(exported.read_text(encoding="utf-8"))["sheets"]

    @pytest.mark.parametrize("utf8_mode", [False, True])
    def test_run_erc_in_a_locale_that_is_not_utf8(self, tmp_path, utf8_mode):
        # The server runs in a locale that is not UTF-8, and the project path
        # holds characters of that locale. kicad-cli must load the file and
        # write English text.
        env, encoding = non_utf8_env()
        names = [
            name for name in ["电源板", "لوحة", "řídicí"] if can_name(name, encoding, utf8_mode)
        ]
        assert names, encoding
        code = (
            "import asyncio, shutil\n"
            "from pathlib import Path\n"
            "from kicad_workbench.tools import validation\n"
            "directory = Path(ARGS[0]) / ARGS[1]\n"
            "directory.mkdir()\n"
            "sch = directory / (ARGS[1] + '.kicad_sch')\n"
            "shutil.copy(ARGS[2], sch)\n"
            "result(asyncio.run(validation.run_erc(str(sch))))\n"
        )
        out = run_python(code, env, utf8_mode, str(tmp_path), names[0], str(LABEL_SCH))
        assert "label_dangling" in out, out
        assert "'NET_A'" in out
        if sys.platform.startswith("linux"):
            assert "Global Label 'NET_A'" in out


@pytest.mark.parametrize(
    "data, text",
    [
        (b'{"sheets": [\xff]}', "is not UTF-8 JSON"),
        (b"<report/>", "is not UTF-8 JSON"),  # no XML form: kicad-cli writes JSON only
        (b"[]", "is not a JSON object"),
    ],
)
def test_a_report_file_that_is_not_json(tmp_path, data, text):
    # The reader replaced the bytes that are not UTF-8, tried XML, and gave a reader
    # error as a violation "parse_error" of the design.
    path = tmp_path / "erc.json"
    path.write_bytes(data)
    for read, kind in (
        (validation._parse_erc_report, "ERC"),
        (validation._parse_drc_report, "DRC"),
    ):
        with pytest.raises(InputError, match=f"The {kind} report .* {text}"):
            read(path)
