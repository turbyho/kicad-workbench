"""Tests for visualization export tools (render / STEP / schematic SVG)."""

import asyncio
from pathlib import Path

import pytest

from kicad_workbench.tools import visualization
from kicad_workbench.tools.visualization import (
    export_pcb_3d,
    export_schematic_svg,
    render_pcb,
)


class _FakeResult:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def pcb_file(tmp_path):
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240130))", encoding="utf-8")
    return f


@pytest.fixture
def sch_file(tmp_path):
    f = tmp_path / "root.kicad_sch"
    f.write_text("(kicad_sch (version 20231120))", encoding="utf-8")
    return f


class TestRenderPcb:
    def test_args_include_input_file(self, monkeypatch, pcb_file, tmp_path):
        """Regression: the input file MUST be the last positional arg — an
        earlier version omitted it and kicad-cli failed with rc=1."""
        captured = {}

        async def fake(args, timeout=60.0):
            captured["args"] = args
            return _FakeResult()

        monkeypatch.setattr(visualization, "run_kicad_cli", fake)
        out = tmp_path / "r.png"
        result = asyncio.run(render_pcb(str(pcb_file), side="top", output_path=str(out)))
        assert captured["args"][0:2] == ["pcb", "render"]
        assert captured["args"][-1] == str(pcb_file)
        assert "--side" in captured["args"]
        assert isinstance(result, object)  # Image or str, not an exception

    def test_rejects_invalid_side(self, pcb_file):
        result = asyncio.run(render_pcb(str(pcb_file), side="diagonal"))
        assert isinstance(result, str) and "Invalid side" in result

    def test_rejects_invalid_quality(self, pcb_file):
        result = asyncio.run(render_pcb(str(pcb_file), quality="ultra"))
        assert isinstance(result, str) and "Invalid quality" in result

    def test_missing_pcb_file(self, tmp_path):
        result = asyncio.run(render_pcb(str(tmp_path / "nope.kicad_pcb")))
        assert isinstance(result, str) and "not found" in result

    def test_rotate_appended_before_input(self, monkeypatch, pcb_file, tmp_path):
        captured = {}

        async def fake(args, timeout=60.0):
            captured["args"] = args
            return _FakeResult()

        monkeypatch.setattr(visualization, "run_kicad_cli", fake)
        asyncio.run(
            render_pcb(str(pcb_file), output_path=str(tmp_path / "r.png"), rotate="-60,0,30")
        )
        args = captured["args"]
        assert args[-3:-1] == ["--rotate", "-60,0,30"]
        assert args[-1] == str(pcb_file)


class TestExportStep:
    def test_args_include_input_file(self, monkeypatch, pcb_file, tmp_path):
        captured = {}

        async def fake(args, timeout=60.0):
            captured["args"] = args
            return _FakeResult()

        monkeypatch.setattr(visualization, "run_kicad_cli", fake)
        out = tmp_path / "b.step"
        # create the file so the success path sees it
        out.write_bytes(b"ISO-10303-21;")
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(out)))
        assert captured["args"][0:4] == ["pcb", "export", "step", "--output"]
        assert captured["args"][-1] == str(pcb_file)
        assert "✅" in result

    def test_board_only_inserted_before_input(self, monkeypatch, pcb_file, tmp_path):
        captured = {}

        async def fake(args, timeout=60.0):
            captured["args"] = args
            return _FakeResult()

        monkeypatch.setattr(visualization, "run_kicad_cli", fake)
        out = tmp_path / "b.step"
        out.write_bytes(b"ISO-10303-21;")
        asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(out), board_only=True))
        assert captured["args"][-2] == "--board-only"
        assert captured["args"][-1] == str(pcb_file)


class _SvgRun:
    """A kicad-cli that writes one SVG file for each page."""

    def __init__(self, names_by_page, code=0):
        self.names_by_page = names_by_page
        self.code = code
        self.calls = []

    def __call__(self, args, timeout=60.0, english=False):
        self.calls.append(args)
        out = Path(args[args.index("--output") + 1])
        if self.code == 0:
            for page, name in self.names_by_page.items():
                (out / name).write_text(f"<svg>{page}</svg>", encoding="utf-8")
        return _FakeRun(self.code)


class _FakeRun:
    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b"" if returncode == 0 else b"Error: cannot load"


ROOT_UUID = "11111111-1111-1111-1111-111111111111"


def _sheet(uuid, name, page):
    return (
        f'  (sheet (at {10 + int(page) * 40} 10) (size 20 20) (uuid "{uuid}")\n'
        f'    (property "Sheetname" "{name}" (at 10 9 0))\n'
        f'    (property "Sheetfile" "sub.kicad_sch" (at 10 31 0))\n'
        f'    (instances (project "root" (path "/{ROOT_UUID}" (page "{page}")))))\n'
    )


@pytest.fixture
def hierarchy(tmp_path):
    """root.kicad_sch with the sheet sub.kicad_sch two times (pages 2 and 3)."""
    root = tmp_path / "root.kicad_sch"
    root.write_text(
        f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "{ROOT_UUID}") '
        '(paper "A4")\n'
        + _sheet("22222222-2222-2222-2222-222222222222", "A", "2")
        + _sheet("33333333-3333-3333-3333-333333333333", "B", "3")
        + '  (sheet_instances (path "/" (page "1"))))\n',
        encoding="utf-8",
    )
    (tmp_path / "sub.kicad_sch").write_text(
        '(kicad_sch (version 20250114) (generator "eeschema") '
        '(uuid "44444444-4444-4444-4444-444444444444") (paper "A4"))\n',
        encoding="utf-8",
    )
    (tmp_path / "root.kicad_pro").write_text("{}\n", encoding="utf-8")
    return root


NAMES = {"1": "root.svg", "2": "root-A.svg", "3": "root-B.svg"}


class TestExportSchematicSvg:
    def test_the_root_gives_all_pages(self, monkeypatch, hierarchy, tmp_path):
        run = _SvgRun(NAMES)
        monkeypatch.setattr(visualization, "run_kicad_cli_sync", run)
        out = tmp_path / "out"
        out.mkdir()
        (out / "old.svg").write_text("<svg/>", encoding="utf-8")
        result = asyncio.run(export_schematic_svg(str(hierarchy), output_dir=str(out)))
        assert run.calls[0][:4] == ["sch", "export", "svg", "--output"]
        assert "--pages" not in run.calls[0] and run.calls[0][-1] == str(hierarchy)
        assert "**Files of this export (3):**\n- root-A.svg\n- root-B.svg\n- root.svg" in result
        # A file of an earlier export is not in the answer.
        assert "old.svg" not in result
        assert (out / "root-A.svg").read_text(encoding="utf-8") == "<svg>2</svg>"

    def test_a_sub_sheet_gives_the_pages_of_its_sheet_paths(self, monkeypatch, hierarchy, tmp_path):
        # kicad-cli of the sub-sheet alone gave one page with the references of
        # the Reference fields, not the two pages of the hierarchy. --pages of
        # kicad-cli 8.0.9 to 10.0.7 gives only the root page for SVG: the tool keeps
        # the files of the sheet paths of the file from the export of all pages.
        async def version():
            return "10.0.6"

        run = _SvgRun(NAMES)
        monkeypatch.setattr(visualization, "run_kicad_cli_sync", run)
        monkeypatch.setattr(visualization, "get_kicad_version", version)
        out = tmp_path / "out"
        result = asyncio.run(export_schematic_svg(str(tmp_path / "sub.kicad_sch"), str(out)))
        args = run.calls[0]
        assert "--pages" not in args and args[-1] == str(hierarchy), args
        assert "**Files of this export (2):**\n- root-A.svg\n- root-B.svg" in result, result
        assert sorted(p.name for p in out.iterdir()) == ["root-A.svg", "root-B.svg"]
        assert "the pages of its sheet paths in the hierarchy of `root.kicad_sch`" in result

    def test_a_name_of_two_sheet_paths(self, monkeypatch, hierarchy, tmp_path):
        # A sheet "A-B" of the root and a sheet "B" in a sheet "A" give one name.
        async def version():
            return "10.0.6"

        monkeypatch.setattr(visualization, "run_kicad_cli_sync", _SvgRun(NAMES))
        monkeypatch.setattr(visualization, "get_kicad_version", version)
        text = hierarchy.read_text(encoding="utf-8").replace('"Sheetname" "B"', '"Sheetname" "A"')
        hierarchy.write_text(text, encoding="utf-8")
        result = asyncio.run(export_schematic_svg(str(tmp_path / "sub.kicad_sch")))
        assert "⚠️ More than one sheet path gives the file name root-A.svg" in result, result

    def test_a_missing_page_is_no_complete_export(self, monkeypatch, hierarchy, tmp_path):
        async def version():
            return "10.0.6"

        names = {k: v for k, v in NAMES.items() if v != "root-B.svg"}
        monkeypatch.setattr(visualization, "run_kicad_cli_sync", _SvgRun(names))
        monkeypatch.setattr(visualization, "get_kicad_version", version)
        result = asyncio.run(export_schematic_svg(str(tmp_path / "sub.kicad_sch")))
        assert result.startswith("⚠️ Schematic SVG export is not complete."), result
        assert "⚠️ kicad-cli wrote no file root-B.svg" in result

    def test_two_top_level_sheets_with_kicad_10_0_7(self, monkeypatch, hierarchy, tmp_path):
        # kicad-cli 10.0.7 loads all top-level sheets: the file of a page starts with
        # the file name of its top-level sheet.
        async def version():
            return "10.0.7"

        (tmp_path / "extra.kicad_sch").write_text(
            '(kicad_sch (version 20250114) (generator "eeschema") '
            '(uuid "55555555-5555-5555-5555-555555555555") (paper "A4"))\n',
            encoding="utf-8",
        )
        (tmp_path / "root.kicad_pro").write_text(
            '{"schematic": {"top_level_sheets": ['
            f'{{"filename": "root.kicad_sch", "name": "root", "uuid": "{ROOT_UUID}"}}, '
            '{"filename": "extra.kicad_sch", "name": "Extra", "uuid": '
            '"66666666-6666-6666-6666-666666666666"}]}}',
            encoding="utf-8",
        )
        run = _SvgRun({**NAMES, "4": "extra.svg"})
        monkeypatch.setattr(visualization, "run_kicad_cli_sync", run)
        monkeypatch.setattr(visualization, "get_kicad_version", version)
        result = asyncio.run(export_schematic_svg(str(tmp_path / "extra.kicad_sch")))
        assert "**Files of this export (1):**\n- extra.svg" in result, result
        result = asyncio.run(export_schematic_svg(str(tmp_path / "sub.kicad_sch")))
        assert "**Files of this export (2):**\n- root-A.svg\n- root-B.svg" in result, result
        assert all(args[-1] == str(hierarchy) for args in run.calls)

    def test_the_file_name_of_a_sheet_path(self):
        assert visualization.svg_name(Path("/p/board.kicad_sch"), ()) == "board.svg"
        assert visualization.svg_name(Path("/p/board.kicad_sch"), ("a/b", "c\\d")) == (
            "board-a_b-c_d.svg"
        )

    def test_an_error_of_kicad_cli_changes_no_file(self, monkeypatch, hierarchy, tmp_path):
        monkeypatch.setattr(visualization, "run_kicad_cli_sync", _SvgRun(NAMES, code=1))
        out = tmp_path / "out"
        result = asyncio.run(export_schematic_svg(str(hierarchy), output_dir=str(out)))
        assert result.startswith("❌ kicad-cli wrote no SVG file (exit code 1)"), result
        assert "Error: cannot load" in result
        assert not out.exists()

    def test_missing_schematic(self, tmp_path):
        result = asyncio.run(export_schematic_svg(str(tmp_path / "nope.kicad_sch")))
        assert "not found" in result
