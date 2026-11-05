"""Tests for setup_pcb_layout.

pcbnew of the installed KiCad makes the board. Most tests replace pcbnew with
a fake script run, thus they run with no KiCad. TestWithKiCad uses the real
pcbnew and is skipped when no Python with pcbnew is found.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import header_version
from kicad_workbench.parsers.sexpr import quote
from kicad_workbench.tools.pcb_layout import _version_warning, setup_pcb_layout
from kicad_workbench.utils import kicad_python
from kicad_workbench.utils.kicad_cli import find_kicad_cli, run_kicad_cli_sync
from kicad_workbench.utils.kicad_python import find_kicad_python

from .installed_kicad import installed_schematic_version, write_schematic

ROUTED_BOARD = '(kicad_pcb (version 20241229)\n  (footprint "R_0603" (layer "F.Cu"))\n)\n'
PROJECT = '{\n  "meta": {"filename": "board.kicad_pro", "version": 3},\n  "user": "settings"\n}\n'


def _result(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _fake_board(version: int | None, title: str, width: float, height: float) -> str:
    header = f"\t(version {version})\n" if version is not None else ""
    corners = [(0, 0), (width, 0), (width, height), (0, height)]
    lines = "".join(
        f'\t(gr_line (start {x1} {y1}) (end {x2} {y2}) (layer "Edge.Cuts"))\n'
        for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1], strict=True)
    )
    return (
        f'(kicad_pcb\n{header}\t(generator "pcbnew")\n\t(paper "A4")\n'
        f"\t(title_block\n\t\t(title {quote(title)})\n\t)\n{lines})\n"
    )


@pytest.fixture
def fake_pcbnew(monkeypatch):
    """Replace the pcbnew script run. Set "board" and "kicad" to change its output."""
    state = {"board": 20260206, "kicad": "10.0.6", "calls": []}

    def run(args, timeout=120.0, input=None):
        state["calls"].append(args)
        path = Path(args[2])
        path.write_text(
            _fake_board(state["board"], args[3], float(args[6]), float(args[7])), encoding="utf-8"
        )
        # NewBoard() also saves the project file next to the board.
        path.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
        # A log line of pcbnew after the version line of the script.
        return _result(stdout=f"KICAD_VERSION={state['kicad']}\nlog line\n".encode())

    monkeypatch.setattr(kicad_python, "run_kicad_python_sync", run)
    return state


def setup(schematic, **kwargs) -> str:
    return asyncio.run(setup_pcb_layout(str(schematic), **kwargs))


class TestNewBoard:
    def test_new_board(self, tmp_path, fake_pcbnew):
        sch = write_schematic(tmp_path, 20260306)
        result = setup(sch, width=50, height=40)
        assert result.startswith("✅ PCB layout initialized.\n\n**Schematic:**"), result
        assert "50.000 × 40.000 mm" in result
        assert "**Made by:** pcbnew of KiCad 10.0.6" in result
        content = sch.with_suffix(".kicad_pcb").read_text(encoding="utf-8")
        assert content == _fake_board(20260206, "board", 50.0, 40.0)
        # The script gets the title, the outline in mm, and no project save.
        args = fake_pcbnew["calls"][0]
        assert args[3:] == ["board", "", "", "50.0", "40.0", "no-project"]

    def test_outline_in_mil(self, tmp_path, fake_pcbnew):
        setup(write_schematic(tmp_path, 20260306), width=1000, height=500, unit="mil")
        assert fake_pcbnew["calls"][0][6:8] == ["25.4", "12.7"]

    def test_project_files_are_not_changed(self, tmp_path, fake_pcbnew):
        sch = write_schematic(tmp_path, 20260306)
        project = sch.with_suffix(".kicad_pro")
        project.write_text(PROJECT, encoding="utf-8")
        assert setup(sch).startswith("✅")
        assert project.read_text(encoding="utf-8") == PROJECT
        names = sorted(path.name for path in tmp_path.iterdir())
        assert names == ["board.kicad_pcb", "board.kicad_pro", "board.kicad_sch"]

    def test_warning_for_an_older_schematic(self, tmp_path, fake_pcbnew):
        sch = write_schematic(tmp_path, 20231120)
        result = setup(sch)
        assert result.startswith("✅ PCB layout initialized.\n\n⚠️ **Warning:**"), result
        assert "KiCad 8.0 cannot open the board. Tell the user." in result
        assert sch.with_suffix(".kicad_pcb").exists()

    def test_note_for_a_nightly_schematic(self, tmp_path, fake_pcbnew):
        fake_pcbnew.update(board=20261031, kicad="10.99.0")
        result = setup(write_schematic(tmp_path, 20261025))
        assert "⚠️" not in result
        assert "**Note:**" in result
        assert "can open it only if it reads the board version 20261031" in result

    def test_existing_board_is_replaced_with_overwrite(self, tmp_path, fake_pcbnew):
        sch = write_schematic(tmp_path, 20260306)
        pcb = sch.with_suffix(".kicad_pcb")
        pcb.write_text(ROUTED_BOARD, encoding="utf-8")
        result = setup(sch, width=50, height=40, overwrite=True)
        assert result.startswith("✅"), result
        assert "R_0603" not in pcb.read_text(encoding="utf-8")


class TestRefusal:
    def test_existing_board_is_not_replaced(self, tmp_path, fake_pcbnew):
        sch = write_schematic(tmp_path, 20260306)
        pcb = sch.with_suffix(".kicad_pcb")
        pcb.write_text(ROUTED_BOARD, encoding="utf-8")
        result = setup(sch, width=50, height=40)
        assert result.startswith("Error: The board file"), result
        assert "overwrite=True" in result
        assert pcb.read_text(encoding="utf-8") == ROUTED_BOARD
        assert fake_pcbnew["calls"] == []

    def test_schematic_of_an_unknown_version(self, tmp_path, fake_pcbnew):
        sch = write_schematic(tmp_path, 20230121)
        result = setup(sch)
        assert result.startswith(f"❌ {sch}: "), result
        assert "The tool did not write the board." in result
        assert not sch.with_suffix(".kicad_pcb").exists()

    @pytest.mark.parametrize(
        "width, height", [(0, 40), (-5, 40), (50, float("nan")), (3000, 40), (50, float("inf"))]
    )
    def test_bad_size(self, tmp_path, fake_pcbnew, width, height):
        sch = write_schematic(tmp_path, 20260306)
        result = setup(sch, width=width, height=height)
        assert result.startswith("❌ The width and the height must be more than 0"), result
        assert fake_pcbnew["calls"] == []
        assert not sch.with_suffix(".kicad_pcb").exists()

    def test_unknown_unit(self, tmp_path, fake_pcbnew):
        result = setup(write_schematic(tmp_path, 20260306), width=4, height=3, unit="inch")
        assert result == '❌ The unit must be "mm" or "mil", not \'inch\'.'
        assert fake_pcbnew["calls"] == []

    def test_board_with_no_version(self, tmp_path, fake_pcbnew):
        fake_pcbnew["board"] = None
        sch = write_schematic(tmp_path, 20260306)
        result = setup(sch)
        assert result.startswith("❌ pcbnew wrote a board with no file version"), result
        assert not sch.with_suffix(".kicad_pcb").exists()

    def test_no_kicad_version(self, tmp_path, fake_pcbnew):
        fake_pcbnew["kicad"] = "unknown"
        sch = write_schematic(tmp_path, 20260306)
        result = setup(sch)
        assert result.startswith("❌ pcbnew did not give the KiCad version"), result
        assert not sch.with_suffix(".kicad_pcb").exists()

    def test_pcbnew_failure(self, tmp_path, monkeypatch):
        def run(args, timeout=120.0, input=None):
            return _result(returncode=1, stderr=b"ImportError: pcbnew")

        monkeypatch.setattr(kicad_python, "run_kicad_python_sync", run)
        result = setup(write_schematic(tmp_path, 20260306))
        assert result.startswith("❌ pcbnew could not create the board:"), result
        assert "ImportError: pcbnew" in result

    def test_no_python_with_pcbnew(self, tmp_path, monkeypatch):
        monkeypatch.setattr(kicad_python, "find_kicad_python", lambda: None)
        result = setup(write_schematic(tmp_path, 20260306))
        assert result.startswith("❌ No Python with KiCad's pcbnew module was found."), result

    def test_timeout(self, tmp_path, monkeypatch):
        def run(args, timeout=120.0, input=None):
            raise subprocess.TimeoutExpired("python", timeout)

        monkeypatch.setattr(kicad_python, "run_kicad_python_sync", run)
        result = setup(write_schematic(tmp_path, 20260306))
        assert result == "❌ pcbnew did not stop in 120 s. The tool did not write the board."


class TestVersionWarning:
    @pytest.mark.parametrize(
        "board, schematic, reader",
        [
            (20260206, 20231120, "KiCad 8.0 cannot open the board"),
            (20260206, 20250114, "KiCad 9.0 cannot open the board"),
        ],
    )
    def test_warning(self, board, schematic, reader):
        warning = _version_warning(board, "10.0.6", schematic)
        assert warning.startswith("⚠️ **Warning:** The board has the file version")
        assert reader in warning
        assert "KiCad 10.0.6 or later" in warning

    def test_note_for_a_nightly_build_between_two_releases(self):
        note = _version_warning(20260206, "10.0.6", 20240620)
        assert note.startswith("**Note:**")
        assert "KiCad 8.0 cannot open the board" in note
        assert "can open it only if it reads the board version 20260206" in note

    @pytest.mark.parametrize(
        "board, schematic",
        [
            # The board of the release of the schematic, or an older board.
            (20260206, 20260306),
            (20241229, 20250114),
            (20241229, 20260306),
            # A nightly schematic after KiCad 10.0 reads a board of KiCad 10.
            (20260206, 20261025),
        ],
    )
    def test_no_warning(self, board, schematic):
        assert _version_warning(board, "10.0.6", schematic) == ""


@pytest.mark.skipif(find_kicad_python() is None, reason="no Python with pcbnew")
class TestWithKiCad:
    """The real pcbnew of the installed KiCad."""

    @pytest.fixture
    def schematic(self, tmp_path):
        # The schematic of the installed release gets no warning.
        return write_schematic(tmp_path, installed_schematic_version() or 20260306)

    def test_new_board(self, schematic):
        result = setup(schematic, width=50, height=40)
        assert result.startswith("✅"), result
        assert "50.000 × 40.000 mm" in result
        content = schematic.with_suffix(".kicad_pcb").read_text(encoding="utf-8")
        assert content.count('(layer "Edge.Cuts")') == 4
        assert '(title "board")' in content
        assert header_version(content, "kicad_pcb") is not None

    def test_project_files_are_not_changed(self, schematic, tmp_path):
        project = schematic.with_suffix(".kicad_pro")
        project.write_text(PROJECT, encoding="utf-8")
        assert setup(schematic).startswith("✅")
        assert project.read_text(encoding="utf-8") == PROJECT
        names = sorted(path.name for path in tmp_path.iterdir())
        assert names == ["board.kicad_pcb", "board.kicad_pro", "board.kicad_sch"]

    @pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not found")
    def test_installed_kicad_loads_the_board(self, schematic, tmp_path):
        assert setup(schematic, width=50, height=40).startswith("✅")
        svg = tmp_path / "out.svg"
        pcb = schematic.with_suffix(".kicad_pcb")
        command = ["pcb", "export", "svg", "--layers", "Edge.Cuts", "-o", str(svg), str(pcb)]
        result = run_kicad_cli_sync(command, timeout=120)
        assert result.returncode == 0, result.stderr
        assert svg.exists()
