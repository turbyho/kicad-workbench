"""Tests of add_wire: KiCad keeps a wire as a line of two points."""

import asyncio

import pytest

from kicad_workbench.parsers.kicad_versions import NEWEST_SCHEMATIC
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools.schematic_editor import add_wire
from kicad_workbench.utils.kicad_cli import find_kicad_cli, run_kicad_cli_sync

from .installed_kicad import installed_schematic_version, write_schematic


def wires(path) -> list[list[tuple[float, float]]]:
    root = parse(path.read_text(encoding="utf-8"))
    return [
        [(xy.number(0), xy.number(1)) for xy in wire.first("pts").children("xy")]
        for wire in root.children("wire")
    ]


def test_one_wire_for_each_segment(tmp_path):
    path = write_schematic(tmp_path, NEWEST_SCHEMATIC)
    result = asyncio.run(add_wire(str(path), [(10.16, 10.16), (20.32, 10.16), (20.32, 20.32)]))
    assert result.startswith("✅ Wire added: 2 segments"), result
    assert wires(path) == [
        [(10.16, 10.16), (20.32, 10.16)],
        [(20.32, 10.16), (20.32, 20.32)],
    ]
    text = path.read_text(encoding="utf-8")
    uuids = {wire.first("uuid").value(0) for wire in parse(text).children("wire")}
    assert len(uuids) == 2
    # One wire on each line, as KiCad writes them: no blank line.
    assert "\n\n" not in text[text.index("\t(wire") :]


def test_positions_as_kicad_writes_them(tmp_path):
    path = write_schematic(tmp_path, NEWEST_SCHEMATIC)
    points = [(10, 2.54), (12.700001, 2.54)]
    assert asyncio.run(add_wire(str(path), points, snap_to_grid=False)).startswith("✅")
    assert "(xy 10 2.54) (xy 12.7 2.54)" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "points, message",
    [
        ([], "❌ A wire needs two or more points."),
        ([(1, 1)], "❌ A wire needs two or more points."),
        (
            [(1, 1), (2, 2), (2.00001, 2)],
            "❌ The wire has a segment of length 0 at (2, 2). Remove one of the two points.",
        ),
        ([(1, 1), (float("nan"), 2)], "❌ The point (nan, 2) is not a position in mm."),
        ([(float("inf"), 1), (2, 2)], "❌ The point (inf, 1) is not a position in mm."),
    ],
)
def test_wire_that_kicad_cannot_keep(tmp_path, points, message):
    path = write_schematic(tmp_path, NEWEST_SCHEMATIC)
    before = path.read_text(encoding="utf-8")
    assert asyncio.run(add_wire(str(path), points, snap_to_grid=False)) == message
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.skipif(
    find_kicad_cli() is None or installed_schematic_version() is None,
    reason="kicad-cli or the libraries of the installed KiCad not found",
)
def test_installed_kicad_loads_a_wire_with_corners(tmp_path):
    path = write_schematic(tmp_path, installed_schematic_version())
    points = [(10.16, 10.16), (20.32, 10.16), (20.32, 20.32), (30.48, 20.32)]
    assert asyncio.run(add_wire(str(path), points)).startswith("✅")
    out = tmp_path / "net.xml"
    result = run_kicad_cli_sync(
        ["sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(path)], timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
