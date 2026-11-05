"""Tests for parsers/board_models.py and the tool check_library_links."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from kicad_workbench.parsers.board_models import BoardFootprint, board_models
from kicad_workbench.parsers.board_netlist import NetlistComponent
from kicad_workbench.tools import library as library_tools
from kicad_workbench.tools import library_links
from kicad_workbench.utils import kicad_cli as kicad_cli_module
from kicad_workbench.utils.library_footprint import FootprintItems, PadNumber
from kicad_workbench.utils.schematic_settings import rule_severities, unknown_severity_reason

from .test_library_footprint import make_footprints
from .test_library_search import table_rows
from .test_pin_pad_mapping import NETLIST, section

BOARD = """(kicad_pcb (version 20241229) (generator "pcbnew")
  (footprint "Lib:Two" (layer "F.Cu")
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (model "${KIPRJMOD}/m.step" (offset (xyz 0 0 0)))
    (model "gone.step" (hide yes)))
  (footprint "Lib:Test" (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (embedded_files (file (name "f.step") (type model)))
    (model "kicad-embed://f.step" hide)
    (model "kicad-embed://b.step" (hide no))
    (model "kicad-embed://none.step" (hide)))
  (footprint "Nope:X" (layer "F.Cu")
    (property "Reference" "J9" (at 0 0 0) (layer "F.SilkS"))
    (model "x.step"))
  (footprint "R_0603" (layer "F.Cu")
    (property "Reference" "R9" (at 0 0 0) (layer "F.SilkS"))
    (model "y.step"))
  (embedded_files (file (name "b.step") (type model))))
"""


class TestBoardModels:
    def test_footprints(self):
        footprints, embedded = board_models(BOARD)
        assert embedded == ("b.step",)
        assert footprints == [
            BoardFootprint(
                "R1", "Lib:Two", (("${KIPRJMOD}/m.step", True), ("gone.step", False)), ()
            ),
            BoardFootprint(
                "U1",
                "Lib:Test",
                (
                    ("kicad-embed://f.step", False),  # A bare hide (older files).
                    ("kicad-embed://b.step", True),
                    ("kicad-embed://none.step", False),  # (hide) with no value is yes.
                ),
                ("f.step",),
            ),
            BoardFootprint("J9", "Nope:X", (("x.step", True),), ()),
            BoardFootprint("R9", "R_0603", (("y.step", True),), ()),
        ]


class TestSeverity:
    @pytest.mark.parametrize(
        "text, severity",
        [
            (None, "warning"),  # No project file: the default.
            ("not json", "warning"),
            ("{}", "warning"),
            ('{"erc": {"rule_severities": []}}', "warning"),
            ('{"erc": {"rule_severities": {"lib_symbol_issues": "ignore"}}}', "ignore"),
            ('{"erc": {"rule_severities": {"lib_symbol_issues": "Error"}}}', "error"),
            ('{"erc": {"rule_severities": {"lib_symbol_issues": "warning"}}}', "warning"),
            # KiCad reads only its keys: "x" is not one of them.
            ('{"erc": {"rule_severities": {"lib_symbol_issues": "ignore", "x": 1}}}', "ignore"),
            # A value that is not a text stops the read: the keys after it keep the default.
            ('{"erc": {"rule_severities": {"lib_symbol_issues": "ignore", "pin_not_connected": 1}}}',
             "warning"),
            ('{"erc": {"rule_severities": {"footprint_filter": 1, "lib_symbol_issues": "ignore"}}}',
             "ignore"),
        ],
    )  # fmt: skip
    def test_severity(self, tmp_path, text, severity):
        project = tmp_path / "p.kicad_pro"
        if text is not None:
            project.write_text(text, encoding="utf-8")
        defaults = {"lib_symbol_issues": "warning"}
        assert rule_severities(project, library_links._ERC_KEYS, defaults, "erc", "10.0.6") == {
            "lib_symbol_issues": severity
        }
        # A version with keys that the server does not know: a value that is not a text
        # makes the severity of a check with a text not known, except when the text gives
        # the default.
        found = rule_severities(project, library_links._ERC_KEYS, defaults, "erc", "10.99.0")
        expected = None if text and '": 1' in text else severity
        assert found == {"lib_symbol_issues": expected}


ERC = {
    "sheets": [
        {
            "path": "/",
            "violations": [
                {
                    "type": "lib_symbol_issues",
                    "severity": "warning",
                    "description": "The current configuration does not include the symbol "
                    "library 'Gone'",
                    "items": [{"description": "Symbol R1 [1k]"}],
                },
                {
                    "type": "lib_symbol_issues",
                    "severity": "warning",
                    "description": "The current configuration does not include the symbol "
                    "library 'Gone'",
                    "items": [{"description": "Symbol U1 [X]"}],
                },
                {
                    "type": "lib_symbol_mismatch",
                    "severity": "warning",
                    "excluded": True,
                    "description": "Symbol 'X' doesn't match copy in library 'Lib'",
                    "items": [{"description": "Symbol J1 [C]"}],
                },
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [],
                },
            ],
        }
    ]
}
DRC = {
    "violations": [
        {
            "type": "lib_footprint_mismatch",
            "severity": "warning",
            "description": "Footprint 'Two' does not match copy in library 'Lib'",
            "items": [{"description": "Footprint R1", "pos": {"x": 0, "y": 0}}],
        }
    ],
    "unconnected_items": [],
    "schematic_parity": [],
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with a schematic and a board, a fake kicad-cli and a fake pcbnew."""
    tables = make_footprints(tmp_path, monkeypatch)
    (tables["pretty"] / "Two.kicad_mod").write_text('(footprint "Two")\n', encoding="utf-8")
    directory = tables["project"]
    (directory / "board.kicad_pcb").write_text(BOARD, encoding="utf-8")
    (directory / "m.step").write_text("step", encoding="utf-8")
    state = {"erc": ERC, "drc": DRC, "calls": []}

    def kicad_cli(args, timeout, english):
        state["calls"].append(args[:2])
        output = args[args.index("--output") + 1]
        texts = {
            ("sch", "export"): NETLIST,
            ("sch", "erc"): json.dumps(state["erc"]),
            ("pcb", "drc"): json.dumps(state["drc"]),
        }
        with open(output, "w", encoding="utf-8") as file:
            file.write(texts[tuple(args[:2])])
        return subprocess.CompletedProcess(args, 0, b"", b"")

    two = FootprintItems(
        (PadNumber("1", True), PadNumber("2", True)), (("${KIPRJMOD}/m.step", True),), ()
    )
    test = FootprintItems((PadNumber("1", True),), (("lost.step", True), ("", True)), ())

    async def read_footprints(items, embedded=False):
        assert embedded
        return "10.0.6", [{"Two": two, "Test": test}.get(name) for _library, name in items]

    async def version():
        return "10.0.6"

    monkeypatch.setattr(kicad_cli_module, "run_kicad_cli_sync", kicad_cli)
    monkeypatch.setattr(library_links, "run_kicad_cli_sync", kicad_cli)
    monkeypatch.setattr(library_tools, "read_footprints", read_footprints)
    monkeypatch.setattr(library_links, "get_kicad_version", version)
    tables.update(state=state, directory=directory)
    return tables


class TestCheckLibraryLinks:
    async def test_links(self, project):
        out = await library_links.check_library_links(str(project["directory"]))
        assert out.startswith("# Library links of the project board")
        assert sorted(map(tuple, project["state"]["calls"])) == [
            ("pcb", "drc"),
            ("sch", "erc"),
            ("sch", "export"),
        ]
        assert (
            "**Result**: ❌ 2 symbols with a library problem, 4 symbols whose footprint KiCad "
            "does not find, 2 3D models of the schematic footprints not found, 1 board "
            "footprint with a library problem, 2 3D models of the board footprints not found"
        ) in out
        # One row for each problem text. An excluded violation is not counted.
        assert table_rows(section(out, "Schematic symbols (ERC of KiCad)")) == [
            [
                "lib_symbol_issues",
                "warning",
                "The current configuration does not include the symbol library 'Gone'",
                "R1, U1",
            ],
            [
                "lib_symbol_mismatch",
                "warning (excluded)",
                "Symbol 'X' doesn't match copy in library 'Lib'",
                "J1",
            ],
        ]
        rows = table_rows(section(out, "Schematic footprints"))
        assert [row[1] for row in rows] == ["J1", "J2", "J3", "J4"]
        assert "Models: 1 found, 1 not found, 1 no file name" in out
        assert table_rows(section(out, "3D models of the schematic footprints")) == [
            [
                "U1",
                "Lib:Test",
                "lost.step",
                "yes",
                "not found",
                "KiCad first looks for a relative path in its working directory, which this "
                "server does not know.",
            ],
            ["U1", "Lib:Test", "", "yes", "no file name", ""],
        ]
        assert table_rows(section(out, "Board footprints (DRC of KiCad)")) == [
            [
                "lib_footprint_mismatch",
                "warning",
                "Footprint 'Two' does not match copy in library 'Lib'",
                "R1",
            ]
        ]
        rows = table_rows(section(out, "3D models of the board footprints"))
        # The result does not count the 2 hidden models. The board embeds b.step. Nope is no library, and R_0603 has no library
        # nickname: no library path.
        assert [row[:5] for row in rows] == [
            ["R1", "Lib:Two", "gone.step", "no", "not found"],
            ["U1", "Lib:Test", "kicad-embed://none.step", "no", "not found"],
            ["J9", "Nope:X", "x.step", "yes", "not found"],
            ["R9", "R_0603", "y.step", "yes", "not found"],
        ]
        assert rows[1][5] == 'The footprint and the board have no embedded file "none.step".'

    async def test_ignored_checks(self, project):
        (project["directory"] / "board.kicad_pro").write_text(
            '{"erc": {"rule_severities": {"lib_symbol_issues": "ignore"}},'
            ' "board": {"design_settings": {"rule_severities": {"lib_footprint_issues": 1}}}}',
            encoding="utf-8",
        )
        project["state"].update(erc={"sheets": []}, drc={"violations": []})
        out = await library_links.check_library_links(str(project["directory"]))
        assert "_The project ignores the check lib_symbol_issues: KiCad does not run it._" in out
        # KiCad cannot read the value 1: the check keeps its default severity.
        assert "lib_footprint_issues is not known" not in out
        assert "lib_footprint_issues: KiCad does not run it" not in out

    def test_checks_with_a_severity_that_is_not_known(self, tmp_path):
        # A nightly build: one note for all checks whose severity depends on the order.
        path = tmp_path / "p.kicad_pro"
        severities = {"lib_symbol_issues": "ignore", "lib_symbol_mismatch": "error", "x": 1}
        path.write_text(json.dumps({"erc": {"rule_severities": severities}}), encoding="utf-8")
        lines, count = library_links._check_lines(
            "ERC", [], library_links._ERC_TYPES, path, library_links._ERC_KEYS, "erc", "10.99.0"
        )
        reason = unknown_severity_reason("10.99.0")
        assert lines[3:] == [
            "_The severities of the checks lib_symbol_issues, lib_symbol_mismatch are not "
            f"known: {reason}._",
            "✅ KiCad reports no problem.",
        ]
        assert count == 0

    async def test_board_only(self, project):
        (project["directory"] / "board.kicad_sch").unlink()
        out = await library_links.check_library_links(str(project["directory"] / "board.kicad_pcb"))
        assert "**Schematic**: none (board.kicad_sch does not exist)" in out
        assert "## Schematic symbols" not in out
        assert project["state"]["calls"] == [["pcb", "drc"]]

    async def test_no_schematic_and_no_board(self, project):
        (project["directory"] / "board.kicad_sch").unlink()
        (project["directory"] / "board.kicad_pcb").unlink()
        out = await library_links.check_library_links(str(project["directory"]))
        assert out.startswith("❌ The project") and "has no schematic and no board" in out

    async def test_kicad_cli_error(self, project, monkeypatch):
        def failing(args, timeout, english):
            return subprocess.CompletedProcess(args, 3, b"", b"Failed to load")

        monkeypatch.setattr(library_links, "run_kicad_cli_sync", failing)
        out = await library_links.check_library_links(str(project["directory"]))
        # A failed check stops only its section.
        assert "❌ ERC failed: kicad-cli stopped with the exit code 3: Failed to load" in out
        assert "❌ DRC failed: kicad-cli stopped with the exit code 3: Failed to load" in out
        assert "schematic symbols not checked" in out and "board footprints not checked" in out
        assert "1 schematic symbols" not in out  # a flag, not a count
        assert "## 3D models of the board footprints" in out

    async def test_schematic_error_stops_only_the_schematic(self, project):
        (project["directory"] / "board.kicad_pro").write_text(
            '{"schematic": {"top_level_sheets": [{"filename": "main.kicad_sch"}]}}',
            encoding="utf-8",
        )
        (project["directory"] / "main.kicad_sch").write_text("", encoding="utf-8")
        out = await library_links.check_library_links(str(project["directory"]))
        assert "❌ The top-level sheet of the project" in section(out, "Schematic")
        assert "## Board footprints (DRC of KiCad)" in out

    async def test_given_board(self, project):
        other = project["directory"] / "rev2.kicad_pcb"
        other.write_text('(kicad_pcb (version 20241229) (generator "pcbnew"))\n', "utf-8")
        out = await library_links.check_library_links(str(other))
        assert f"**Board**: {other}" in out
        assert "_The footprints have no 3D model._" in out


class TestAnnotationNote:
    """With an annotation error, the footprints and the 3D models get no pass mark."""

    @pytest.mark.parametrize(
        "warning, errors, references, blocked, starts",
        [
            (False, [], set(), False, None),  # no annotation error
            # kicad-cli warns, and the server checks did not run.
            (True, None, None, None, "The server does not know the netlist components"),
            (True, ["e"], {"r1"}, True, "The annotation errors change the netlist"),
            # A symbol that is not annotated, or units with different values: no component
            # changes, but "Update PCB from Schematic" opens the annotation dialog.
            (True, ["e"], set(), True, '"Update PCB from Schematic" finds annotation errors'),
            (True, ["e"], set(), False, "The netlist of kicad-cli has annotation errors."),
            # Only "Update PCB from Schematic" finds errors.
            (False, [], set(), True, '"Update PCB from Schematic" finds annotation errors'),
        ],
    )
    def test_annotation_note(self, warning, errors, references, blocked, starts):
        found = SimpleNamespace(
            annotation=warning,
            annotation_errors=errors,
            annotation_references=references,
            update_blocked=blocked,
        )
        text = library_links._annotation_note(found, "footprint")
        assert text is None if starts is None else text.startswith(starts)

    def test_footprints_with_a_note(self):
        found = SimpleNamespace(
            components=[NetlistComponent("R?", "1k", "Lib:Two", [])],
            reasons={},
            annotation=True,
            annotation_errors=["e"],
            annotation_references=set(),
            update_blocked=True,
        )
        lines, _count, _users = library_links._footprint_lines(found)
        assert lines[-1] == (
            "⚠️ KiCad finds the footprint of each symbol (1 footprint). "
            '"Update PCB from Schematic" finds annotation errors: it opens the annotation '
            "dialog, and it runs only when no error stays."
        )

    def test_models_with_a_note(self):
        assert library_links._model_lines("T", [], "Symbols", {}, "N.")[-1] == (
            "_The footprints have no 3D model._"
        )
        model = library_links._Model(
            "R1", "Lib:Two", "m.step", True, SimpleNamespace(state="found")
        )
        lines = library_links._model_lines("T", [model], "Symbols", {}, "N.")
        assert lines[-1] == "⚠️ KiCad finds the file of each 3D model. N."
        lines = library_links._model_lines("T", [model], "Symbols", {}, None)
        assert lines[-1] == "✅ KiCad finds the file of each 3D model."
