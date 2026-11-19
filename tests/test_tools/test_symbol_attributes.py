"""Tests for the tool set_symbol_attributes (tools/symbol_fields.py), with the rules of
the Symbol Properties dialog of KiCad 10.0.6. spike/kicad-versions/symbol_attributes_check.py
compares the writes with kicad-cli 10.0.6."""

import pytest
from pydantic import ValidationError

from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import symbol_fields
from kicad_workbench.tools.symbol_fields import AttributeChange, set_symbol_attributes

from .test_symbol_fields import write_project

ATTRS = "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n\t\t(dnp no)\n"


def symbol(reference, unit, paths=("/a0",), variants="", attributes=ATTRS):
    """A placed unit of LM358 with the attribute tokens of KiCad 10."""
    instances = ""
    for path in paths:
        path, ref = path if isinstance(path, tuple) else (path, reference)
        instances += (
            f'\t\t\t\t(path "{path}"\n\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit {unit})\n'
            f"{variants}\t\t\t\t)\n"
        )
    return (
        f'\t(symbol\n\t\t(lib_id "A:LM358")\n\t\t(at 100 50 0)\n\t\t(unit {unit})\n{attributes}'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at 100 45 90)\n\t\t)\n'
        f'\t\t(property "Value" "LM358"\n\t\t\t(at 100 55 0)\n\t\t)\n'
        f'\t\t(property "MPN" "A"\n\t\t\t(at 100 50 0)\n\t\t\t(hide yes)\n\t\t)\n'
        f'\t\t(instances\n\t\t\t(project "board"\n{instances}\t\t\t)\n\t\t)\n\t)\n'
    )


def variant(name, *items):
    body = "".join(f"\t\t\t\t\t\t{item}\n" for item in items)
    return f'\t\t\t\t\t(variant\n\t\t\t\t\t\t(name "{name}")\n{body}\t\t\t\t\t)\n'


@pytest.fixture(autouse=True)
def kicad_10(monkeypatch, tmp_path):
    async def version():
        return "10.0.6"

    monkeypatch.setattr(symbol_fields, "get_kicad_version", version)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "kicad-config"))


async def write(path, *changes):
    return await set_symbol_attributes(str(path), list(changes))


def change(reference, variant="", **attributes):
    return AttributeChange(reference=reference, attributes=attributes, variant=variant)


def symbols(path):
    """The attribute tokens and the variants of each placed unit: (reference, unit) ->
    (tokens, {variant: tokens and fields})."""
    found = {}
    for node in parse(path.read_text("utf-8")).children("symbol"):
        reference = next(p.value(1) for p in node.children("property") if p.value(0) == "Reference")
        tokens = {
            item.head: item.value(0)
            for item in node.children()
            if item.head in ("exclude_from_sim", "in_bom", "on_board", "in_pos_files", "dnp")
        }
        variants = {}
        for path_node in node.first("instances").first("project").children("path"):
            for item in path_node.children("variant"):
                variants[item.first("name").value(0)] = [
                    (child.head, child.value(0))
                    for child in item.children()
                    if child.head != "name"
                ]
        found[(reference, node.first("unit").value(0))] = (tokens, variants)
    return found


class TestSymbolAttributes:
    async def test_each_unit(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1) + symbol("U1", 2))
        out = await write(path, change("U1", dnp=True, exclude_from_bom=True,
                                       exclude_from_simulation=True))  # fmt: skip
        assert out.startswith("# Attributes of the symbols of board.kicad_pro"), out
        assert "✅ The tool wrote 6 attribute values" in out, out
        for unit in ("1", "2"):
            tokens, _variants = symbols(path)[("U1", unit)]
            # in_bom has the inverted logic; Exclude from simulation in each unit.
            assert tokens == {
                "exclude_from_sim": "yes", "in_bom": "no", "on_board": "yes",
                "in_pos_files": "yes", "dnp": "yes",
            }  # fmt: skip
        again = await write(path, change("U1", dnp=True))
        assert "have these values already" in again

    async def test_a_variant_keeps_the_old_value(self, tmp_path):
        # The variant Lite sets only a field: KiCad starts it with the attributes of the
        # load, thus after the change it differs and gets the old value as a token.
        lite = variant("Lite", '(field\n(name "MPN")\n(value "B")\n)')
        path = write_project(tmp_path, symbol("U1", 1, variants=lite))
        await write(path, change("U1", dnp=True))
        tokens, variants = symbols(path)[("U1", "1")]
        assert tokens["dnp"] == "yes"
        assert variants["Lite"] == [("dnp", "no"), ("field", None)]

    async def test_a_variant_token_equal_to_the_new_value_goes(self, tmp_path):
        pro = variant("Pro", "(dnp yes)")
        path = write_project(tmp_path, symbol("U1", 1, variants=pro))
        await write(path, change("U1", dnp=True))
        # No differentials: KiCad writes no variant.
        assert symbols(path)[("U1", "1")][1] == {}

    async def test_the_attributes_of_a_variant(self, tmp_path):
        path = write_project(tmp_path, symbol("U1", 1))
        out = await write(path, change("U1", variant="Pro", dnp=True, exclude_from_board=False))
        assert "| U1 | A | Pro | dnp | no | yes |" in out, out
        # Only the differential: exclude_from_board equals the symbol.
        assert symbols(path)[("U1", "1")][1] == {"Pro": [("dnp", "yes")]}
        # A value equal to the symbol: no differential, the variant goes.
        await write(path, change("U1", variant="Pro", dnp=False))
        assert symbols(path)[("U1", "1")][1] == {}

    async def test_symbol_and_new_variant_in_one_call(self, tmp_path):
        # SetDNP of a new variant starts it with the attributes of the symbol after the
        # change of the symbol (InitializeAttributes).
        path = write_project(tmp_path, symbol("U1", 1))
        await write(
            path, change("U1", dnp=True), change("U1", variant="Lite", exclude_from_bom=True)
        )
        tokens, variants = symbols(path)[("U1", "1")]
        assert tokens["dnp"] == "yes" and variants == {"Lite": [("in_bom", "no")]}

    async def test_a_shared_sheet(self, tmp_path):
        sub = symbol("U5", 1, paths=(("/a0/b1", "U5"), ("/a0/b2", "U6")))
        path = write_project(tmp_path, "", sub)
        out = await write(path, change("U5", dnp=True))
        assert out.startswith("❌ The symbol of U5 is in a sheet") and "attributes" in out, out
        out = await write(path, change("U5", dnp=True), change("U6", dnp=True))
        assert out.startswith("# Attributes"), out

    async def test_errors(self, tmp_path, monkeypatch):
        path = write_project(tmp_path, symbol("U1", 1, attributes=""))
        out = await write(path, change("U1", dnp=True))
        assert "has no (dnp ...)" in out, out
        assert "has no attributes" in await write(path, change("U1"))
        assert "has no variant 'Nope'" in await write(path, change("U1", variant="Nope", dnp=True))
        assert "is not annotated" in await write(path, change("U?", dnp=True))

        async def kicad_9():
            return "9.0.9"

        monkeypatch.setattr(symbol_fields, "get_kicad_version", kicad_9)
        out = await write(path, change("U1", dnp=True))
        assert out.startswith("❌") and "10.0" in out, out


class TestReviewCases:
    def test_an_unknown_attribute_name(self):
        # The token name of the file is not an attribute: an error, not a part of the
        # change. The schema tells the client, and the server refuses the arguments.
        with pytest.raises(ValidationError, match="in_bom"):
            AttributeChange.model_validate(
                {"reference": "U1", "attributes": {"dnp": True, "in_bom": False}}
            )
        schema = AttributeChange.model_json_schema()
        assert schema["$defs"]["SymbolAttributes"]["additionalProperties"] is False
        assert schema["additionalProperties"] is False

    async def test_a_value_equal_to_the_variant_writes_no_file(self, tmp_path):
        lite = variant("Lite", '(field\n(name "MPN")\n(value "B")\n)')
        path = write_project(tmp_path, symbol("U1", 1, variants=lite))
        before = path.read_bytes()
        out = await write(path, change("U1", variant="Lite", dnp=False))
        assert "have these values already" in out, out
        assert path.read_bytes() == before

    async def test_the_last_token_stays(self, tmp_path):
        # The parser sets DNP from each token: the last one stays.
        attributes = ATTRS + "\t\t(dnp yes)\n"
        path = write_project(tmp_path, symbol("U1", 1, attributes=attributes))
        out = await write(path, change("U1", dnp=False))
        assert "| dnp | yes | no |" in out, out
        text = path.read_text("utf-8")
        assert "(dnp no)" in text and "(dnp yes)" not in text

    async def test_a_quoted_value(self, tmp_path):
        attributes = ATTRS.replace("(dnp no)", '(dnp "no")')
        path = write_project(tmp_path, symbol("U1", 1, attributes=attributes))
        out = await write(path, change("U1", dnp=True))
        assert "is not yes or no" in out, out
