"""Tests for the compare of the fields of the units of one package (parsers/unit_fields.py)."""

from pathlib import Path

from kicad_workbench.parsers.unit_fields import (
    is_confirmed_field,
    is_mpn_field,
    unit_conflicts,
    variant_unit_conflicts,
)

VERSION = "20260306"  # KiCad 10: the variants


def symbol(reference, unit, path, value="LM324", mpn="LM324DR", mpn_name="MPN", variants=""):
    mpn_text = f'(property "{mpn_name}" "{mpn}" (at 0 0 0))' if mpn is not None else ""
    return (
        f'  (symbol (lib_id "Amplifier_Operational:LM324") (at 0 0 0) (unit {unit})\n'
        f'    (property "Reference" "{reference}" (at 0 0 0))\n'
        f'    (property "Value" "{value}" (at 0 0 0))\n'
        f"    {mpn_text}\n"
        f'    (instances (project "board" (path "{path}" (reference "{reference}") (unit {unit})'
        f" {variants}))))\n"
    )


def write_project(
    directory: Path, root_symbols: str, sub_symbols: str = "", version=VERSION
) -> Path:
    sheets = ""
    if sub_symbols:
        sheets = "".join(
            f'  (sheet (at 1 1) (size 2 2) (uuid "{uuid}")\n'
            f'    (property "Sheetname" "{name}" (at 0 0 0))\n'
            f'    (property "Sheetfile" "sub.kicad_sch" (at 0 0 0)))\n'
            for uuid, name in (("b1", "A"), ("b2", "B"))
        )
        (directory / "sub.kicad_sch").write_text(
            f'(kicad_sch (version {VERSION}) (generator "eeschema") (uuid "c0")\n{sub_symbols})\n',
            encoding="utf-8",
        )
    (directory / "board.kicad_sch").write_text(
        f'(kicad_sch (version {version}) (generator "eeschema") (uuid "a0")\n'
        f"{root_symbols}{sheets})\n",
        encoding="utf-8",
    )
    project = directory / "board.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    return project


def texts(project: Path, variants=None) -> list[str]:
    conflicts, problems = unit_conflicts(project, "10.0.6", variants)
    assert problems == []
    return [c.text() for c in conflicts]


class TestUnitConflicts:
    def test_four_units_with_one_other_mpn(self, tmp_path):
        symbols = "".join(symbol("U1", unit, "/a0") for unit in (1, 2, 3))
        symbols += symbol("U1", 4, "/a0", mpn="LM324N")
        assert texts(write_project(tmp_path, symbols)) == [
            "U1: MPN: A = 'LM324DR', B = 'LM324DR', C = 'LM324DR', D = 'LM324N'"
        ]

    def test_same_fields_give_nothing(self, tmp_path):
        symbols = "".join(symbol("U1", unit, "/a0") for unit in (1, 2, 3, 4))
        assert texts(write_project(tmp_path, symbols)) == []

    def test_value_and_empty_mpn(self, tmp_path):
        # An empty field and a missing field are differences too (user, 2026-11-05).
        symbols = symbol("U3", 1, "/a0", value="LM358 (A)", mpn="A")
        symbols += symbol("U3", 2, "/a0", value="LM358", mpn=None)
        symbols += symbol("U4", 1, "/a0", mpn="X ") + symbol("U4", 2, "/a0", mpn="X")
        assert texts(write_project(tmp_path, symbols)) == [
            "U3: Value: A = 'LM358 (A)', B = 'LM358'",
            "U3: MPN: A = 'A', B = ''",
            "U4: MPN: A = 'X ', B = 'X'",  # KiCad compares the text as it is
        ]

    def test_tilde_by_file_version(self, tmp_path):
        # "~" is an empty field only in a file older than EMPTY_TEXT_MARKER (parseSchField).
        symbols = symbol("U4", 1, "/a0", mpn="~") + symbol("U4", 2, "/a0", mpn=None)
        assert texts(write_project(tmp_path, symbols, version="20250114")) == []
        assert texts(write_project(tmp_path, symbols)) == ["U4: MPN: A = '~', B = ''"]

    def test_duplicate_reference_is_not_compared(self, tmp_path):
        symbols = symbol("R1", 1, "/a0", value="10k") + symbol("R1", 1, "/a0", value="1k")
        conflicts, problems = unit_conflicts(write_project(tmp_path, symbols), "10.0.6")
        assert conflicts == [] and problems[0].startswith("R1: a duplicate reference")

    def test_reference_case_and_reference_field(self, tmp_path):
        # KiCad groups "U1" and "u1" (CompareRef); a symbol with no instance for the
        # sheet path uses its Reference field (SCH_SYMBOL::GetRef).
        symbols = symbol("U1", 1, "/a0", mpn="A") + symbol("u1", 2, "/other", mpn="B")
        assert texts(write_project(tmp_path, symbols)) == ["U1: MPN: A = 'A', B = 'B'"]

    def test_other_mpn_field_names(self, tmp_path):
        assert is_mpn_field("Manufacturer_Part_Number") and is_mpn_field("mfr pn")
        assert not is_mpn_field("Part Number")
        symbols = symbol("U1", 1, "/a0", mpn_name="Manufacturer Part Number", mpn="A")
        symbols += symbol("U1", 2, "/a0", mpn_name="Manufacturer Part Number", mpn="B")
        assert texts(write_project(tmp_path, symbols)) == [
            "U1: Manufacturer Part Number: A = 'A', B = 'B'"
        ]

    def test_not_annotated_and_power_symbols(self, tmp_path):
        # "U" with no number is not annotated too: the BOM gives "U?" lines.
        symbols = symbol("U?", 1, "/a0", mpn="A") + symbol("U?", 2, "/a0", mpn="B")
        symbols += symbol("U", 1, "/a0", mpn="A") + symbol("U", 2, "/a0", mpn="B")
        symbols += symbol("#PWR01", 1, "/a0", mpn="A") + symbol("#PWR01", 2, "/a0", mpn="B")
        assert texts(write_project(tmp_path, symbols)) == []

    def test_shared_sheet_gives_each_reference(self, tmp_path):
        # The sheet sub.kicad_sch is used two times: its units get U5 and U6.
        def unit(number, mpn):
            return symbol("U5", number, "/a0/b1", mpn=mpn).replace(
                '(reference "U5")',
                f'(reference "U5") (unit {number})) (path "/a0/b2" (reference "U6")',
                1,
            )

        sub = unit(1, "P1") + unit(2, "P2")
        assert texts(write_project(tmp_path, "", sub)) == [
            "U5: MPN: A = 'P1', B = 'P2'",
            "U6: MPN: A = 'P1', B = 'P2'",
        ]

    def test_value_confirmed(self, tmp_path):
        """Value_Confirmed is the confirmed copy of Value: the BOM line joins it too."""
        assert is_confirmed_field("value confirmed") and not is_confirmed_field("Value")
        symbols = symbol("U1", 1, "/a0", mpn_name="Value_Confirmed", mpn="LM324")
        symbols += symbol("U1", 2, "/a0", mpn_name="Value_Confirmed", mpn="LM324 (LM324DR)")
        assert texts(write_project(tmp_path, symbols)) == [
            "U1: Value_Confirmed: A = 'LM324', B = 'LM324 (LM324DR)'"
        ]

    def test_variant_that_repairs_a_conflict(self, tmp_path):
        """variant_unit_conflicts gives the full set of each variant: a variant can
        make the fields of the units equal."""
        lite = '(variant (name "Lite") (field (name "MPN") (value "B")))'
        symbols = symbol("U2", 1, "/a0", mpn="A", variants=lite) + symbol("U2", 2, "/a0", mpn="B")
        check = variant_unit_conflicts(write_project(tmp_path, symbols), "10.0.6", ["Lite"])
        assert (check.hierarchy, check.duplicates, check.conflicts["Lite"]) == ([], [], [])
        assert [c.text() for c in check.conflicts[""]] == ["U2: MPN: A = 'A', B = 'B'"]

    def test_variant(self, tmp_path):
        lite = '(variant (name "Lite") (field (name "MPN") (value "LM324N")))'
        # KiCad finds the field of a variant by its exact name: "value" is no Value.
        lite += '(variant (name "Lite") (field (name "value") (value "X")))'
        symbols = symbol("U2", 1, "/a0") + symbol("U2", 2, "/a0", variants=lite)
        project = write_project(tmp_path, symbols)
        assert texts(project) == []
        assert texts(project, ["Lite", "Pro"]) == [
            "U2 (variant 'Lite'): MPN: A = 'LM324DR', B = 'LM324N'"
        ]
        # A variant gives only the conflicts that the default variant does not give.
        symbols = symbol("U2", 1, "/a0", mpn="A") + symbol("U2", 2, "/a0", mpn="B")
        assert texts(write_project(tmp_path, symbols), ["Lite"]) == ["U2: MPN: A = 'A', B = 'B'"]
