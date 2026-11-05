"""Tests for utils/pin_pad_mapping.py, parsers/board_netlist.py and the pin and pad tools."""

import subprocess
from pathlib import Path

import pytest

from kicad_workbench.parsers.annotation import AnnotationCheck, AnnotationError, AnnotationItem
from kicad_workbench.parsers.board_netlist import NetlistPin, read_board_netlist
from kicad_workbench.parsers.symbol_info import SymbolPin
from kicad_workbench.tools import library as library_tools
from kicad_workbench.utils import kicad_cli as kicad_cli_module
from kicad_workbench.utils import pin_pad_mapping
from kicad_workbench.utils.library_footprint import FootprintItems, PadNumber, footprint_info
from kicad_workbench.utils.pin_pad_mapping import (
    MAX_STACKED,
    StackedPinError,
    expand_stacked,
    map_pins,
    match_pads,
)

from .test_library_footprint import make_footprints, pad, script_result
from .test_library_search import symbol_lib, table_rows
from .test_symbol_info import pin


def symbol_pin(number: str, unit: int = 1, style: int = 1) -> SymbolPin:
    return SymbolPin(
        number, f"N{number}", "passive", "line", unit, style, 0, 0, "right", 0, False, ()
    )


def pads(*numbers: str, **values):
    return footprint_info(script_result(pads=[pad(number=n, **values) for n in numbers])).pads


class TestExpandStacked:
    @pytest.mark.parametrize(
        "number, numbers",
        [
            ("1", ["1"]),
            ("", [""]),
            ("1-3", ["1-3"]),  # No brackets: one number.
            ("[1-3]", ["1", "2", "3"]),
            ("[1,3, A5 ,]", ["1", "3", "A5"]),
            ("[A01-A03]", ["A1", "A2", "A3"]),  # No leading zeros (wxString::Format %ld).
            ("[ 7 - 8 ]", ["7", "8"]),
            ("[5]", ["5"]),
            ("[[5]]", ["[5]"]),
            ("[a]b]", ["a]b"]),  # KiCad 10.0 does not look for brackets in the list.
            ("[x-]", ["[x-]"]),  # Not valid: no digits at the end.
        ],
    )
    def test_numbers(self, number, numbers):
        assert expand_stacked(number)[0] == numbers

    @pytest.mark.parametrize(
        "number", ["[1", "1]", "a[1]", "[]", "[,]", "[1-]", "[A1-B3]", "[3-1]", "[A-C]"]
    )
    def test_notation_that_is_not_valid(self, number):
        assert expand_stacked(number) == ([number], False)

    def test_valid(self):
        assert expand_stacked("1") == (["1"], True)
        assert expand_stacked("[1-2]") == (["1", "2"], True)

    def test_number_larger_than_long(self, monkeypatch):
        # wxString::ToLong gives LONG_MAX for a number that is too large.
        monkeypatch.setattr(pin_pad_mapping, "_LONG_MAX", 2**31 - 1)
        assert expand_stacked("[99999999999-99999999999]") == ([str(2**31 - 1)], True)

    def test_too_many_numbers(self):
        assert len(expand_stacked(f"[1-{MAX_STACKED}]")[0]) == MAX_STACKED
        with pytest.raises(StackedPinError, match="more than 100000"):
            expand_stacked(f"[1-{MAX_STACKED},A1]")
        # A range of more numbers than a Python range can count.
        with pytest.raises(StackedPinError, match="more than 100000"):
            expand_stacked("[0-99999999999999999999]")
        # int() refuses a text of more than 4300 digits.
        with pytest.raises(StackedPinError, match="more than 100000"):
            expand_stacked(f"[1-{'9' * 5000}]")
        # A part that is not valid makes the full notation not valid.
        big = f"[1-{MAX_STACKED * 2},x-]"
        assert expand_stacked(big) == ([big], False)


class TestMapPins:
    def test_all_match(self):
        result = map_pins([symbol_pin("1"), symbol_pin("2")], pads("1", "2", "2"), False)
        assert (result.no_number, result.no_pad, result.no_pin) == ((), (), ())
        assert result.pin_numbers == 2

    def test_errors_and_warnings(self):
        pins = [symbol_pin("1"), symbol_pin("3", unit=2), symbol_pin("3", unit=3), symbol_pin("")]
        footprint = pads("1", "2", "") + pads("4", on_copper=False)
        result = map_pins(pins, footprint, False)
        assert result.no_number == (pins[3],)
        # The netlist has the pin number 3 one time.
        assert result.no_pad == (("3", (pins[1], pins[2])),)
        # A pad with no number, and a pad that is not on copper, give no warning.
        assert result.no_pin == (1,)
        assert result.pin_numbers == 2

    def test_numbers_are_equal_only_as_text(self):
        result = map_pins([symbol_pin("01"), symbol_pin("a")], pads("1", "A"), False)
        assert [number for number, _pins in result.no_pad] == ["01", "a"]
        assert result.no_pin == (0, 1)

    def test_stacked_numbers_of_kicad_10(self):
        pins = [symbol_pin("[1-2]"), symbol_pin("[3"), symbol_pin("[[5]]")]
        result = map_pins(pins, pads("1", "2", "[3", "5"), True)
        assert result.stacked == ((pins[0], ("1", "2")), (pins[2], ("[5]",)))
        assert result.invalid == (pins[1],)
        # FindPadByNumber("[5]") finds no pad, but GetNet gives pad 5 the net
        # of the pin "[5]" of the netlist.
        assert result.no_pad == (("[5]", (pins[2],)),)
        assert result.no_pin == ()

    def test_stacked_numbers_of_kicad_9(self):
        pins = [symbol_pin("[1-2]")]
        result = map_pins(pins, pads("1", "2"), False)
        assert result.no_pad == (("[1-2]", (pins[0],)),)
        assert result.no_pin == (0, 1)
        assert result.stacked == () and result.invalid == ()


def part(name: str, units: dict[str, list[str]], footprint: str = "Lib:Test", extra: str = ""):
    """A library symbol with a Footprint field and sub-symbols "<name>_<key>"."""
    subs = "".join(
        f'\t\t(symbol "{name}_{key}"\n' + "".join(f"\t\t\t{p}\n" for p in pins) + "\t\t)\n"
        for key, pins in units.items()
    )
    return (
        f'\t(symbol "{name}"{extra}\n'
        f'\t\t(property "Footprint" "{footprint}" (at 0 0 0))\n{subs}\t)\n'
    )


SYMBOLS = symbol_lib(
    part("R2", {"1_1": [pin("1"), pin("2")]}),
    part(
        "Q",
        {"1_1": [pin("1")], "2_1": [pin("2"), pin("3")], "0_1": [pin("", kind="power_in line")]},
    ),
    part("S", {"1_1": [pin("1"), pin("2")], "1_2": [pin("1"), pin("4")]}),
    part("ST", {"1_1": [pin("[1-2]"), pin("3"), pin("[9")]}),
    part("NoFp", {"1_1": [pin("1")]}, footprint=""),
    part("BadFp", {"1_1": [pin("1")]}, footprint="Test"),
    part("Other", {"1_1": [pin("1")]}, footprint="Nope:X"),
    part("Off", {"1_1": [pin("1"), pin("2")]}, extra="\n\t\t(on_board no)"),
    part("PWR", {"0_1": [pin("1", kind="power_in line")]}, footprint="", extra="\n\t\t(power)"),
)


@pytest.fixture
def parts(tmp_path, monkeypatch):
    footprints = make_footprints(tmp_path, monkeypatch)
    (footprints["libs"] / "Device.kicad_sym").write_text(SYMBOLS, encoding="utf-8")
    footprints["result"]["value"] = (
        "10.0.6",
        footprint_info(script_result(pads=[pad(number=n) for n in ("1", "2", "")])),
    )
    return footprints


def set_pads(parts, *numbers: str) -> None:
    version = parts["result"]["value"][0]
    parts["result"]["value"] = (
        version,
        footprint_info(script_result(pads=[pad(number=n) for n in numbers])),
    )


def set_version(parts, monkeypatch, version: str) -> None:
    """Use an other installed KiCad, with the same library tables."""

    async def kicad_version():
        return version

    monkeypatch.setattr(library_tools, "get_kicad_version", kicad_version)
    config = parts["config"] / ".".join(version.split(".")[:2])
    config.mkdir(exist_ok=True)
    for name in ("sym-lib-table", "fp-lib-table"):
        (config / name).write_text((parts["config"] / "10.0" / name).read_text("utf-8"), "utf-8")
    parts["result"]["value"] = (version, parts["result"]["value"][1])


def section(out: str, heading: str) -> str:
    start = out.index(f"## {heading}")
    end = out.find("\n## ", start + 1)
    return out[start : end if end >= 0 else len(out)]


class TestCheckPinPadMapping:
    async def test_match(self, parts):
        out = await library_tools.check_pin_pad_mapping("Device:R2", "Lib:Test")
        assert out.startswith("# Pins and pads of Device:R2 and Lib:Test")
        assert f"**Footprint**: Lib (global): {parts['pretty'] / 'Test.kicad_mod'}\n" in out
        assert '**Rules**: "Update PCB from Schematic" of KiCad 10.0.6' in out
        assert "**Pins**: 2, with 2 pin numbers\n" in out
        assert "**Pads**: 2 with a number, 1 with no number" in out
        assert "✅ Each pin number has a pad, and each pad with a number has a pin." in out
        assert "## " not in out
        assert parts["result"]["args"] == (parts["pretty"], "Test")

    async def test_footprint_field(self, parts):
        out = await library_tools.check_pin_pad_mapping("Device:R2")
        assert "Test.kicad_mod (the Footprint field of the symbol)" in out

    async def test_errors_and_warnings(self, parts):
        set_pads(parts, "1", "2", "3")
        out = await library_tools.check_pin_pad_mapping("Device:Q")
        assert "**Pins**: 4, with 3 pin numbers in 2 units" in out
        assert "**Result**: ❌ 1 pin with no number\n" in out
        assert "_The check uses the pins of all units." in out
        assert table_rows(section(out, "Pins with no number (1)")) == [
            ["", "N", "power_in", "all", "1"]
        ]
        set_pads(parts, "1", "2", "5", "5")
        out = await library_tools.check_pin_pad_mapping("Device:Q")
        assert (
            "**Result**: ❌ 1 pin number with no pad, 1 pin with no number, 2 pads with no pin"
            in out
        )
        assert table_rows(section(out, "Pin numbers with no pad (1)")) == [
            ["3", "N3", "passive", "2 (B)", "1"]
        ]
        assert '"<reference> pad <number> not found in <footprint>"' in out
        assert table_rows(section(out, "Pads with no pin (2)")) == [
            ["5", "smd", "F.Cu, F.Mask, F.Paste"],
            ["5", "smd", "F.Cu, F.Mask, F.Paste"],
        ]

    async def test_body_styles(self, parts):
        set_pads(parts, "1", "2", "3")
        out = await library_tools.check_pin_pad_mapping("Device:S")
        assert "**Pins**: 4 in all body styles, with 2 pin numbers\n" in out
        assert table_rows(section(out, "Pin numbers with no pad (1)")) == [
            ["4", "N4", "passive", "1", "2 (Alternate)"]
        ]
        # Pad 2 has a pin only in body style 1, and pad 3 in no body style.
        assert table_rows(section(out, "Pads with no pin (2)")) == [
            ["2", "smd", "F.Cu, F.Mask, F.Paste", "2 (Alternate)"],
            ["3", "smd", "F.Cu, F.Mask, F.Paste", "1 (Standard), 2 (Alternate)"],
        ]

    async def test_stacked_pins_of_kicad_10(self, parts):
        set_pads(parts, "1", "2", "3")
        out = await library_tools.check_pin_pad_mapping("Device:ST")
        assert table_rows(section(out, "Stacked pin numbers (1)")) == [
            ["[1-2]", "N[1-2]", "1", "1, 2"]
        ]
        assert table_rows(section(out, "Pin numbers with brackets")) == [
            ["[9", "N[9", "passive", "1", "1"]
        ]
        assert '"Pin name resembles stacked pin" (stacked_pin_name)' in out
        assert table_rows(section(out, "Pin numbers with no pad (1)")) == [
            ["[9", "N[9", "passive", "1", "1"]
        ]

    async def test_stacked_pins_of_kicad_9(self, parts, monkeypatch):
        set_pads(parts, "1", "2", "3")
        set_version(parts, monkeypatch, "9.0.9")
        out = await library_tools.check_pin_pad_mapping("Device:ST")
        assert "## Stacked pin numbers" not in out
        assert [row[0] for row in table_rows(section(out, "Pin numbers with no pad (2)"))] == [
            "[1-2]",
            "[9",
        ]
        assert [row[0] for row in table_rows(section(out, "Pads with no pin (2)"))] == ["1", "2"]
        assert "_KiCad 9.0.9 uses a pin number with brackets as one number." in out

    async def test_nightly_build(self, parts, monkeypatch):
        set_version(parts, monkeypatch, "10.99.0")
        out = await library_tools.check_pin_pad_mapping("Device:ST")
        assert out.startswith("❌ The installed KiCad is the nightly build 10.99.0.")
        # The tool stops before pcbnew reads the footprint.
        assert "args" not in parts["result"]
        # "[9" is one number in each version.
        assert out.endswith('the pin numbers "[1-2]".')
        out = await library_tools.check_pin_pad_mapping("Device:R2")
        assert "✅" in out

    async def test_warnings_only(self, parts):
        set_pads(parts, "1", "2", "3")
        out = await library_tools.check_pin_pad_mapping("Device:R2")
        assert "**Result**: ⚠️ 1 pad with no pin\n" in out

    async def test_symbol_not_on_board(self, parts):
        out = await library_tools.check_pin_pad_mapping("Device:Off")
        assert '_The symbol has the flag "Exclude from board"' in out

    @pytest.mark.parametrize(
        "symbol_id, footprint_id, message",
        [
            ("R2", "", 'is not "<library>:<symbol>"'),
            ("Device:Nope", "", "Cannot read the symbol Device:Nope"),
            ("Device:PWR", "", "is a power symbol"),
            ("Device:NoFp", "", "The Footprint field of the symbol Device:NoFp is empty"),
            ("Device:BadFp", "", 'is not "<library>:<footprint>", for example'),
            ("Device:BadFp", "", "It is the Footprint field of the symbol."),
            ("Device:R2", "Test", 'is not "<library>:<footprint>", for example'),
            ("Device:Other", "", "Cannot read the footprint Nope:X"),
        ],
    )
    async def test_errors(self, parts, symbol_id, footprint_id, message):
        out = await library_tools.check_pin_pad_mapping(symbol_id, footprint_id)
        assert out.startswith("❌") and message in out, out

    async def test_unknown_kicad(self, parts, monkeypatch):
        set_version(parts, monkeypatch, "11.0.0")
        out = await library_tools.check_pin_pad_mapping("Device:R2")
        assert out.startswith("❌ The installed KiCad is 11.0.0.")

    async def test_too_many_stacked_numbers(self, parts):
        (parts["libs"] / "Device.kicad_sym").write_text(
            symbol_lib(part("Big", {"1_1": [pin(f"[1-{MAX_STACKED + 1}]")]})), encoding="utf-8"
        )
        out = await library_tools.check_pin_pad_mapping("Device:Big")
        assert out.startswith("❌ The pin number") and "more than 100000" in out


class TestMatchPads:
    def test_numbers_of_a_netlist(self):
        # A pin of two units is two times in the nets of the netlist.
        no_pad, no_pin = match_pads(["1", "3", "3"], pads("1", "2", ""), False)
        assert (no_pad, no_pin) == (["3"], (1,))

    def test_stacked_number_of_the_netlist(self):
        # KiCad 10: the netlist number "[5]" (of the pin "[[5]]") has no pad,
        # but pad 5 gets its net (COMPONENT::GetNet).
        assert match_pads(["[5]"], pads("5"), True) == (["[5]"], ())
        assert match_pads(["[5]"], pads("5"), False) == (["[5]"], (0,))


NETLIST = """(export (version "E")
  (components
    (comp (ref "R1") (value "1k") (footprint "Lib:Two"))
    (comp (ref "U1") (value "X") (footprint "Lib:Test"))
    (comp (ref "J1") (value "C") (footprint ""))
    (comp (ref "J2") (value "C") (footprint "Nope:X"))
    (comp (ref "J3") (value "C") (footprint "NoLib"))
    (comp (ref "J4") (value "C") (footprint "Lib:Missing"))
    (comp (ref "R1") (value "2k") (footprint "Lib:Test")))
  (nets
    (net (code "1") (name "A")
      (node (ref "R1") (pin "1") (pinfunction "A") (pintype "passive"))
      (node (ref "U1") (pin "3") (pinfunction "OUT") (pintype "output"))
      (node (ref "X9") (pin "1")))
    (net (code "2") (name "B")
      (node (ref "R1") (pin "2") (pintype "passive"))
      (node (ref "U1") (pin "") (pinfunction "NC") (pintype "no_connect")))
    (net (code "0") (name "")
      (node (ref "R1") (pin "9")))))
"""


class TestReadBoardNetlist:
    def test_components_and_pins(self):
        components = read_board_netlist(NETLIST)
        assert [(c.reference, c.value, c.footprint) for c in components] == [
            ("R1", "1k", "Lib:Two"),  # The first component of a reference.
            ("U1", "X", "Lib:Test"),
            ("J1", "C", ""),
            ("J2", "C", "Nope:X"),
            ("J3", "C", "NoLib"),
            ("J4", "C", "Lib:Missing"),
        ]
        # A net with the code 0 adds no pin. A node of an other reference is not used.
        assert components[0].pins == [
            NetlistPin("1", "A", "passive"),
            NetlistPin("2", "", "passive"),
        ]
        assert components[1].pins == [
            NetlistPin("3", "OUT", "output"),
            NetlistPin("", "NC", "no_connect"),
        ]


PADS = {
    "Two": (PadNumber("1", True), PadNumber("2", True)),
    "Test": (PadNumber("1", True), PadNumber("2", True), PadNumber("4", True), PadNumber("", True)),
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A project with a fake kicad-cli that writes NETLIST, and a fake pcbnew."""
    footprints = make_footprints(tmp_path, monkeypatch)
    (footprints["pretty"] / "Two.kicad_mod").write_text('(footprint "Two")\n', encoding="utf-8")
    state = {"netlist": NETLIST, "stderr": b"", "code": 0, "reader": "10.0.6", "pads": PADS}

    def kicad_cli(args, timeout, english):
        state["args"] = args
        if state["code"] == 0:
            output = args[args.index("--output") + 1]
            with open(output, "w", encoding="utf-8") as file:
                file.write(state["netlist"])
        return subprocess.CompletedProcess(args, state["code"], b"", state["stderr"])

    async def read_pads(items, embedded=False):
        state["items"] = items
        pads = [state["pads"].get(name) for _library, name in items]
        return state["reader"], [None if p is None else FootprintItems(p, (), ()) for p in pads]

    monkeypatch.setattr(kicad_cli_module, "run_kicad_cli_sync", kicad_cli)
    monkeypatch.setattr(library_tools, "read_footprints", read_pads)
    footprints.update(state=state, schematic=footprints["project"] / "board.kicad_sch")
    return footprints


def _item(reference: str, unit: int = 1) -> AnnotationItem:
    return AnnotationItem(
        reference, unit, "", Path("a.kicad_sch"), "/", "/", None, (0, 0), "R", None
    )


class TestAnnotationReferences:
    def test_changed_references(self):
        duplicate = AnnotationError(
            "duplicate_reference", "Duplicate items R1", _item("R1"), _item("r1")
        )
        mismatch = AnnotationError("unit_value_mismatch", "x", _item("U1"), _item("U1", 2))
        check = AnnotationCheck(
            [duplicate, mismatch],
            [],
            # Two items of "C?" share one component; one item of "L?" has its own.
            unannotated=[_item("C?"), _item("C?", 2), _item("L?")],
            extra_units=[_item("U5", 3)],
        )
        assert library_tools.annotation_references(check) == {"r1", "c?", "u5"}

    def test_result_lines(self):
        result = library_tools._annotation_result
        # The errors of a changed symbol keep the result line of the counts.
        assert result("**Result**: ❌ 1 pin number with no pad", 3, 1, True, True) == (
            "**Result**: ❌ 1 pin number with no pad; 1 symbol has an annotation error: the "
            "result of this symbol is not complete."
        )
        assert result(None, 3, 2, True, True) == (
            "**Result**: ⚠️ No error in the 3 other symbols; 2 symbols have an annotation "
            "error: the result of these symbols is not complete."
        )
        assert result(None, 3, 0, True, False).startswith(
            "**Result**: ⚠️ No error in the 3 symbols;"
        )
        assert result(None, 3, 0, True, True) is None  # only units with different values
        assert result(None, 3, 0, False, True) is None


class TestCheckSchematicPinPadMapping:
    async def test_result(self, board):
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert out.startswith("# Pins and pads of the schematic board.kicad_sch")
        assert board["state"]["args"][:5] == ["sch", "export", "netlist", "--format", "kicadsexpr"]
        assert board["state"]["args"][-1] == str(board["schematic"])
        assert board["state"]["items"] == [(board["pretty"], "Two"), (board["pretty"], "Test")]
        assert "**Symbols**: 2 checked, 4 not checked" in out
        assert (
            "**Result**: ❌ 1 pin number with no pad, 1 pin with no number, 4 symbols that the "
            "tool did not check, 3 pads with no pin"
        ) in out
        assert table_rows(section(out, "Symbols with errors or warnings (1)")) == [
            ["U1", "X", "Lib:Test", "3 (OUT)", "NC", "1, 2, 4"]
        ]
        rows = table_rows(section(out, "Symbols that the tool did not check (4)"))
        assert [row[1] for row in rows] == ["J1", "J2", "J3", "J4"]
        assert rows[0][0] == 'No footprint ("No footprint defined for symbol").'
        assert rows[1][0].startswith('KiCad does not find the footprint "Nope:X": no footprint')
        assert rows[2][0] == 'The footprint ID "NoLib" has no library name.'
        assert 'has no footprint "Missing"' in rows[3][0]
        assert section(out, "Symbols with no error and no warning (1)").endswith("R1")
        assert "Annotation" not in out

    async def test_references(self, board):
        out = await library_tools.check_schematic_pin_pad_mapping(
            str(board["schematic"]), ["R1", "X9"]
        )
        assert "**Symbols**: 1 checked, 0 not checked" in out
        assert "✅ Each pin number has a pad" in out
        assert "_Not in the netlist: X9 (a power symbol" in out
        assert board["state"]["items"] == [(board["pretty"], "Two")]

    async def test_duplicate_pads_of_one_number(self, board):
        board["state"]["pads"] = {
            **PADS,
            "Two": (
                PadNumber("1", True),
                PadNumber("2", True),
                PadNumber("9", True),
                PadNumber("9", True),
            ),
        }
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]), ["R1"])
        assert "**Result**: ⚠️ 2 pads with no pin" in out
        assert table_rows(section(out, "Symbols with errors or warnings (1)"))[0][5] == "9 (2 pads)"

    async def test_annotation_errors(self, board):
        board["state"]["stderr"] = (
            b"Warning: schematic has annotation errors, please use the schematic editor to "
            b"fix them\n"
        )
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert "⚠️ **Annotation**: " in out
        assert "kicad-cli (KiCad 10.0.6) reports annotation errors in the netlist." in out
        # The schematic of the fixture is not a schematic: the check of the server does not run.
        assert "⚠️ The server cannot list the annotation errors: KiCad cannot load" in out
        assert "the server does not know the symbols that they change" in out
        assert "❌" not in out.split("**Annotation**")[1]

    async def test_annotation_errors_are_listed(self, board):
        board["state"]["stderr"] = b"Warning: schematic has annotation errors\n"
        uuid = "11111111-1111-1111-1111-111111111111"
        symbols = "".join(
            f'  (symbol (lib_id "Device:R") (at {x} 0 0) (unit 1)\n'
            '    (property "Reference" "R1" (at 0 0 0)) (property "Value" "1k" (at 0 0 0))\n'
            f'    (instances (project "board" (path "/{uuid}" (reference "R1") (unit 1)))))\n'
            for x in (0, 10)
        )
        board["schematic"].write_text(
            f'(kicad_sch (version 20260306) (generator "eeschema") (uuid "{uuid}")\n'
            '  (lib_symbols (symbol "Device:R" (symbol "R_1_1")))\n'
            f"{symbols})\n",
            encoding="utf-8",
        )
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert "- duplicate_reference: Duplicate items R1 (sheet /)" in out
        assert "❌" not in out.split("**Annotation**")[1].split("\n\n")[0]
        # The netlist component R1 can have the pins of the two symbols: its result is
        # not complete, and it is not a symbol with no error.
        assert (
            "; 1 symbol has an annotation error: the result of this symbol is not complete." in out
        )
        assert table_rows(section(out, "Symbols with an annotation error (1)")) == [
            ["R1", "1k", "Lib:Two", "none", "none", "none"]
        ]
        assert "Symbols with no error and no warning" not in out
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]), ["R1"])
        assert "**Result**: ⚠️ 1 symbol has an annotation error: the result of this symbol" in out
        assert "✅" not in out

    async def test_footprint_that_kicad_cannot_load(self, board):
        board["state"]["pads"] = {"Two": None}
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]), ["R1"])
        rows = table_rows(section(out, "Symbols that the tool did not check (1)"))
        assert rows == [
            [
                'KiCad cannot load the file of the footprint "Lib:Two". Open it in the '
                "footprint editor to see the error.",
                "R1",
            ]
        ]

    async def test_pcbnew_of_an_other_kicad(self, board):
        board["state"]["reader"] = "9.0.9"
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert out.startswith(
            "❌ kicad-cli is KiCad 10.0.6, but the Python with pcbnew is KiCad 9.0.9."
        )

    async def test_nightly_build(self, board, monkeypatch):
        set_version(board, monkeypatch, "10.99.0")
        board["state"]["reader"] = "10.99.0"
        board["state"]["netlist"] = NETLIST.replace('(pin "3")', '(pin "[3]")')
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert (
            out.startswith("❌ The installed KiCad is the nightly build 10.99.0.")
            and '"[3]"' in out
        )

    async def test_symbols_of_one_reason(self, board):
        board["state"]["netlist"] = NETLIST.replace('"Nope:X"', '""')
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        rows = table_rows(section(out, "Symbols that the tool did not check (4)"))
        assert rows[0] == ['No footprint ("No footprint defined for symbol").', "J1, J2"]

    async def test_too_many_stacked_numbers_of_one_symbol(self, board):
        board["state"]["netlist"] = NETLIST.replace('(pin "3")', '(pin "[1-200000]")')
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        rows = table_rows(section(out, "Symbols that the tool did not check (5)"))
        assert ['The pin number "[1-200000]" gives more than 100000 pin numbers.', "U1"] in rows
        assert section(out, "Symbols with no error and no warning (1)").endswith("R1")

    async def test_duplicate_references(self, board):
        out = await library_tools.check_schematic_pin_pad_mapping(
            str(board["schematic"]), ["X9", "R1", "X9"]
        )
        assert "_Not in the netlist: X9 (a power symbol" in out

    @pytest.fixture
    def two_tops(self, board):
        """A KiCad 10 project with the top-level sheets board.kicad_sch and second.kicad_sch."""
        second = board["project"] / "second.kicad_sch"
        for path in (board["schematic"], second):
            path.write_text(
                '(kicad_sch (version 20260306) (generator "eeschema") (paper "A4"))\n',
                encoding="utf-8",
            )
        (board["project"] / "board.kicad_pro").write_text(
            '{"schematic": {"top_level_sheets": ['
            '{"filename": "board.kicad_sch", "name": "board"},'
            '{"filename": "second.kicad_sch", "name": "second"}]}}',
            encoding="utf-8",
        )
        return second

    async def test_top_level_sheets_of_kicad_10_0_6(self, board, two_tops):
        out = await library_tools.check_schematic_pin_pad_mapping(str(two_tops))
        assert out.startswith("❌ The project") and "has 2 top-level sheets" in out
        assert "KiCad 10.0.7 and later write the netlist of all top-level sheets." in out

    async def test_top_level_sheets_of_kicad_10_0_7(self, board, two_tops, monkeypatch):
        set_version(board, monkeypatch, "10.0.7")
        board["state"]["reader"] = "10.0.7"
        out = await library_tools.check_schematic_pin_pad_mapping(str(two_tops))
        # kicad-cli gets the schematic of the project name: it loads all top-level sheets.
        assert board["state"]["args"][-1] == str(board["schematic"])
        assert f", with 2 top-level sheets (the project of {two_tops})" in out

    async def test_unknown_kicad(self, board, monkeypatch):
        set_version(board, monkeypatch, "11.0.0")
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert out.startswith("❌ The installed KiCad is 11.0.0.")
        assert "args" not in board["state"]

    async def test_top_level_sheet_with_an_other_name(self, board):
        main = board["project"] / "main.kicad_sch"
        main.write_text('(kicad_sch (version 20260306) (generator "eeschema"))\n', "utf-8")
        (board["project"] / "board.kicad_pro").write_text(
            '{"schematic": {"top_level_sheets": [{"filename": "main.kicad_sch", "name": "m"}]}}',
            encoding="utf-8",
        )
        out = await library_tools.check_schematic_pin_pad_mapping(str(main))
        assert out.startswith("❌ The top-level sheet of the project") and "main.kicad_sch" in out

    async def test_kicad_cli_error(self, board):
        board["state"].update(code=1, stderr=b"Failed to load schematic")
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["schematic"]))
        assert out.startswith("❌ kicad-cli did not give the netlist of")
        assert "exit code 1: Failed to load schematic" in out

    @pytest.mark.parametrize("name", ["board.kicad_pro", "nope.kicad_sch"])
    async def test_not_a_schematic(self, board, name):
        out = await library_tools.check_schematic_pin_pad_mapping(str(board["project"] / name))
        assert out.startswith("❌ No schematic file (.kicad_sch) at")
