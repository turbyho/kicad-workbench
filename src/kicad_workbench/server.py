"""Main MCP server setup for KiCad integration."""

from fastmcp import FastMCP

from .version_check import VersionNotes

# Create MCP server
mcp = FastMCP(
    name="kicad-workbench",
    instructions="""KiCad Workbench - A server for analyzing KiCad schematics and PCBs,
    and performing design rule checks.

    Available capabilities:
    - Schematic analysis (components, nets, symbols)
    - PCB analysis (footprints, tracks, statistics)
    - Design rule checking (ERC/DRC)
    - Bill of materials (BOM) through kicad-cli
    - The fields of placed symbols, and the check of their MPNs
    - Schematic summarization
    - Hierarchical sheet and net tracing
    - Pin/power/bus extraction and device tree generation
    - Symbol and footprint library search
    """,
)


# The notes about a newer release and a restart of the client (version_check).
mcp.add_middleware(VersionNotes())


# Import and register tools
from .tools import (  # noqa: F401, E402
    bom,
    device_tree,
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
