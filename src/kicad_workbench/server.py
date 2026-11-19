"""The FastMCP server of KiCad Workbench: its instructions, its middleware and its tools."""

from fastmcp import FastMCP

from .background_jobs import JOB_WAIT, BackgroundJobs
from .kicad_build_notes import KicadBuildNotes
from .version_check import VersionNotes

# Create MCP server
mcp = FastMCP(
    name="kicad-workbench",
    instructions="""KiCad Workbench: the tools read, check and change KiCad projects as
    the installed KiCad does (kicad-cli, pcbnew and the rules of the KiCad source). The
    supported version is KiCad 10 and later. The existing tools also work with KiCad 8
    and 9, and a new tool gives an error with them.

    - Schematics: components, nets, symbols, the sheet hierarchy, the netlist.
    - Boards: footprints, tracks, nets, statistics.
    - Checks: ERC and DRC of kicad-cli, the pin conflicts of the ERC, the pin and
      pad checks, the library links.
    - Libraries: the library tables, the search, symbols and footprints, PNG images,
      new project libraries, the rows of the project tables, the upgrade of older
      project libraries, the copy of symbols and footprints into them, the properties
      of their symbols, derived symbols, the rename and the delete of their symbols
      and footprints.
    - BOM: the BOM of kicad-cli, and the check of the confirmed MPNs.
    - Edits: symbols, wires, labels, the fields and the attributes of placed symbols,
      new projects and boards.
    - Exports: SVG, 3D view, STEP, Gerber.

    An error starts with "❌", a warning with "⚠️". When a tool gives a step for the
    user (for example the conversion of a file in KiCad, or the install of KiCad),
    tell the user.

    The tools read the files on the disk: a change that is not saved in KiCad is not
    in the result. A tool that writes a schematic, a board or a project file refuses
    the file when KiCad has it open (the lock file of KiCad). Tell the user to save the
    files first (and to close them for a write). The ipc_ tools are different: they use
    the running KiCad (ipc_status).

    With kicad-cli 10.0.0 to 10.0.6, a tool that needs the full project (the netlist,
    ERC, the BOM, the SVG export) gives an error for a project with more than one
    top-level sheet: these versions of kicad-cli load only one top-level sheet.

    A tool call that takes longer than {wait} s becomes a background job (an MCP client
    stops a call after its timeout, Claude Desktop after 60 s): the answer starts with
    "⏳" and gives the job ID. The work continues. Call job_status with the job ID for
    the result, or with no ID for the list of the jobs. A call of the tool with the same
    arguments while the job runs starts no second run: it waits for the same job.
    """.format(wait=f"{JOB_WAIT:.0f}"),
)


# The notes about a newer release and a restart of the client (version_check). The
# outermost middleware: the client gets the notes with each answer, also with the "⏳"
# answer of a job.
mcp.add_middleware(VersionNotes())
# The tool calls that take longer than JOB_WAIT become background jobs. The result of a
# job has the notes of the middleware inside (KicadBuildNotes).
mcp.add_middleware(BackgroundJobs())
# The warning for an installed KiCad that the server did not compare with its rules.
mcp.add_middleware(KicadBuildNotes())


# Import and register tools
from .tools import (  # noqa: F401, E402
    bom,
    field_check,
    hierarchical_analysis,
    ipc,
    jobs,
    library,
    netlist,
    parts_registry,
    pcb,
    pcb_layout,
    pin_analysis,
    project,
    project_libraries,
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
