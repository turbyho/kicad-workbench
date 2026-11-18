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


def _board_with_a_library(tmp_path: Path) -> Path:
    """The example board, with the library nickname in the ID of R1."""
    text = BOARD.read_text(encoding="utf-8")
    old = '(footprint "R_0805_2012Metric"'
    board = tmp_path / "b.kicad_pcb"
    new = '(footprint "Resistor_SMD:R_0805_2012Metric"'
    board.write_text(text.replace(old, new, 1), encoding="utf-8")
    return board


def _footprint_rows(result: str) -> list[str]:
    return [line for line in result.splitlines() if line.startswith(("| R1 ", "| R2 "))]


def test_text_reader_gives_the_library_id_and_the_pads(no_pcbnew, tmp_path):
    rows = _footprint_rows(run("list_pcb_footprints", _board_with_a_library(tmp_path)))
    assert [row.split(" | ")[2] for row in rows] == [
        "Resistor_SMD:R_0805_2012Metric",
        "R_0805_2012Metric",
    ]
    assert all(row.endswith("| 2 |") for row in rows), rows


@pytest.mark.skipif(not _pcbnew_installed(), reason="pcbnew is not installed")
def test_the_two_readers_give_the_same_footprints(tmp_path, monkeypatch, kicad_version):
    """pcbnew (LIB_ID::Format, FOOTPRINT::GetPadCount) is the specification of the text
    reader."""
    board = _board_with_a_library(tmp_path)
    with_pcbnew = _footprint_rows(run("list_pcb_footprints", board))
    pcb._pcbnew_importable.cache_clear()
    monkeypatch.setitem(sys.modules, "pcbnew", None)
    assert _footprint_rows(run("list_pcb_footprints", board)) == with_pcbnew
    assert len(with_pcbnew) == 2


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


def test_a_value_with_a_bar_is_one_cell(no_pcbnew, tmp_path):
    board = tmp_path / "b.kicad_pcb"
    text = BOARD.read_text(encoding="utf-8")
    board.write_text(text.replace('"Value" "10k"', '"Value" "10k|1%"', 1), encoding="utf-8")
    rows = _footprint_rows(run("list_pcb_footprints", board))
    assert "| R1 | 10k\\|1% | R_0805_2012Metric |" in rows[0], rows


ZONES_ONLY = """(kicad_pcb (version 20260206)
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal))
  (zone (net "GND") (layers "F.Cu" "B.Cu") (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))
)
"""


def test_text_reader_a_net_with_only_zones(no_pcbnew, tmp_path):
    board = tmp_path / "zones.kicad_pcb"
    board.write_text(ZONES_ONLY, encoding="utf-8")
    answer = run("find_tracks_by_net", board, net_name="GND")
    assert "**Copper Zones:** 1" in answer, answer
    assert "| GND | F.Cu, B.Cu |" in answer, answer


def test_text_reader_gives_the_gnd_layer_warning(no_pcbnew, tmp_path):
    board = tmp_path / "zones.kicad_pcb"
    board.write_text(ZONES_ONLY, encoding="utf-8")
    answer = run("analyze_pcb_power_integrity", board)
    assert "**GND Coverage:** 1 zones across 2 layers" in answer, answer
    assert "⚠️ GND zones only on 2 of 4 copper layers." in answer, answer


def test_text_reader_similar_nets_with_zones(no_pcbnew, tmp_path):
    # A net with only zones is a similar net too.
    board = tmp_path / "zones.kicad_pcb"
    board.write_text(ZONES_ONLY, encoding="utf-8")
    answer = run("find_tracks_by_net", board, net_name="gnd")
    assert "- `GND` (0 segments, 0 vias, 1 copper zones)" in answer, answer
    answer = run("find_tracks_by_net", board, net_name="VCC")
    assert answer.startswith("No tracks, vias or copper zones found for net 'VCC'."), answer


def _many_items_board(segments: int = 55, vias: int = 22, widths=(0.2, 0.3)) -> str:
    """A KiCad 10 board with segments (of the widths, in turn) and vias on the net GND."""
    segment_text = "".join(
        f"  (segment (start {i} 0) (end {i + 1} 0) (width {widths[i % len(widths)]}) "
        f'(layer "F.Cu") (net "GND") (uuid "s{i}"))\n'
        for i in range(segments)
    )
    via_text = "".join(
        f'  (via (at {i} 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND") '
        f'(uuid "v{i}"))\n'
        for i in range(vias)
    )
    return (
        '(kicad_pcb (version 20260206)\n  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n'
        + segment_text
        + via_text
        + ")\n"
    )


def test_text_reader_tells_the_cut_tables(no_pcbnew, tmp_path):
    # As with pcbnew: a row for the items that the tables do not show, and the advice
    # about different widths.
    board = tmp_path / "many.kicad_pcb"
    board.write_text(_many_items_board(), encoding="utf-8")
    answer = run("find_tracks_by_net", board, net_name="GND")
    assert "| ... | (5 more segments) | | | | |" in answer, answer
    assert "| ... | (2 more vias) | | | |\n" in answer, answer
    assert "⚠️ The tracks of the net have different widths" in answer, answer


def test_text_reader_tables_at_their_limits(no_pcbnew, tmp_path):
    # 50 segments and 20 vias are all in the tables; one width gives no warning.
    board = tmp_path / "limits.kicad_pcb"
    board.write_text(_many_items_board(50, 20, widths=(0.25,)), encoding="utf-8")
    answer = run("find_tracks_by_net", board, net_name="GND")
    assert "more segments" not in answer and "more vias" not in answer, answer
    assert "different widths" not in answer, answer
    assert "| 50 | (49.00, 0.00) |" in answer and "| 20 | (19.00, 5.00) |" in answer


ORDER = """(kicad_pcb (version 20260206)
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (arc (start 1 0) (mid 0.7071067811865476 0.7071067811865476) (end 0 1) (width 0.2)
    (layer "F.Cu") (net "GND") (uuid "a"))
  (segment (start 5 5) (end 6 5) (width 0.2) (layer "F.Cu") (net "GND") (uuid "s"))
  (via (at 3 3) (size 0.6) (drill 0.3) (net "GND") (uuid "v"))
)
"""


def test_text_reader_order_of_the_file_and_via_layers(no_pcbnew, tmp_path):
    # BOARD::Tracks() has the order of the file (the arc first); a via with no
    # (layers ...) has F.Cu and B.Cu (the constructor of PCB_VIA).
    board = tmp_path / "order.kicad_pcb"
    board.write_text(ORDER, encoding="utf-8")
    answer = run("find_tracks_by_net", board, net_name="GND")
    assert "| 1 | (1.00, 0.00) | (0.00, 1.00) |" in answer, answer
    assert "| 2 | (5.00, 5.00) | (6.00, 5.00) |" in answer, answer
    assert "| 1 | (3.00, 3.00) | 0.600 | 0.300 | F.Cu -> B.Cu |" in answer, answer


def test_a_footprint_filter_with_a_user_name(no_pcbnew):
    # A footprint is on F.Cu or B.Cu: a user name of the layer ("Top") is an error, not
    # an empty list.
    answer = run("list_pcb_footprints", BOARD, filter_layer="Top")
    assert answer.startswith("❌ filter_layer 'Top' is not F.Cu or B.Cu"), answer
