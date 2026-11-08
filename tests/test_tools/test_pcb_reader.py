"""Tests of the reader of the board tools (tools/pcb.py).

The tools read the board with pcbnew when the server can import it. Only then:
the text reader, with a warning, when KiCad is installed (kicad-cli); with no KiCad,
an error that tells the user to install KiCad. When pcbnew cannot read the board, the tools
give an error: pcbnew of KiCad 8 raises OSError (IO_ERROR), pcbnew of KiCad 9
and 10 give None (checked with 8.0.9, 9.0.9 and 10.0.6).
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

from kicad_workbench.tools import pcb
from kicad_workbench.utils import kicad_cli

BOARD = Path(__file__).resolve().parent.parent / "fixtures" / "example_pcb.kicad_pcb"
TOOLS = [
    ("list_pcb_footprints", {}),
    ("get_pcb_statistics", {}),
    ("analyze_pcb_nets", {}),
    ("find_tracks_by_net", {"net_name": "GND"}),
    ("analyze_pcb_signal_integrity", {}),
    ("analyze_pcb_power_integrity", {}),
]


def run(tool: str, path: Path, **kwargs) -> str:
    return asyncio.run(getattr(pcb, tool)(str(path), **kwargs))


@pytest.fixture(autouse=True)
def import_check():
    # The tools check the import of pcbnew one time: each test sets its own pcbnew.
    pcb._pcbnew_importable.cache_clear()
    yield
    pcb._pcbnew_importable.cache_clear()


@pytest.fixture
def pcbnew_not_importable(monkeypatch):
    # A None entry in sys.modules makes "import pcbnew" raise ImportError.
    monkeypatch.setitem(sys.modules, "pcbnew", None)


@pytest.fixture
def no_pcbnew(pcbnew_not_importable, kicad_version):
    """KiCad is installed (kicad-cli gives a version), but the process cannot import
    its pcbnew."""


def fake_pcbnew(monkeypatch, load_board, version="10.0.6", cli=None):
    """A pcbnew of a KiCad version, and kicad-cli of the same version (or of cli)."""
    module = types.ModuleType("pcbnew")
    module.LoadBoard = load_board
    module.GetMajorMinorPatchVersion = lambda: version
    monkeypatch.setitem(sys.modules, "pcbnew", module)
    async def cli_version():
        return cli or version

    monkeypatch.setattr(kicad_cli, "get_kicad_version", cli_version)


def test_pcbnew_of_an_other_kicad(monkeypatch):
    """The pcbnew of the server process and kicad-cli must be of one KiCad."""
    fake_pcbnew(monkeypatch, lambda path: pytest.fail("no load"), "9.0.9", cli="10.0.6")
    result = run("list_pcb_footprints", BOARD)
    assert result.startswith(
        "❌ kicad-cli is KiCad 10.0.6, but the pcbnew of the server process is KiCad 9.0.9."
    ), result


@pytest.mark.parametrize("tool, kwargs", TOOLS)
def test_text_reader_with_a_warning(no_pcbnew, tool, kwargs):
    result = run(tool, BOARD, **kwargs)
    assert result.count("pcbnew not available") == 1, result
    assert not result.startswith("❌")


@pytest.mark.parametrize("tool, kwargs", TOOLS)
def test_no_kicad_is_an_error(pcbnew_not_importable, monkeypatch, tool, kwargs):
    """With no KiCad, the text reader does not read the board: the tools work with the
    files as the installed KiCad does."""
    monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: None)
    result = run(tool, BOARD, **kwargs)
    assert result.startswith("❌ KiCad is not installed"), result
    assert "Tell the user: install KiCad 8 or later" in result


def test_the_limits_of_each_tool(no_pcbnew):
    assert run("list_pcb_footprints", BOARD).endswith("not with pcbnew of KiCad.")
    stats = run("get_pcb_statistics", BOARD)
    assert stats.endswith(": no board outline, no design rules.")
    # No size guess from the footprint positions.
    assert "**Dimensions:** not known" in stats


@pytest.mark.parametrize("version", [20231014, 20250801, 20991231])
def test_text_reader_reads_only_boards_of_kicad_8_to_10(no_pcbnew, tmp_path, version):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(f'(kicad_pcb (version {version}) (generator "pcbnew"))', encoding="utf-8")
    result = run("get_pcb_statistics", board)
    assert result.startswith(f"❌ The board has the file version {version}"), result


def _pcbnew_installed() -> bool:
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _pcbnew_installed(), reason="pcbnew is not installed")
@pytest.mark.parametrize("tool, kwargs", TOOLS)
def test_pcbnew_answers_have_no_warning(tool, kwargs):
    result = run(tool, BOARD, **kwargs)
    assert "pcbnew not available" not in result and not result.startswith("❌"), result


@pytest.mark.parametrize("tool, kwargs", TOOLS)
def test_a_board_that_pcbnew_of_kicad_8_cannot_read(monkeypatch, tool, kwargs):
    def load_board(path):
        raise OSError("KiCad was unable to open this file")

    fake_pcbnew(monkeypatch, load_board, "8.0.9")
    result = run(tool, BOARD, **kwargs)
    assert result.startswith("❌ pcbnew of KiCad 8.0.9 cannot read"), result
    assert "unable to open this file" in result
    assert "pcbnew not available" not in result


def test_a_newer_board_for_pcbnew_of_kicad_9(monkeypatch, tmp_path):
    board = tmp_path / "new.kicad_pcb"
    board.write_text('(kicad_pcb (version 20991231) (generator "pcbnew"))', encoding="utf-8")
    fake_pcbnew(monkeypatch, lambda path: None, "9.0.9")
    result = run("get_pcb_statistics", board)
    assert result.startswith("❌ pcbnew of KiCad 9.0.9 cannot read"), result
    assert "file version 20991231 is newer than the version that KiCad 9.0.9 writes (20241229)" in (
        result
    )


def test_a_board_with_no_reason(monkeypatch):
    fake_pcbnew(monkeypatch, lambda path: None, "10.0.6")
    result = run("analyze_pcb_nets", BOARD)
    assert result.startswith("❌ pcbnew of KiCad 10.0.6 cannot read"), result
    assert "pcbnew gives no reason" in result


@pytest.mark.parametrize("tool, kwargs", TOOLS)
def test_a_file_that_is_not_there(no_pcbnew, tmp_path, tool, kwargs):
    result = run(tool, tmp_path / "none.kicad_pcb", **kwargs)
    assert result.startswith("❌ File not found"), result


def test_a_pcbnew_that_cannot_be_imported(monkeypatch):
    """An import error that is not ImportError (for example a pcbnew of an other wx
    build) is an error, not the text reader."""

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pcbnew":
            raise SystemError("wx version mismatch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = run("get_pcb_statistics", BOARD)
    assert result == "❌ The import of pcbnew failed: SystemError: wx version mismatch"
