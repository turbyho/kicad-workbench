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


class _ExportRun:
    """A kicad-cli that writes the file of --output (or no file)."""

    def __init__(self, content=b"PNG", code=0, write=True, stderr=b"", stdout=b""):
        self.content, self.code, self.write = content, code, write
        self.stderr, self.stdout = stderr, stdout
        self.calls = []
        self.english = []

    def __call__(self, args, timeout=60.0, english=False):
        self.calls.append(args)
        self.english.append(english)
        if self.write:
            Path(args[args.index("--output") + 1]).write_bytes(self.content)
        result = _FakeRun(self.code, self.stderr)
        result.stdout = self.stdout
        return result


def _no_run(*args, **kwargs):
    pytest.fail("kicad-cli must not run")


@pytest.fixture
def cli(monkeypatch):
    """Set the kicad-cli of export_with_kicad_cli (utils/kicad_cli.py)."""
    from kicad_workbench.utils import kicad_cli

    def set_run(run):
        monkeypatch.setattr(kicad_cli, "run_kicad_cli_sync", run)
        return run

    return set_run


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


@pytest.mark.usefixtures("kicad_version")
class TestRenderPcb:
    def test_kicad_8_has_no_render(self, monkeypatch, cli, pcb_file):
        """kicad-cli 8.0.9 has no "pcb render" (its "pcb" has only drc and export)."""
        from kicad_workbench.utils import kicad_cli

        monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "8.0.9")
        cli(_no_run)
        result = asyncio.run(render_pcb(str(pcb_file)))
        assert result.startswith("❌ render_pcb needs KiCad 9 or later: kicad-cli 8.0.9"), result

    @pytest.mark.file_versions
    def test_a_kicad_8_board_with_kicad_8(self, monkeypatch, cli, tmp_path):
        """The version check of the board (KiCad 8 writes 20240108) passes, and the
        tool gives the step for KiCad 9."""
        from kicad_workbench.utils import file_versions, kicad_cli

        board = tmp_path / "b.kicad_pcb"
        board.write_text('(kicad_pcb (version 20240108) (generator "pcbnew"))\n', encoding="utf-8")
        monkeypatch.setattr(file_versions, "kicad_cli_version_sync", lambda: "8.0.9")
        monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "8.0.9")
        cli(_no_run)
        result = asyncio.run(render_pcb(str(board)))
        assert result.startswith("❌ render_pcb needs KiCad 9 or later: kicad-cli 8.0.9"), result
        assert "⚠️" not in result

    def test_an_old_image_is_not_the_result(self, cli, pcb_file, tmp_path):
        """kicad-cli exits with 0 and writes no file: the image of an earlier render
        is not the result, and the tool does not change it."""
        cli(_ExportRun(write=False))
        out = tmp_path / "r.png"
        out.write_bytes(b"old image")
        result = asyncio.run(render_pcb(str(pcb_file), output_path=str(out)))
        assert isinstance(result, str) and result.startswith("❌ PCB render failed"), result
        assert out.read_bytes() == b"old image"

    def test_the_error_of_kicad_cli(self, cli, pcb_file):
        cli(_ExportRun(code=1, write=False, stderr=b"Failed to load board"))
        result = asyncio.run(render_pcb(str(pcb_file)))
        assert result == (
            "❌ PCB render failed: kicad-cli stopped with the exit code 1: Failed to load board"
        ), result

    def test_args_and_the_image(self, cli, pcb_file, tmp_path):
        """The input file is the last argument. kicad-cli writes into a temporary
        folder in English, and the tool saves the image in the output file."""
        run = cli(_ExportRun(content=b"new image"))
        out = tmp_path / "r.png"
        result = asyncio.run(render_pcb(str(pcb_file), side="bottom", output_path=str(out)))
        args = run.calls[0]
        assert args[0:2] == ["pcb", "render"] and run.english == [True]
        target = Path(args[args.index("--output") + 1])
        assert target.name == "output.png"
        assert target.parent != out.parent and not target.parent.exists()
        assert args[args.index("--side") + 1] == "bottom"
        assert args[-1] == str(pcb_file)
        assert isinstance(result, visualization.Image) and result.data == b"new image"
        assert result.to_image_content().mimeType == "image/png"
        assert out.read_bytes() == b"new image"

    @pytest.mark.parametrize("name", ["r.jpg", "r.JPEG"])
    def test_a_jpeg_image(self, cli, pcb_file, tmp_path, name):
        """command_pcb_render.cpp (KiCad 9 and 10) writes a JPEG for a file name that
        ends with .jpg or .jpeg (in lower case)."""
        run = cli(_ExportRun(content=b"jpeg data"))
        out = tmp_path / name
        result = asyncio.run(render_pcb(str(pcb_file), output_path=str(out)))
        target = Path(run.calls[0][run.calls[0].index("--output") + 1])
        assert target.suffix == out.suffix.lower()
        assert isinstance(result, visualization.Image)
        assert result.to_image_content().mimeType == "image/jpeg"
        assert out.read_bytes() == b"jpeg data"

    def test_the_default_output_file(self, cli, pcb_file):
        cli(_ExportRun())
        asyncio.run(render_pcb(str(pcb_file), side="left"))
        assert (pcb_file.parent / "board_render_left.png").read_bytes() == b"PNG"

    def test_an_output_file_of_an_other_type(self, cli, pcb_file, tmp_path):
        cli(_no_run)
        result = asyncio.run(render_pcb(str(pcb_file), output_path=str(tmp_path / "r.kicad_pcb")))
        assert result.startswith("❌ The output file"), result
        assert "must have the extension .png or .jpg or .jpeg" in result

    def test_rejects_invalid_side(self, pcb_file):
        result = asyncio.run(render_pcb(str(pcb_file), side="diagonal"))
        assert isinstance(result, str) and "Invalid side" in result

    def test_rejects_invalid_quality(self, pcb_file):
        result = asyncio.run(render_pcb(str(pcb_file), quality="ultra"))
        assert isinstance(result, str) and "Invalid quality" in result

    def test_missing_pcb_file(self, tmp_path):
        result = asyncio.run(render_pcb(str(tmp_path / "nope.kicad_pcb")))
        assert isinstance(result, str) and "not found" in result

    def test_rotate(self, cli, pcb_file, tmp_path):
        run = cli(_ExportRun())
        asyncio.run(
            render_pcb(str(pcb_file), output_path=str(tmp_path / "r.png"), rotate="-60,0,30")
        )
        args = run.calls[0]
        assert args[args.index("--rotate") + 1] == "-60,0,30"
        assert args[-1] == str(pcb_file)


class TestExportStep:
    def test_args_and_the_file(self, cli, pcb_file, tmp_path):
        run = cli(_ExportRun(content=b"ISO-10303-21;"))
        out = tmp_path / "b.step"
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(out)))
        args = run.calls[0]
        assert args[0:4] == ["pcb", "export", "step", "--force"] and run.english == [True]
        assert args[-1] == str(pcb_file) and "--board-only" not in args
        assert result.startswith("✅") and f"**Output:** {out}" in result
        assert result.endswith("## Messages of kicad-cli\n\n```\n(no messages)\n```")
        assert out.read_bytes() == b"ISO-10303-21;"

    def test_board_only(self, cli, pcb_file, tmp_path):
        run = cli(_ExportRun(content=b"ISO-10303-21;"))
        out = tmp_path / "b.step"
        asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(out), board_only=True))
        assert "--board-only" in run.calls[0] and run.calls[0][-1] == str(pcb_file)

    def test_an_old_model_is_not_the_result(self, cli, pcb_file, tmp_path):
        cli(_ExportRun(write=False))
        out = tmp_path / "b.step"
        out.write_bytes(b"old model")
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(out)))
        assert result.startswith("❌ STEP export failed: kicad-cli stopped with the exit code 0")
        assert out.read_bytes() == b"old model"

    def test_a_message_with_no_reason(self, cli, pcb_file, tmp_path):
        """The line after the message is the reason only when it is a reason of
        EXPORTER_STEP::buildFootprint3DShapes."""
        stdout = b"Could not add 3D model for R1.\nCould not add 3D model for R2.\n"
        cli(_ExportRun(content=b"ISO-10303-21;", stdout=stdout))
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(tmp_path / "b.step")))
        assert "- R1: kicad-cli gave no reason\n- R2: kicad-cli gave no reason" in result

    def test_a_warning_of_the_save_is_no_success(self, cli, monkeypatch, pcb_file, tmp_path):
        """CLAUDE.md rule 7: no "✅" when a step after the save failed."""
        cli(_ExportRun(content=b"ISO-10303-21;"))
        monkeypatch.setattr(visualization, "save_files", lambda files: ["The flush failed."])
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(tmp_path / "b.step")))
        assert result.startswith("⚠️ STEP 3D model exported, with a warning of the save."), result
        assert "⚠️ The flush failed." in result

    @pytest.mark.parametrize("word", ["to", "for"])  # KiCad 8, KiCad 9 and 10
    def test_the_models_that_kicad_cli_did_not_add(self, cli, pcb_file, tmp_path, word):
        """EXPORTER_STEP::buildFootprint3DShapes gives a warning for each model that it
        did not add. The tool gives these models, and no "✅". The messages of kicad-cli
        have no line that names the temporary file (export_with_kicad_cli)."""
        run = _ExportRun(content=b"ISO-10303-21;")

        def with_messages(args, timeout=60.0, english=False):
            target = args[args.index("--output") + 1]
            run.stdout = (
                f"Build STEP data\n\nCould not add 3D model {word} R1.\n"
                "File not found: ${KICAD8_3DMODEL_DIR}/R.3dshapes/R_0805.wrl\n"
                f"Could not add 3D model {word} U1.\nOpenCASCADE error: bad shape\n"
                f"STEP file '{target}' created."  # no line break at the end
            ).encode()
            run.stderr = b"Error on stderr\n"
            return run(args, timeout, english)

        cli(with_messages)
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(tmp_path / "b.step")))
        assert result.startswith("⚠️ STEP 3D model exported, with no 3D model"), result
        assert "no 3D model for 2 item(s)" in result
        assert "- R1: File not found: ${KICAD8_3DMODEL_DIR}/R.3dshapes/R_0805.wrl" in result
        assert "- U1: OpenCASCADE error: bad shape" in result
        assert result.endswith(
            f"```\nBuild STEP data\nCould not add 3D model {word} R1.\n"
            "File not found: ${KICAD8_3DMODEL_DIR}/R.3dshapes/R_0805.wrl\n"
            f"Could not add 3D model {word} U1.\nOpenCASCADE error: bad shape\n"
            "Error on stderr\n```"
        ), result

    @pytest.mark.parametrize("name", ["b.png", "board.kicad_pcb"])
    def test_an_output_file_of_an_other_type(self, cli, pcb_file, name):
        cli(_no_run)
        result = asyncio.run(export_pcb_3d(str(pcb_file), output_path=str(pcb_file.parent / name)))
        assert "must have the extension .step or .stp" in result, result
        assert pcb_file.read_text(encoding="utf-8") == "(kicad_pcb (version 20240130))"


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
    def __init__(self, returncode, stderr=None):
        self.returncode = returncode
        self.stdout = b""
        if stderr is None:
            stderr = b"" if returncode == 0 else b"Error: cannot load"
        self.stderr = stderr


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
