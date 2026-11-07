"""Tests of the parser of one schematic file of a hierarchy, and of merge_units.

The components of a full hierarchy come from schematic.placed_components (the
references of each sheet path), not from the parser of one file.
"""

from pathlib import Path

from kicad_workbench.parsers.schematic_parser import (
    SchematicComponent,
    SchematicParser,
    merge_units,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "hier"


def make_root_parser() -> SchematicParser:
    return SchematicParser(str(FIXTURES / "root.kicad_sch"))


class TestHierarchicalParsing:
    def test_flat_root_returns_only_local_components(self):
        refs = [c.reference for c in make_root_parser().get_components()]
        assert refs == ["R1"]

    def test_subsheet_component_metadata_is_preserved(self):
        comps = {
            c.reference: c
            for c in SchematicParser(str(FIXTURES / "child.kicad_sch")).get_components()
        }
        assert comps["C9"].value == "100nF"
        assert comps["C9"].footprint == "Capacitor_SMD:C_0603_1608Metric"

    def test_get_sheets_exposes_hierarchy(self):
        sheets = {s["name"]: s["file"] for s in make_root_parser().get_sheets()}
        assert sheets["Power"] == "child.kicad_sch"
        assert sheets["Ghost"] == "missing.kicad_sch"


class TestMergeDuplicateReferences:
    def _comp(self, ref: str, pins: list[dict]) -> SchematicComponent:
        return SchematicComponent(
            reference=ref, value="V", library_id="Lib:X", pins=list(pins)
        )

    def test_multi_unit_instances_merge_with_pin_union(self):
        u1a = self._comp("U1", [{"number": "1", "name": "VDD"}])
        u1b = self._comp("U1", [{"number": "2", "name": "GND"}, {"number": "1", "name": "VDD"}])
        merged = merge_units([u1a, u1b])
        assert len(merged) == 1
        numbers = [p["number"] for p in merged[0].pins]
        assert numbers == ["1", "2"]  # union, no duplicates

    def test_pins_with_one_number_and_other_names_stay(self):
        # Unit A and unit B have a pin "5" with an other name: a pin number on two
        # nets (BaseNames needs the two library pins).
        u1a = self._comp("U1", [{"number": "5", "name": "PA0", "base_name": "PA0"}])
        u1b = self._comp("U1", [{"number": "5", "name": "PB1", "base_name": "PB1"}])
        pins = merge_units([u1a, u1b])[0].pins
        assert [p["name"] for p in pins] == ["PA0", "PB1"]

    def test_distinct_references_preserved(self):
        comps = [self._comp("R1", []), self._comp("C9", [])]
        assert len(merge_units(comps)) == 2

    def test_empty_references_are_not_collapsed(self):
        comps = [self._comp("", []), self._comp("", [])]
        assert len(merge_units(comps)) == 2
