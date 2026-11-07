"""Tests of the edit tools off the event loop.

The schematic edit tools do their work (file reads, the parse, kicad-cli, the
save) in a thread (schematic_editor._run_edit). A cancel of the tool does not stop
the thread: the work then stops at the save. setup_pcb_layout writes the board and
reads it back for its summary in a thread (pcb_layout._write_and_summarize).
"""

import asyncio
import threading

import pytest

from kicad_workbench.tools import pcb_layout, schematic_editor


def test_a_cancel_stops_add_label_before_the_save(tmp_path, monkeypatch):
    # The cancel comes while the tool computes the position, in its thread: the work
    # goes on, but stops at the save, and the file does not change.
    path = tmp_path / "a.kicad_sch"
    text = '(kicad_sch (version 20260306) (generator "eeschema") (uuid "a0"))\n'
    path.write_text(text, encoding="utf-8")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    position, save = schematic_editor._label_position, schematic_editor._save
    saves = []

    def slow_position(*args):
        started.set()
        release.wait(5)
        return position(*args)

    def recorded_save(*args):
        saves.append(save(*args))
        finished.set()
        return saves[-1]

    monkeypatch.setattr(schematic_editor, "_label_position", slow_position)
    monkeypatch.setattr(schematic_editor, "_save", recorded_save)

    async def run():
        task = asyncio.create_task(
            schematic_editor.add_label(str(path), "A", 10, 10, snap_to_grid=False)
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    release.set()
    assert finished.wait(5)
    assert saves == [("❌ The tool call was cancelled: the tool did not change the file.", "")]
    assert path.read_text(encoding="utf-8") == text


BOARD = """(kicad_pcb (version 20260206) (generator "pcbnew")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(net 0 "")
{items})
"""
LINES = "".join(
    f'\t(gr_line (start {x1} {y1}) (end {x2} {y2}) (layer "Edge.Cuts"))\n'
    for x1, y1, x2, y2 in ((0, 0, 50, 0), (50, 0, 50, 40), (50, 40, 0, 40), (0, 40, 0, 0))
)


def test_the_summary_of_a_new_board():
    report = pcb_layout._summarize_pcb(BOARD.format(items=LINES))
    assert "**Dimensions (read back):** 50.000 × 40.000 mm" in report, report
    assert "**Layers:** 3" in report
    assert "0 footprint(s), 0 track(s), 1 net(s) declared" in report


def test_an_outline_with_an_other_item():
    # An Edge.Cuts item that is not a gr_line: the outline is not known.
    arc = '\t(gr_arc (start 0 0) (mid 1 1) (end 2 0) (layer "Edge.Cuts"))\n'
    report = pcb_layout._summarize_pcb(BOARD.format(items=LINES + arc))
    assert "**Dimensions (read back):** (not known: the board has no outline" in report


def test_a_line_with_no_end():
    line = '\t(gr_line (start 0 0) (end 10) (layer "Edge.Cuts"))\n'
    report = pcb_layout._summarize_pcb(BOARD.format(items=line))
    assert "**Dimensions (read back):** (not known" in report, report


def test_a_board_that_the_summary_cannot_read(tmp_path, monkeypatch):
    # The board is written: an error of the summary is a warning, not an error.
    def write(path, content, stamp):
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(pcb_layout, "write_board", write)
    error, saved, report = pcb_layout._write_and_summarize(tmp_path / "a.kicad_pcb", "(a", None)
    assert error is None and saved == ""
    assert report.startswith("⚠️ The tool cannot read back the board that it wrote"), report
