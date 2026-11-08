"""The FastMCP server of KiCad Workbench: its instructions, its middleware and its tools."""

from fastmcp import FastMCP

from .version_check import VersionNotes

# Create MCP server
mcp = FastMCP(
    name="kicad-workbench",
    instructions="""KiCad Workbench: the tools read, check and change KiCad 8, 9 and 10
    projects as the installed KiCad does (kicad-cli, pcbnew and the rules of the KiCad
    source).

    - Schematics: components, nets, symbols, the sheet hierarchy, the netlist.
    - Boards: footprints, tracks, nets, statistics.
    - Checks: ERC and DRC of kicad-cli, the pin conflicts of the netlist, the pin and
      pad checks, the library links.
    - Libraries: the library tables, the search, symbols and footprints, PNG images.
    - BOM: the BOM of kicad-cli, and the check of the confirmed MPNs.
    - Edits: symbols, wires, labels, the fields of placed symbols, new projects and
      boards.
    - Exports: SVG, 3D view, STEP, Gerber.

    An error starts with "❌", a warning with "⚠️". When a tool gives a step for the
    user (for example the conversion of a file in KiCad, or the install of KiCad),
    tell the user.
    """,
)


# The notes about a newer release and a restart of the client (version_check).
mcp.add_middleware(VersionNotes())


# Import and register tools
from .tools import (  # noqa: F401, E402
    bom,
    field_check,
    hierarchical_analysis,
    library,
    netlist,
    parts_registry,
    pcb,
    pcb_layout,
    pin_analysis,
    project,
    schematic,
    schematic_editor,
    schematic_search,
    symbol_fields,
    validation,
    visualization,
)


def create_server() -> FastMCP:
    """Create and configure the MCP server instance.

    Returns:
        Configured FastMCP server instance
    """
    return mcp


def get_server() -> FastMCP:
    """Get the singleton MCP server instance.

    Returns:
        The FastMCP server instance
    """
    return mcp
