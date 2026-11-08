"""Tests of the warning for an unverified installed KiCad (kicad_build_notes)."""

import asyncio

import mcp.types as mt
from fastmcp.tools import ToolResult

from kicad_workbench.kicad_build_notes import KicadBuildNotes
from kicad_workbench.utils import kicad_cli


def call(version: str | None) -> ToolResult:
    """A tool call through the middleware: the tool reads the version of KiCad."""

    async def call_next(context):
        if version is not None:
            kicad_cli.note_kicad_version(version)
        return ToolResult(
            content=[mt.TextContent(type="text", text="✅ Done")],
            structured_content={"result": "✅ Done"},
        )

    return asyncio.run(KicadBuildNotes().on_call_tool(None, call_next))


def test_an_unverified_kicad_gets_the_warning():
    result = call("10.0.8")
    text = result.structured_content["result"]
    assert text.startswith("✅ Done\n\n⚠️ KiCad 10.0.8 is a patch release after KiCad 10.0.7.")
    assert result.content[0].text == text


def test_the_pcbnew_of_a_newer_patch_release(monkeypatch):
    """check_same_kicad notes the version of pcbnew: a newer patch release of it gets
    the warning."""
    from kicad_workbench.utils import kicad_python

    async def call_next(context):
        kicad_python.check_same_kicad("10.0.8", "pcbnew", "10.0.7")
        return ToolResult(content=[mt.TextContent(type="text", text="✅ Done")])

    result = asyncio.run(KicadBuildNotes().on_call_tool(None, call_next))
    assert "⚠️ KiCad 10.0.8 is a patch release after KiCad 10.0.7." in result.content[-1].text


def test_a_defect_of_the_warning_keeps_the_result(monkeypatch):
    """The tool did its work: a defect of the warning gives the result with no warning."""
    from kicad_workbench import kicad_build_notes

    def defect(version):
        raise RuntimeError("defect")

    monkeypatch.setattr(kicad_build_notes, "installed_kicad_warning", defect)
    assert call("10.0.8").structured_content["result"] == "✅ Done"


def test_a_verified_kicad_gets_no_warning():
    for version in ("10.0.6", None):
        assert call(version).structured_content["result"] == "✅ Done"
