"""Tests for the tool assign_footprint (tools/footprint_assignment.py), with the rules of
CvPcb of KiCad 10.0.6. spike/kicad-versions/assign_footprint_check.py compares the
tool with kicad-cli 10.0.6 and pcbnew."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kicad_workbench.parsers.board_netlist import CvpcbPart, read_cvpcb_parts
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import footprint_assignment, symbol_fields
from kicad_workbench.tools.footprint_assignment import FootprintAssignment, assign_footprint
from kicad_workbench.utils.footprint_filter import filter_matches, footprint_id_problem
from kicad_workbench.utils.library_footprint import FootprintItems, PadNumber
from kicad_workbench.utils.library_lookup import LibraryLookupError

from .test_symbol_fields import write_project

HIDDEN = "\t\t\t(hide yes)\n"


def symbol(reference, unit, footprint="", hide=HIDDEN, paths=("/a0",), variants=""):
    """A placed unit of LM358 with a Footprint field."""
    instances = ""
    for path in paths:
        path, ref = path if isinstance(path, tuple) else (path, reference)
        instances += (
            f'\t\t\t\t(path "{path}"\n\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit {unit})\n'
            f"{variants}\t\t\t\t)\n"
        )
    return (
        f'\t(symbol\n\t\t(lib_id "A:LM358")\n\t\t(at 100 50 0)\n\t\t(unit {unit})\n'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at 100 45 90)\n\t\t)\n'
        f'\t\t(property "Value" "LM358"\n\t\t\t(at 100 55 0)\n\t\t)\n'
        f'\t\t(property "Footprint" "{footprint}"\n\t\t\t(at 100 50 0)\n{hide}'
        "\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n\t\t)\n"
        f'\t\t(instances\n\t\t\t(project "board"\n{instances}\t\t\t)\n\t\t)\n\t)\n'
    )


def netlist(*components, filters=("SOIC*",), pins=8):
    """A netlist of kicad-cli (kicadsexpr) with one libpart A:LM358."""
    comps = "".join(
        f'(comp (ref "{ref}") (value "LM358") (libsource (lib "A") (part "LM358") '
        '(description "")))'
        for ref in components
    )
    fps = "".join(f'(fp "{f}")' for f in filters)
    pin_items = "".join(f'(pin (num "{n}") (name "") (type "passive"))' for n in range(pins))
    return (
        f'(export (version "E") (components {comps}) (libparts (libpart (lib "A") '
        f'(part "LM358") (footprints {fps}) (pins {pin_items}))))'
    )


@pytest.fixture(autouse=True)
def kicad(monkeypatch, tmp_path):
    """KiCad 10.0.6, the libraries "P" (SOIC-8 with 8 pads, DIP-8 with 9 pads), and the
    netlist of U1, U5 and U6."""

    async def version():
        return "10.0.6"

    monkeypatch.setattr(symbol_fields, "get_kicad_version", version)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "kicad-config"))
    pads = {"SOIC-8": 8, "DIP-8": 9}
    state = {"netlist": netlist("U1", "U5", "U6")}

    def find(library, name, kicad, project, rows):
        if library != "P" or name not in pads:
            raise LibraryLookupError(f'the library "{library}" has no footprint "{name}".')
        return (type("Row", (), {"path": tmp_path / "P.pretty"})(), [], None)

    async def read(items, embedded=False):
        found = [
            FootprintItems((PadNumber("1", True),), pads[name], (), None) for _lib, name in items
        ]
        return "10.0.6", found

    monkeypatch.setattr(footprint_assignment, "checked_footprint_file", find)
    monkeypatch.setattr(footprint_assignment, "library_rows", lambda kind, kicad, project: ([], []))
    monkeypatch.setattr(footprint_assignment, "read_footprints", read)
    monkeypatch.setattr(footprint_assignment, "netlist_root", lambda project, kicad: (project, 1))
    monkeypatch.setattr(
        footprint_assignment, "board_netlist_text", lambda root: (state["netlist"], "")
    )
    return state


async def assign(path: Path, **assignments) -> str:
    items = [FootprintAssignment(reference=r, footprint=f) for r, f in assignments.items()]
    return await assign_footprint(str(path), items)


def footprints(path):
    """(reference, unit) -> (the Footprint text, hidden)."""
    found = {}
    for node in parse(path.read_text("utf-8")).children("symbol"):
        props = {p.value(0): p for p in node.children("property")}
        field = props["Footprint"]
        hide = field.first("hide")
        found[(props["Reference"].value(1), node.first("unit").value(0))] = (
            field.value(1),
            hide is not None and hide.value(0) == "yes",
        )
    return found


class TestAssignFootprint:
    async def test_each_unit_and_the_checks(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1, "P:OLD") + symbol("U1", 2, "P:OLD"))
        out = await assign(path, U1="P:SOIC-8")
        assert out.startswith("# Footprints of the symbols of board.kicad_pro"), out
        assert "✅ The tool wrote 2 Footprint field values" in out, out
        assert "| U1 | P:SOIC-8 | SOIC* | yes | 8 | 8 |" in out, out
        assert "⚠️" not in out
        assert footprints(path) == {
            ("U1", "1"): ("P:SOIC-8", True),
            ("U1", "2"): ("P:SOIC-8", True),
        }
        again = await assign(path, U1="P:SOIC-8")
        assert "have these values already" in again

    async def test_the_warnings_of_the_filters_and_the_pin_count(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1))
        out = await assign(path, U1="P:DIP-8")
        assert "| U1 | P:DIP-8 | SOIC* | no | 8 | 9 |" in out, out
        assert "⚠️ The footprint filters of U1 (SOIC*) do not match" in out
        assert '⚠️ U1 has 8 pins, and "P:DIP-8" has 9 pads' in out
        assert footprints(path)[("U1", "1")][0] == "P:DIP-8"

    async def test_an_empty_visible_field_becomes_hidden(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1, "", hide="") + symbol("U1", 2, "", hide=""))
        out = await assign(path, U1="P:SOIC-8")
        assert "| U1 | A | - | Footprint field visible | yes | no |" in out, out
        text = path.read_text("utf-8")
        # SCH_IO_KICAD_SEXPR::saveField writes (hide yes) after the (at ...).
        assert '(property "Footprint" "P:SOIC-8"\n\t\t\t(at 100 50 0)\n\t\t\t(hide yes)\n' in text
        # A visible field with a footprint stays visible.
        path = write_project(tmp_path, symbol("U1", 1, "P:OLD", hide=""))
        out = await assign(path, U1="P:SOIC-8")
        assert "✅ The tool wrote 1 Footprint field value into `board.kicad_sch`." in out, out
        assert footprints(path)[("U1", "1")] == ("P:SOIC-8", False)

    async def test_a_hide_no_becomes_yes(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1, "", hide="\t\t\t(hide no)\n"))
        await assign(path, U1="P:SOIC-8")
        assert footprints(path)[("U1", "1")] == ("P:SOIC-8", True)
        assert "(hide no)" not in path.read_text("utf-8")

    async def test_the_hide_with_no_change_of_the_footprint(self, tmp_path):
        # AssignFootprints hides the field with no commit, and the save of CvPcb writes
        # it: SaveProject of KiCad 10 never skips a save with no changes (measured).
        path = write_project(tmp_path, symbol("U1", 1, "", hide="") + symbol("U1", 2, "", hide=""))
        out = await assign(path, U1="")
        assert "✅ The tool wrote 2 Footprint field values" in out, out
        assert "| U1 | A | - | Footprint field visible | yes | no |" in out, out
        assert footprints(path)[("U1", "1")] == ("", True)
        again = await assign(path, U1="")
        assert "have these values already" in again

    async def test_a_shared_sheet(self, tmp_path):
        sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
        path = write_project(tmp_path, "", sub)
        out = await assign(path, U5="P:SOIC-8")
        assert out.startswith("❌ The symbol of U5 is in a sheet") and "footprint" in out, out
        out = await assign(path, U5="P:SOIC-8", U6="P:SOIC-8")
        assert out.startswith("# Footprints"), out

    async def test_a_variant_of_the_other_reference_of_a_shared_sheet(self, tmp_path):
        lite = '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Lite")\n\t\t\t\t\t\t(field\n(name "Footprint")\n(value "P:X")\n)\n\t\t\t\t\t)\n'
        instances = (
            '\t\t\t\t(path "/a0/b1"\n\t\t\t\t\t(reference "U5")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n'
            '\t\t\t\t(path "/a0/b2"\n\t\t\t\t\t(reference "U6")\n\t\t\t\t\t(unit 1)\n'
            f"{lite}\t\t\t\t)\n"
        )
        sub = symbol("U5", 1, paths=()).replace(
            '(project "board"\n', '(project "board"\n' + instances
        )
        path = write_project(tmp_path, "", sub)
        out = await assign(path, U5="P:SOIC-8", U6="P:SOIC-8")
        assert "⚠️ The variants 'Lite' of U6 have a Footprint of their own" in out, out

    async def test_the_case_of_a_reference(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1))
        out = await assign(path, u1="P:SOIC-8")
        assert out.startswith("❌ Give the reference 'U1', not 'u1'"), out

    async def test_a_variant_with_a_footprint_of_its_own(self, tmp_path):
        lite = '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Lite")\n\t\t\t\t\t\t(field\n(name "Footprint")\n(value "P:X")\n)\n\t\t\t\t\t)\n'
        path = write_project(tmp_path, symbol("U1", 1, variants=lite))
        out = await assign(path, U1="P:SOIC-8")
        assert "⚠️ The variants 'Lite' of U1 have a Footprint of their own" in out, out
        assert '(value "P:X")' in path.read_text("utf-8")

    async def test_errors(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1) + symbol("U2", 1, "${FP}"))
        before = path.read_bytes()
        assert 'KiCad does not find the footprint "P:NONE"' in await assign(path, U1="P:NONE")
        assert "has no library nickname" in await assign(path, U1="SOIC-8")
        assert "is not annotated" in await assign(path, **{"U?": "P:SOIC-8"})
        assert "power symbol" in await assign(path, **{"#PWR01": "P:SOIC-8"})
        assert "has a text variable" in await assign(path, U2="P:SOIC-8")
        # U2 is not in the netlist of CvPcb (excluded from the board).
        out = await assign(path, U1="P:SOIC-8", U2="")
        assert "has a text variable" in out, out
        assert path.read_bytes() == before

    async def test_a_reference_that_the_netlist_does_not_have(self, tmp_path, kicad):
        kicad["netlist"] = netlist("U5")
        path = write_project(tmp_path, symbol("U1", 1))
        out = await assign(path, U1="P:SOIC-8")
        assert out.startswith("❌ The netlist of CvPcb has no component U1"), out

    async def test_an_unknown_key(self):
        with pytest.raises(ValidationError):
            FootprintAssignment.model_validate({"reference": "U1", "footprint": "P:X", "unit": 1})


class TestRules:
    def test_filters(self):
        # Anchored wildcards with no difference of case; ":" compares the library too.
        assert filter_matches((), "P", "anything")
        assert filter_matches(("SOIC*",), "P", "soic-8_3.9x4.9mm")
        assert not filter_matches(("SOIC*",), "P", "XSOIC-8")
        assert filter_matches(("C_????",), "P", "C_0603")
        assert not filter_matches(("C_????",), "P", "C_06031")
        assert filter_matches(("p:R*",), "P", "R_1")
        assert not filter_matches(("Q:R*",), "P", "R_1")
        # The regular expression characters are literal.
        assert filter_matches(("R(1).x",), "P", "r(1).x")
        assert not filter_matches(("R.x",), "P", "Rax")

    def test_footprint_ids(self):
        assert footprint_id_problem("P:SOIC-8") is None
        assert footprint_id_problem("P:a:b") is not None  # ":" in the name
        assert footprint_id_problem("P:a<b") is not None
        assert footprint_id_problem(":a") is not None
        assert footprint_id_problem("P:") is not None

    def test_cvpcb_parts(self):
        text = (
            '(export (components (comp (ref "U1") (libsource (lib "A") (part "B"))) '
            '(comp (ref "U2") (libsource (lib "A") (part "C"))) (comp (ref "U3"))) '
            '(libparts (libpart (lib "A") (part "B") (aliases (alias "C")) '
            '(footprints (fp "X*") (fp)) (pins (pin (num "1")) (pin (num "2"))) '
            '(pins (pin (num "3"))))))'
        )
        parts = read_cvpcb_parts(text)
        assert parts == {
            "U1": CvpcbPart(3, ("X*",)),
            "U2": CvpcbPart(3, ("X*",)),  # the alias
            "U3": CvpcbPart(0, ()),  # no libpart
        }
