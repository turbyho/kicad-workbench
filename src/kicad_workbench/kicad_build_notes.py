"""The warning for an installed KiCad that the server did not compare with its rules.

A development build of a release branch and a patch release after the newest verified
one (kicad_versions.unverified_reason) get the rules of the newest verified patch
release of the same release. Each tool call that uses such a KiCad gets the warning of
kicad_cli.installed_kicad_warning after its result: the middleware collects the KiCad
versions that the call uses (kicad_cli.note_kicad_version).
"""

from __future__ import annotations

import logging

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from .utils.kicad_cli import (
    collect_kicad_versions,
    installed_kicad_warning,
    reset_collection,
)
from .version_check import add_notes

log = logging.getLogger(__name__)


class KicadBuildNotes(Middleware):
    """Adds the warning for an unverified installed KiCad to the result of a tool."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        versions, token = collect_kicad_versions()
        try:
            result = await call_next(context)
        finally:
            reset_collection(token)
        if not isinstance(result, ToolResult):
            # A task of the MCP task protocol: the result comes later, through a
            # different request.
            return result
        try:
            notes = [warning for v in versions if (warning := installed_kicad_warning(v))]
            return add_notes(result, notes)
        except Exception:
            # The tool did its work (also a write): its result goes to the client. The
            # log tells the defect, as in VersionNotes.
            log.warning("the warning of the KiCad version failed", exc_info=True)
            return result
