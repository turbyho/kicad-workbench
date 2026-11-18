"""Tests of export_gerber with a fake kicad-cli: the plot settings of the board, the
drill origin of the board and the argument refill_zones. The check with kicad-cli is
spike/kicad-versions/gerber_settings_check.py."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from kicad_workbench.tools import pcb_layout


def _board(aux_origin: str, zone: bool = True) -> str:
    return (
        '(kicad_pcb (version 20260206) (generator "pcbnew")\n'
        f"  (setup (pcbplotparams (useauxorigin {aux_origin}) (usegerberextensions no)))\n"
        + ('  (zone (net "GND") (layer "F.Cu"))\n' if zone else "")
        + ")\n"
    )


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A fake kicad-cli (its calls), and the installed KiCad of the list (10.0.6)."""
    calls: list[list[str]] = []
    installed = ["10.0.6"]

    async def run(args, *rest, **kwargs):
        calls.append(args)
        output = Path(args[args.index("-o") + 1])
        (output / f"{args[2]}.out").write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    async def version():
        return installed[0]

    monkeypatch.setattr(pcb_layout, "run_kicad_cli", run)
    monkeypatch.setattr(pcb_layout, "get_kicad_version", version)
    return calls, installed


@pytest.mark.parametrize("flag, origin", [("yes", "plot"), ("true", "plot"), ("no", "absolute")])
def test_the_settings_of_the_board(tmp_path, cli, flag, origin):
    calls, _installed = cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board(flag), encoding="utf-8")
    out = asyncio.run(pcb_layout.export_gerber(str(pcb)))
    assert out.startswith("✅ Gerber export complete."), out
    gerbers, drill = calls
    assert gerbers[:4] == ["pcb", "export", "gerbers", "--board-plot-params"]
    assert "--check-zones" not in gerbers
    assert drill[:5] == ["pcb", "export", "drill", "--drill-origin", origin]
    assert "**Zones:** the fills saved in the board file (refill_zones=True" in out
    named = "the auxiliary origin" if origin == "plot" else "the absolute origin"
    assert f"with {named} of the board" in out, out


def test_the_refill_of_the_zones(tmp_path, cli):
    calls, _installed = cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board("no"), encoding="utf-8")
    out = asyncio.run(pcb_layout.export_gerber(str(pcb), refill_zones=True))
    assert "--check-zones" in calls[0], calls
    assert "**Zones:** filled again before the plot" in out


@pytest.mark.parametrize("installed", ["9.0.9", "10.99.0"])
def test_the_refill_needs_kicad_10_0(tmp_path, cli, installed):
    calls, versions = cli
    versions[0] = installed
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board("no"), encoding="utf-8")
    out = asyncio.run(pcb_layout.export_gerber(str(pcb), refill_zones=True))
    assert out == (
        f"❌ refill_zones has the rules of KiCad 10.0 only. The installed KiCad is {installed}."
    )
    assert calls == []


def test_a_board_with_no_plot_settings(tmp_path, cli):
    # No (setup ...): the drill origin of a new board, absolute.
    calls, _installed = cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20260206) (generator "pcbnew"))\n', encoding="utf-8")
    asyncio.run(pcb_layout.export_gerber(str(pcb)))
    assert calls[1][3:5] == ["--drill-origin", "absolute"]


def test_the_last_flag_gives_the_origin(tmp_path, cli):
    # The parsers of KiCad read each item in its sequence: the last one wins.
    calls, _installed = cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20260206) (setup (pcbplotparams (useauxorigin no) "
        "(useauxorigin yes))))\n",
        encoding="utf-8",
    )
    asyncio.run(pcb_layout.export_gerber(str(pcb)))
    assert calls[1][3:5] == ["--drill-origin", "plot"]


def test_a_board_that_the_server_cannot_read(tmp_path, cli):
    # The drill origin is not known: an error, no export.
    calls, _installed = cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20260206) (setup", encoding="utf-8")
    out = asyncio.run(pcb_layout.export_gerber(str(pcb)))
    assert out.startswith("❌ The server cannot read the plot settings of the board"), out
    assert calls == []


def test_no_zones_and_no_refill_hint_for_kicad_9(tmp_path, cli):
    calls, versions = cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board("no", zone=False), encoding="utf-8")
    assert "**Zones:** the board has no zones" in asyncio.run(pcb_layout.export_gerber(str(pcb)))
    versions[0] = "9.0.9"
    pcb.write_text(_board("no"), encoding="utf-8")
    out = asyncio.run(pcb_layout.export_gerber(str(pcb)))
    assert "**Zones:** the fills saved in the board file\n" in out, out
