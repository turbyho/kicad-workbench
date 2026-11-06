"""Tests for the tool set_symbol_fields (tools/symbol_fields.py).

spike/kicad-versions/fields_check.py compares the writes with KiCad 8.0.9, 9.0.9
and 10.0.6.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from kicad_workbench.parsers.unit_fields import unit_conflicts
from kicad_workbench.tools import symbol_fields
from kicad_workbench.tools.symbol_fields import FieldChange, set_symbol_fields
from kicad_workbench.utils.kicad_lock import lock_file_path

KICAD10, KICAD9 = "20260306", "20250114"
LIB = (
    '\t(lib_symbols\n\t\t(symbol "A:LM358"\n\t\t\t(symbol "LM358_1_1")\n'
    '\t\t\t(symbol "LM358_2_1")\n\t\t)\n\t)\n'
)


def symbol(reference, unit, paths=("/a0",), mpn="A", variants=""):
    """A placed unit of LM358 with its instances: (path, reference) or a path."""
    instances = ""
    for path in paths:
        path, ref = path if isinstance(path, tuple) else (path, reference)
        instances += (
            f'\t\t\t\t(path "{path}"\n\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit {unit})\n'
            f"{variants}\t\t\t\t)\n"
        )
    mpn_text = f'\t\t(property "MPN" "{mpn}"\n\t\t\t(at 100 50 0)\n\t\t\t(hide yes)\n\t\t)\n'
    return (
        f'\t(symbol\n\t\t(lib_id "A:LM358")\n\t\t(at 100 50 0)\n\t\t(unit {unit})\n'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at 100 45 90)\n\t\t)\n'
        f'\t\t(property "Value" "LM358"\n\t\t\t(at 100 55 0)\n\t\t)\n'
        f"{mpn_text if mpn is not None else ''}"
        f'\t\t(instances\n\t\t\t(project "board"\n{instances}\t\t\t)\n\t\t)\n\t)\n'
    )


def write_project(
    directory: Path, root: str, sub: str = "", version=KICAD10, settings=None
) -> Path:
    sheets = ""
    if sub:
        sheets = "".join(
            f'\t(sheet\n\t\t(at 1 1)\n\t\t(size 2 2)\n\t\t(uuid "{uuid}")\n'
            f'\t\t(property "Sheetname" "{name}"\n\t\t\t(at 0 0 0)\n\t\t)\n'
            f'\t\t(property "Sheetfile" "sub.kicad_sch"\n\t\t\t(at 0 0 0)\n\t\t)\n\t)\n'
            for uuid, name in (("b1", "A"), ("b2", "B"))
        )
        (directory / "sub.kicad_sch").write_text(
            f'(kicad_sch\n\t(version {version})\n\t(generator "eeschema")\n\t(uuid "c0")\n{LIB}{sub})\n',
            encoding="utf-8",
        )
    path = directory / "board.kicad_sch"
    path.write_text(
        f'(kicad_sch\n\t(version {version})\n\t(generator "eeschema")\n\t(uuid "a0")\n{LIB}{root}{sheets})\n',
        encoding="utf-8",
    )
    variants = {"schematic": {"variants": [{"name": "Pro"}, {"name": "Lite"}]}}
    for key, value in (settings or {}).items():
        variants["schematic"][key] = value
    (directory / "board.kicad_pro").write_text(json.dumps(variants), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch, tmp_path):
    """KiCad 10.0.6 with no application settings: the default text size is 50 mils."""

    async def version():
        return "10.0.6"

    monkeypatch.setattr(symbol_fields, "get_kicad_version", version)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "kicad-config"))


async def write(path: Path, *changes: FieldChange) -> str:
    return await set_symbol_fields(str(path), list(changes))


class TestSymbolFields:
    async def test_each_unit_and_a_new_hidden_field(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1, mpn="A") + symbol("U1", 2, mpn="B"))
        out = await write(
            path, FieldChange(reference="U1", fields={"MPN": "X", "Value_Confirmed": "LM358 (X)"})
        )
        assert out.startswith(
            "# Fields of the symbols of board.kicad_pro\n\n✅ The tool wrote 4 field values"
        )
        assert "| U1 | A | - | MPN | A | X | board.kicad_sch |" in out
        assert "| U1 | B | - | Value_Confirmed | (no field) | LM358 (X) | board.kicad_sch |" in out
        text = path.read_text(encoding="utf-8")
        assert text.count('(property "MPN" "X"') == 2
        # KiCad 10: hide, show_name and do_not_autoplace in the property (saveField),
        # the angle of the Reference field and the position of the symbol.
        assert (
            text.count(
                '\t\t(property "Value_Confirmed" "LM358 (X)"\n\t\t\t(at 100 50 90)\n\t\t\t(hide yes)\n'
                "\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n\t\t\t(effects\n\t\t\t\t(font\n"
                "\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t\t(instances"
            )
            == 2
        )
        assert unit_conflicts(tmp_path / "board.kicad_pro", "10.0.6")[0] == []

    async def test_kicad9_format_and_text_size(self, tmp_path):
        settings = {"drawing": {"default_text_size": 60}}
        path = write_project(tmp_path, symbol("U1", 1), version=KICAD9, settings=settings)
        await write(path, FieldChange(reference="u1", fields={"Value_Confirmed": "LM358"}))
        # KiCad 8 and 9: the hidden state in the effects (SCH_FIELD::Format).
        assert (
            '\t\t(property "Value_Confirmed" "LM358"\n\t\t\t(at 100 50 90)\n\t\t\t(effects\n'
            "\t\t\t\t(font\n\t\t\t\t\t(size 1.524 1.524)\n\t\t\t\t)\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)"
        ) in path.read_text(encoding="utf-8")

    async def test_no_change_writes_no_file(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1, mpn="X"))
        before = path.read_text(encoding="utf-8")
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "X"}))
        assert "The fields have these values already. The tool wrote no file." in out
        assert path.read_text(encoding="utf-8") == before

    async def test_shared_sheet(self, tmp_path):
        # The sheet is used two times: one symbol has the references U5 and U6.
        sub = "".join(
            symbol("U5", unit, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6"))) for unit in (1, 2)
        )
        path = write_project(tmp_path, "", sub)
        out = await write(path, FieldChange(reference="U5", fields={"MPN": "X"}))
        assert out.startswith(
            "❌ The symbol of U5 is in a sheet that the hierarchy uses more than one time"
        )
        assert "KiCad keeps its fields for U6 too" in out
        out = await write(
            path,
            FieldChange(reference="U5", fields={"MPN": "X"}),
            FieldChange(reference="U6", fields={"MPN": "Y"}),
        )
        assert out.startswith("❌") and "Give the same fields for U5, U6" in out
        out = await write(
            path,
            FieldChange(reference="U5", fields={"MPN": "X"}),
            FieldChange(reference="U6", fields={"MPN": "X"}),
        )
        assert out.startswith("# Fields") and "| U5 | B | - | MPN | A | X | sub.kicad_sch |" in out
        assert "| U6 | B | - | MPN | A | X | sub.kicad_sch (the symbol of U5) |" in out
        assert (tmp_path / "sub.kicad_sch").read_text(encoding="utf-8").count(
            '(property "MPN" "X"'
        ) == 2

    async def test_variant(self, tmp_path):
        lite = (
            '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Lite")\n\t\t\t\t\t\t(dnp yes)\n'
            '\t\t\t\t\t\t(field\n\t\t\t\t\t\t\t(name "MPN")\n\t\t\t\t\t\t\t(value "L")\n'
            "\t\t\t\t\t\t)\n\t\t\t\t\t)\n"
        )
        path = write_project(
            tmp_path, symbol("U1", 1, variants=lite) + symbol("U1", 2, variants=lite)
        )
        # KiCad finds a field by its exact name: "mpn" is not "MPN" (SCH_SYMBOL::GetField).
        out = await write(path, FieldChange(reference="U1", fields={"mpn": "P"}, variant="Pro"))
        assert out.startswith("❌ The symbol U1A has no field 'mpn'")
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "P"}, variant="Pro"))
        assert "| U1 | A | Pro | MPN | A | P | board.kicad_sch |" in out
        text = path.read_text(encoding="utf-8")
        # SCH_IO_KICAD_SEXPR: the variant after the unit, one list on each line.
        pro = (
            '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Pro")\n\t\t\t\t\t\t(field\n'
            '\t\t\t\t\t\t\t(name "MPN")\n\t\t\t\t\t\t\t(value "P")\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n'
        )
        assert text.count(pro) == 2
        # A text equal to the field of the symbol removes the field from the variant,
        # and a variant with no differentials (SetFieldText, HasDifferentials).
        await write(path, FieldChange(reference="U1", fields={"MPN": "A"}, variant="Pro"))
        await write(path, FieldChange(reference="U1", fields={"MPN": "A"}, variant="Lite"))
        text = path.read_text(encoding="utf-8")
        assert '(name "Pro")' not in text and '(value "L")' not in text
        assert (
            text.count('(name "Lite")\n\t\t\t\t\t\t(dnp yes)\n\t\t\t\t\t)') == 2
        )  # the flag stays
        assert unit_conflicts(tmp_path / "board.kicad_pro", "10.0.6", ["Pro", "Lite"])[0] == []
        # KiCad 10 reads a mandatory field with no difference of case (also in an older
        # file): the tool takes only the exact name.
        out = await write(path, FieldChange(reference="U1", fields={"value": "X"}, variant="Pro"))
        assert out.startswith("❌ Give the field name 'Value', not 'value'.")

    @pytest.mark.parametrize(
        ("change", "text"),
        [
            (
                FieldChange(reference="U1", fields={"Reference": "U2"}),
                "does not write the Reference",
            ),
            (FieldChange(reference="U?", fields={"MPN": "X"}), "is not annotated"),
            # SCH_REFERENCE::Split: no number is not annotated too.
            (FieldChange(reference="U", fields={"MPN": "X"}), "is not annotated"),
            (FieldChange(reference="U9", fields={"MPN": "X"}), "no symbol with the reference U9"),
            (FieldChange(reference="U1", fields={"MPN": "X\nY"}), "has a line break or a tab"),
            (FieldChange(reference="U1", fields={}), "has no fields"),
            (
                FieldChange(reference="U1", fields={"Nope": "X"}, variant="Pro"),
                "has no field 'Nope'",
            ),
            (
                FieldChange(reference="U1", fields={"MPN": "X"}, variant="Nope"),
                "has no variant 'Nope'",
            ),
        ],
    )
    async def test_refused(self, tmp_path, change, text):
        path = write_project(tmp_path, symbol("U1", 1) + symbol("U1", 2))
        before = path.read_text(encoding="utf-8")
        out = await write(path, change)
        assert out.startswith("❌") and text in out
        assert path.read_text(encoding="utf-8") == before

    async def test_refused_cases_of_the_project(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1) + symbol("U1", 1))
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "X"}))
        assert out.startswith("❌ U1 is a duplicate reference")
        path = write_project(tmp_path, symbol("U1", 1), version=KICAD9)
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "X"}, variant="Pro"))
        assert "has a format with no variants (file version 20250114)" in out
        change = FieldChange(reference="U1", fields={"MPN": "X"})
        out = await write(path, change, change)
        assert out.startswith("❌ Two changes of U1")

    async def test_open_file_writes_no_file(self, tmp_path):
        sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
        path = write_project(tmp_path, symbol("U1", 1), sub)
        lock_file_path(tmp_path / "sub.kicad_sch").write_text(
            '{"hostname":"h","username":"u"}', encoding="utf-8"
        )
        before = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.kicad_sch")}
        out = await write(
            path,
            FieldChange(reference="U1", fields={"MPN": "X"}),
            FieldChange(reference="U5", fields={"MPN": "X"}),
            FieldChange(reference="U6", fields={"MPN": "X"}),
        )
        assert out.startswith("❌ The tool wrote no file: KiCad has the schematic open"), out
        assert {
            p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.kicad_sch")
        } == before


class TestReviewCases:
    @pytest.mark.parametrize("version", [KICAD10, KICAD9])
    async def test_mandatory_name_with_other_case(self, tmp_path, version):
        # KiCad 10 reads "value" as the Value, also in a file of KiCad 9: the tool
        # refuses the name, also with the exact name in the same change.
        path = write_project(tmp_path, symbol("U1", 1), version=version)
        before = path.read_text(encoding="utf-8")
        out = await write(path, FieldChange(reference="U1", fields={"Value": "B", "value": "C"}))
        assert out.startswith("❌ Give the field name 'Value', not 'value'.")
        assert path.read_text(encoding="utf-8") == before

    async def test_legacy_simulation_fields(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1))
        for name in ("Sim.Enable", "Spice_Netlist_Enabled"):
            out = await write(path, FieldChange(reference="U1", fields={name: "1"}))
            assert out.startswith(f"❌ KiCad removes the field {name!r} when it loads the symbol")

    async def test_remove_a_variant_and_insert_one_before_it(self, tmp_path):
        # One edit for all variants of an instance: the removal of Pro and the new
        # Lite before it cannot overlap.
        pro = (
            '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Pro")\n\t\t\t\t\t\t(field\n'
            '\t\t\t\t\t\t\t(name "MPN")\n\t\t\t\t\t\t\t(value "P")\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n'
        )
        path = write_project(tmp_path, symbol("U1", 1, variants=pro))
        out = await write(
            path,
            FieldChange(reference="U1", fields={"MPN": "L"}, variant="Lite"),
            FieldChange(reference="U1", fields={"MPN": "A"}, variant="Pro"),
        )
        assert out.startswith("# Fields")
        text = path.read_text(encoding="utf-8")
        assert '(name "Pro")' not in text and '(name "Lite")' in text
        assert unit_conflicts(tmp_path / "board.kicad_pro", "10.0.6", ["Lite"])[0] == []

    async def test_sheet_hierarchy_that_the_tool_cannot_read(self, tmp_path):
        # The second place of the shared sheet has no UUID: KiCad gives it new UUIDs
        # at each load, thus the tool cannot find its references (sheet_paths).
        sub = symbol("U5", 1, paths=(("/a0/b1", "U5"),))
        path = write_project(tmp_path, symbol("U1", 1), sub)
        text = path.read_text(encoding="utf-8").replace('\t\t(uuid "b2")\n', "")
        path.write_text(text, encoding="utf-8")
        out = await write(path, FieldChange(reference="U5", fields={"MPN": "X"}))
        assert out.startswith("❌ The tool cannot read the full sheet hierarchy")
        assert (tmp_path / "sub.kicad_sch").read_text(encoding="utf-8").count('"MPN" "X"') == 0

    async def test_failed_write_restores_the_written_files(self, tmp_path, monkeypatch):
        before, out = await _write_with(tmp_path, monkeypatch, fail_commit=2)
        assert out.startswith("❌ The tool could not write "), out
        assert out.endswith("again)."), out
        assert "The tool wrote no file (it wrote the old content into " in out
        assert _texts(tmp_path) == before
        assert not list(tmp_path.glob("*.kicad-save-*"))

    async def test_a_disk_error_before_the_renames_changes_no_file(self, tmp_path, monkeypatch):
        # The temporary file of the second file cannot be written: before, the
        # first file had the new fields already.
        before, out = await _write_with(tmp_path, monkeypatch, fail_temp=2)
        sub = tmp_path / "sub.kicad_sch"
        assert out == f"❌ The tool wrote no file. {sub}: [Errno 28] No space left on device.", out
        assert _texts(tmp_path) == before
        assert not list(tmp_path.glob("*.kicad-save-*"))

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions of a user"
    )
    async def test_a_read_only_file_changes_no_file(self, tmp_path):
        sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
        path = write_project(tmp_path, symbol("U1", 1), sub)
        (tmp_path / "sub.kicad_sch").chmod(0o444)
        before = _texts(tmp_path)
        out = await write(path, *_CHANGES)
        assert out.startswith(f"❌ The tool wrote no file. {tmp_path / 'sub.kicad_sch'}: "), out
        assert "is read only" in out, out
        assert _texts(tmp_path) == before

    async def test_an_error_after_a_save_is_a_warning(self, tmp_path, monkeypatch):
        from kicad_workbench.utils import file_save

        real = file_save.PendingSave.commit
        calls = []

        def commit(save):
            real(save)
            calls.append(save.target)
            if len(calls) == 1:
                raise file_save.AfterSaveError(f"The tool wrote {save.target}, but no flush")

        monkeypatch.setattr(file_save.PendingSave, "commit", commit)
        sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
        path = write_project(tmp_path, symbol("U1", 1), sub)
        out = await write(path, *_CHANGES)
        assert out.startswith("# Fields"), out
        assert f"⚠️ The tool wrote {calls[0]}, but no flush" in out
        for name in ("board.kicad_sch", "sub.kicad_sch"):
            assert '"MPN" "X"' in (tmp_path / name).read_text(encoding="utf-8")

    async def test_a_failed_restore_names_the_file(self, tmp_path, monkeypatch):
        from kicad_workbench.utils import kicad_lock

        def save_file(path, data):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(kicad_lock, "save_file", save_file)
        _, out = await _write_with(tmp_path, monkeypatch, fail_commit=2)
        assert "could not write the old content into these files again: " in out, out
        assert out.endswith("These files have the new content, the other files do not."), out

    async def test_instances_of_an_other_project(self, tmp_path):
        text = symbol("U1", 1).replace(
            '\t\t\t(project "board"',
            '\t\t\t(project "old_board"\n\t\t\t\t(path "/x"\n\t\t\t\t\t(reference "R9")\n'
            '\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t\t(project "board"',
        )
        path = write_project(tmp_path, text)
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "X"}))
        assert out.startswith("# Fields")
        assert "⚠️ The file board.kicad_sch has instances of the projects old_board" in out

    async def test_variant_with_the_same_value_writes_no_file(self, tmp_path):
        lite = '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Lite")\n\t\t\t\t\t\t(field\n\t\t\t\t\t\t\t(name "MPN")\n\t\t\t\t\t\t\t(value "L")\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n'
        path = write_project(tmp_path, symbol("U1", 1, variants=lite))
        stamp = path.stat().st_mtime_ns
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "L"}, variant="Lite"))
        assert "The tool wrote no file." in out and path.stat().st_mtime_ns == stamp

    async def test_new_field_and_its_variant_in_one_call(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1))
        out = await write(
            path,
            FieldChange(reference="U1", fields={"Value_Confirmed": "X"}),
            FieldChange(reference="U1", fields={"Value_Confirmed": "Y"}, variant="Pro"),
        )
        assert out.startswith("# Fields")
        text = path.read_text(encoding="utf-8")
        assert '(name "Value_Confirmed")\n\t\t\t\t\t\t\t(value "Y")' in text

    async def test_variants_in_the_order_of_kicad(self, tmp_path):
        pro = '\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Pro")\n\t\t\t\t\t\t(field\n\t\t\t\t\t\t\t(name "MPN")\n\t\t\t\t\t\t\t(value "P")\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n'
        path = write_project(tmp_path, symbol("U1", 1, variants=pro))
        await write(path, FieldChange(reference="U1", fields={"MPN": "L"}, variant="Lite"))
        text = path.read_text(encoding="utf-8")
        # SCH_IO_KICAD_SEXPR writes the variants sorted by name (std::map).
        assert text.index('(name "Lite")') < text.index('(name "Pro")')
        assert '\t\t\t\t\t)\n\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "Pro")' in text

    async def test_tilde_in_an_old_file(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1), version=KICAD9)
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "~"}))
        assert out.startswith("❌") and 'reads "~" as an empty field' in out

    async def test_kicad8_new_field_is_visible(self, tmp_path):
        # KiCad 8 does not hide a new field (DIALOG_SYMBOL_PROPERTIES::OnAddField of
        # 8.0.9). KiCad writes the effects also with the default size.
        path = write_project(tmp_path, symbol("U1", 1), version="20231120")
        await write(
            path, FieldChange(reference="U1", fields={"Manufacturer": "TI", "Value_Confirmed": "V"})
        )
        text = path.read_text(encoding="utf-8")
        assert (
            '\t\t(property "Manufacturer" "TI"\n\t\t\t(at 100 50 90)\n\t\t\t(effects\n\t\t\t\t(font\n'
            "\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        ) in text
        assert '(property "Value_Confirmed" "V"\n\t\t\t(at 100 50 90)\n\t\t\t(effects' in text
        assert "\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)\n\t\t(instances" in text

    async def test_file_versions_of_the_nightly_builds(self, tmp_path):
        # Variants from 20250922; the flags of a field in the property from 20251028.
        path = write_project(tmp_path, symbol("U1", 1), version="20250610")
        out = await write(path, FieldChange(reference="U1", fields={"MPN": "X"}, variant="Pro"))
        assert "has a format with no variants (file version 20250610)" in out
        path = write_project(tmp_path, symbol("U1", 1), version="20251012")
        await write(path, FieldChange(reference="U1", fields={"Manufacturer": "TI"}))
        text = path.read_text(encoding="utf-8")
        assert "\t\t\t\t(hide yes)\n\t\t\t)\n" in text and "show_name" not in text

    async def test_output_cells_and_count(self, tmp_path):
        sub = "".join(
            symbol("U5", unit, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6"))) for unit in (1, 2)
        )
        path = write_project(tmp_path, "", sub)
        out = await write(
            path,
            FieldChange(reference="U5", fields={"MPN": "A|B"}),
            FieldChange(reference="U6", fields={"MPN": "A|B"}),
        )
        # Two units of one symbol: two field values, also with two references.
        assert "✅ The tool wrote 2 field values into `sub.kicad_sch`." in out
        assert "| A\\|B |" in out


_CHANGES = (
    FieldChange(reference="U1", fields={"MPN": "X"}),
    FieldChange(reference="U5", fields={"MPN": "X"}),
    FieldChange(reference="U6", fields={"MPN": "X"}),
)


def _texts(folder):
    return {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.kicad_sch")}


async def _write_with(tmp_path, monkeypatch, fail_commit=0, fail_temp=0):
    """Write U1 (board.kicad_sch) and U5, U6 (sub.kicad_sch). The rename or the
    temporary file of the file number fail_commit or fail_temp fails."""
    from kicad_workbench.utils import file_save

    sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
    path = write_project(tmp_path, symbol("U1", 1), sub)
    before = _texts(tmp_path)
    renames, temps = [], []
    real_rename, real_write = file_save._rename, file_save._write_all

    def rename(temp, target):
        renames.append(target)
        if len(renames) == fail_commit:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(temp, target)

    def write_all(fd, data):
        temps.append(fd)
        if len(temps) == fail_temp:
            raise OSError(28, "No space left on device")
        real_write(fd, data)

    monkeypatch.setattr(file_save, "_rename", rename)
    monkeypatch.setattr(file_save, "_write_all", write_all)
    return before, await write(path, *_CHANGES)
