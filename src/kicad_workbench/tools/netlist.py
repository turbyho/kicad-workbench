"""Netlist generation and analysis tools for KiCad Workbench."""

import asyncio
import collections
import functools
import hashlib
import tempfile
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.netlist_parser import NetlistComponent, NetlistParser
from ..parsers.sheet_list import str_num_cmp
from ..server import mcp
from ..utils.kicad_cli import KicadCliError, cli_root, cli_sheet_paths, export_with_kicad_cli
from ..utils.sch_hierarchy import AmbiguousRootError, SheetPath, schematic_project_file
from ..utils.schematic_upgrade import with_current_schematics
from ..utils.tool_errors import error_answer
from ..utils.user_files import write_atomic

# The time of kicad-cli for the netlist (a large project needs some seconds).
NETLIST_TIMEOUT = 120.0


class NetlistError(WorkbenchError):
    """The server cannot make the netlist of a schematic. The message tells why."""


def netlist_cache_path(sch_path: Path) -> Path:
    """Deterministic per-schematic netlist path in the temp directory.

    Keying only on the file stem made two different projects that both have
    e.g. a ``main.kicad_sch`` overwrite each other's exported netlist, so a
    short digest of the resolved schematic path is mixed into the name.
    """
    digest = hashlib.sha256(str(sch_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"{sch_path.stem}_{digest}.xml"


def export_netlist(sch_path: Path) -> tuple[bytes, Path | None]:
    """Return the XML netlist of a schematic as kicad-cli writes it.

    A sub-sheet does not have the hierarchy that gives the names of the nets with
    no label. Thus kicad-cli writes the netlist of the schematic that gives the
    netlist of the full project (netlist_root, as for the BOM): the schematic of
    the project name. A project with more than one top-level sheet (KiCad 10) is
    an error with kicad-cli 10.0.0 to 10.0.6: it loads only one top-level sheet,
    and the netlist would not have the others. kicad-cli writes into its own
    temporary folder (export_with_kicad_cli). The function blocks: run it off the
    event loop.

    Args:
        sch_path: The schematic file.

    Returns:
        The netlist, and the root schematic when it is not sch_path (None when
        sch_path is the root schematic).

    Raises:
        AmbiguousRootError: More than one project holds the sheet.
        NetlistError: kicad-cli is not there or cannot run, did not stop in
            time, or did not write the netlist; or it cannot write the netlist of
            the full project.
    """
    try:
        root = cli_root(sch_path, "netlist")
    except InputError as e:
        raise NetlistError(f"The server cannot make the netlist of {sch_path}: {e}") from e
    source = root or sch_path
    try:
        export = export_with_kicad_cli(
            ["sch", "export", "netlist", "--format", "kicadxml"],
            source,
            "netlist.xml",
            NETLIST_TIMEOUT,
        )
    except (FileNotFoundError, KicadCliError) as e:  # not there, cannot run, no file
        raise NetlistError(f"The server cannot make the netlist of {source}: {e}") from e
    return export.data, root


def _write_netlist_sync(sch_path: Path) -> tuple[Path, Path | None]:
    data, root = export_netlist(sch_path)
    netlist_path = netlist_cache_path(root or sch_path)
    # In one step: an other tool call can read the file at the same time.
    write_atomic(netlist_path, data)
    return netlist_path, root


def _parsed_netlist(sch_path: Path) -> tuple[NetlistParser, Path | None]:
    """Return the parsed netlist of kicad-cli and the root schematic that kicad-cli
    got. The function blocks (the export and the parse of the XML).

    Raises:
        AmbiguousRootError, NetlistError: As netlist_of.
        InputError: The netlist is not valid XML.
    """
    data, root = export_netlist(sch_path)
    return NetlistParser.from_bytes(data, f"of kicad-cli for {root or sch_path}"), root


def netlist_of(sch_path: Path) -> NetlistParser:
    """Return the netlist of the schematic as kicad-cli writes it now.

    The tools that use the nets read the files of the schematic at each call: a
    netlist file of an earlier export (of the cache, of the schematic editor, or
    of an other project with the same sheet name) can be old. The netlist stays
    in memory: no file of an other tool call can change it. For a schematic that
    is not the root schematic of its project, it is the netlist of the root
    schematic (export_netlist, cli_root). The function blocks:
    run it off the event loop (current_netlist).

    Raises:
        AmbiguousRootError: More than one project holds the sheet.
        NetlistError: kicad-cli is not there or cannot run, did not stop in
            time, or did not write the netlist; or it cannot write the netlist of
            the full project (cli_root).
        InputError: The netlist is not valid XML.
    """
    return _parsed_netlist(sch_path)[0]


async def current_netlist(sch_path: Path) -> NetlistParser:
    """netlist_of off the event loop."""
    return await asyncio.to_thread(netlist_of, sch_path)


_REFERENCE_KEY = functools.cmp_to_key(str_num_cmp)


def reference_key(component: NetlistComponent):
    """The sort key of a component: its reference in the order of KiCad (StrNumCmp)."""
    return _REFERENCE_KEY(component.reference)


def components_of_file(
    sch_path: Path, below: bool
) -> tuple[NetlistParser, list[NetlistComponent], list[str]]:
    """Return the netlist of kicad-cli, the components of a schematic file in it, and
    the warnings.

    kicad-cli gives each component of each sheet path of the hierarchy, with the
    reference of that path (NETLIST_EXPORTER_XML::makeSymbols): no power symbols
    and no other references that start with "#", and no symbol with no library
    symbol (NETLIST_EXPORTER_BASE::findNextSymbol). The components of the file are
    the components of each sheet path of the file in the hierarchy that kicad-cli
    loads (cli_sheet_paths, for the KiCad of the netlist), with below also the
    components of the sheets below these paths. The sheet path of a component is
    its "tstamps" in the netlist (SheetPath.netlist_path), not its "names": KiCad
    expands the text variables of the sheet names. Only the top-level sheets of
    kicad-cli 10.0.7 and later have the same "tstamps" ("/"): for a component on
    one of them, "names" (with the name of the top-level sheet) tells which. The
    function blocks: run it off the event loop.

    Args:
        sch_path: The schematic file (the root schematic or a sub-sheet).
        below: Also the components of the sheets below the file.

    Returns:
        (the netlist, the components in the order of their references, warnings).

    Raises:
        AmbiguousRootError: More than one project holds the sheet.
        NetlistError: kicad-cli did not write the netlist, or it cannot write the
            netlist of the full project (cli_root).
        InputError: The netlist is not valid XML.
    """
    netlist, root = _parsed_netlist(sch_path)
    hierarchy = root or sch_path
    # The hierarchy of the kicad-cli that wrote the netlist (<tool>Eeschema 10.0.7).
    places, problems = cli_sheet_paths(
        hierarchy, netlist.kicad_version, schematic_project_file(sch_path)
    )
    warnings = [f"⚠️ {problem}" for problem in problems] + netlist_warnings(netlist)
    sheets = netlist.get_sheets()
    counts = collections.Counter(sheet.tstamps for sheet in sheets)
    ambiguous = {tstamps for tstamps, count in counts.items() if count > 1}

    def key(tstamps: str | None, names: str) -> tuple[str | None, str | None]:
        # One "tstamps" for more sheets: the top-level sheets of kicad-cli 10.0.7 and
        # later ("/"). Then also "names", which has the name of the top-level sheet.
        return (tstamps, names) if tstamps in ambiguous else (tstamps, None)

    known = {key(p.netlist_path, p.shown) for p in places if p.netlist_path is not None}
    unknown = [s.names for s in sheets if key(s.tstamps, s.names) not in known]
    if unknown:
        warnings.append(
            f"⚠️ The netlist has sheets that the tool does not find in the hierarchy of "
            f"{hierarchy}: {', '.join(unknown)}. The tool cannot tell if their components "
            "are in the file: they are not in the result."
        )
    target = sch_path.resolve()
    mine = [
        p
        for p in places
        if p.netlist_path is not None and p.file is not None and p.file.resolve() == target
    ]
    if not mine:
        warnings.append(
            f"⚠️ {sch_path} has no sheet path with UUIDs in the hierarchy of {hierarchy}."
        )

    def below_place(component: NetlistComponent, place: SheetPath) -> bool:
        if place.netlist_path in ambiguous and place.uuids:
            # A top-level sheet ("/"): the sheets below it are the paths of the server
            # that start with its UUID; their "tstamps" are unique.
            tree = {
                p.netlist_path
                for p in places
                if p.uuids and p.uuids[0] == place.uuids[0] and p.netlist_path not in ambiguous
            }
            return component.sheet_tstamps in tree
        return component.sheet_tstamps.startswith(place.netlist_path)

    def inside(component: NetlistComponent) -> bool:
        own = key(component.sheet_tstamps, component.sheet_names)
        if own not in known:
            return False  # also no sheet path: a symbol with no library symbol
        return any(
            own == key(place.netlist_path, place.shown) or (below and below_place(component, place))
            for place in mine
        )

    components = [c for c in netlist.get_components().values() if inside(c)]
    return netlist, sorted(components, key=reference_key), warnings


def netlist_warnings(netlist: NetlistParser) -> list[str]:
    """The warnings about the components of a netlist that a search cannot use: a
    reference of more than one component (the annotation is not complete), and the
    nodes of a symbol with no library symbol (KiCad writes no <comp> for it)."""
    warnings = []
    duplicates = netlist.get_duplicate_references()
    if duplicates:
        warnings.append(
            "⚠️ The annotation is not complete: the netlist has more than one component "
            f"with the reference {', '.join(duplicates)}. The result has one of them."
        )
    no_comp = [ref for ref, c in netlist.get_components().items() if not c.sheet_tstamps]
    if no_comp:
        warnings.append(
            "⚠️ The netlist has the pins of "
            f"{', '.join(sorted(no_comp, key=functools.cmp_to_key(str_num_cmp)))} but no "
            "component (a symbol with no library symbol): the result does not have them."
        )
    return warnings


async def write_netlist(sch_path: Path) -> tuple[Path, Path | None]:
    """Write the netlist of a schematic (export_netlist) into netlist_cache_path().

    The function replaces the file of the cache in one step.

    Args:
        sch_path: The schematic file.

    Returns:
        The netlist file, and the root schematic when it is not sch_path (None
        for the root schematic of the project).

    Raises:
        AmbiguousRootError: More than one project holds the sheet.
        NetlistError: kicad-cli is not there or cannot run, did not stop in
            time, or did not write the netlist; or it cannot write the netlist of
            the full project (cli_root).
        OSError: The file of the cache cannot be written.
    """
    return await asyncio.to_thread(_write_netlist_sync, sch_path)


@mcp.tool()
@with_current_schematics("schematic_path")
async def generate_netlist(
    schematic_path: str,
) -> str:
    """Generate KiCad netlist from schematic file.

    Args:
        schematic_path: Path to .kicad_sch file

    Returns:
        Path to generated .xml netlist file
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ Schematic file not found: {schematic_path}"
        try:
            netlist_path, root_sch = await write_netlist(sch_path)
        except AmbiguousRootError as e:
            return f"❌ {e}. Generate the netlist of the root schematic that you want."
        subsheet_note = ""
        if root_sch:
            subsheet_note = (
                f"\n\n⚠️ **Note:** `{sch_path.name}` is not the root schematic of its "
                f"project. The netlist is the netlist of `{root_sch.name}`, the schematic "
                "that kicad-cli loads with the full project (a sub-sheet alone does not "
                "give the names of the nets with no label)."
            )
        return f"""✅ Netlist generated successfully

**Output:** {netlist_path}

You can now use netlist-based tools:
- `trace_netlist_connection()` - Trace component connections via netlist
- `get_netlist_nets()` - List all nets
- `get_netlist_components()` - List all components with their nets
{subsheet_note}"""

    except Exception as e:
        return error_answer(e, "Error generating netlist")


@mcp.tool()
async def trace_netlist_connection(
    netlist_path: str,
    reference: str,
    pin_number: str = "",
) -> str:
    """Trace component connections using netlist file (most accurate).

    Args:
        netlist_path: Path to .xml netlist file
        reference: Component reference (e.g., 'R16')
        pin_number: Optional pin number (e.g., '1'). If empty, trace all pins.

    Returns:
        Detailed connection information from netlist
    """
    try:
        parser = await asyncio.to_thread(NetlistParser, netlist_path)

        if pin_number:
            result = parser.trace_connection(reference, pin_number)

            if "error" in result:
                return f"❌ {result['error']}"

            lines = [f"## Netlist Connection Trace: {reference}", "", f"**Pin:** {result['pin']}"]
            # A pin number can be on more than one net: one section for each net.
            for net in result["nets"]:
                lines += ["", f"**Net:** {net['net']}", "", "### Connected Components:", ""]
                if net["connected_to"]:
                    lines.append("| Reference | Pin |")
                    lines.append("|-----------|-----|")
                    for ref, pin in net["connected_to"]:
                        lines.append(f"| {ref} | {pin} |")
                else:
                    lines.append("No other components on this net.")
            if note := parser.pins_on_more_nets_note([reference], pin_number):
                lines += ["", note]

            return "\n".join(lines)

        else:
            result = parser.trace_connection(reference)

            if "error" in result:
                return f"❌ {result['error']}"

            lines = [
                f"## Netlist Connection Trace: {reference}",
                "",
                f"**Total Nets:** {len(result['nets'])}",
                "",
            ]

            # trace_connection gives a list for each net: a component can have more
            # than one pin on a net. The other components of the net are the same
            # for each of these pins.
            for net_name, pins in result["nets"].items():
                lines.append(f"### Net: {net_name}")
                label = "Pins" if len(pins) > 1 else "Pin"
                lines.append(f"**{label}:** {', '.join(pin['pin'] for pin in pins)}")
                lines.append("")

                if pins[0]["connected_to"]:
                    lines.append("| Reference | Pin |")
                    lines.append("|-----------|-----|")
                    for ref, pin in pins[0]["connected_to"]:
                        lines.append(f"| {ref} | {pin} |")
                else:
                    lines.append("No other components on this net.")

                lines.append("")

            if note := parser.pins_on_more_nets_note([reference]):
                lines.append(note)
            return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error tracing netlist")


@mcp.tool()
async def get_netlist_nets(
    netlist_path: str,
    filter_pattern: str = "",
) -> str:
    """Get all nets from netlist file.

    Args:
        netlist_path: Path to .xml netlist file
        filter_pattern: Optional regex pattern to filter net names

    Returns:
        List of all nets with their connections
    """
    try:
        parser = await asyncio.to_thread(NetlistParser, netlist_path)
        nets = parser.get_nets()

        lines = [
            f"## Nets from Netlist: {netlist_path}",
            "",
            f"**Total Nets:** {len(nets)}",
            "",
        ]

        for net_name, net in sorted(nets.items()):
            if filter_pattern:
                import re
                if not re.search(filter_pattern, net_name, re.IGNORECASE):
                    continue

            lines.append(f"### {net_name}")
            lines.append(f"**Code:** {net.code}")
            lines.append(f"**Connections:** {len(net.pins)} pins")

            if net.pins:
                lines.append("")
                lines.append("| Reference | Pin |")
                lines.append("|-----------|-----|")
                for ref, pin in net.pins[:10]:  # Show first 10
                    lines.append(f"| {ref} | {pin} |")
                if len(net.pins) > 10:
                    lines.append(f"| ... | ({len(net.pins) - 10} more) |")

            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error reading netlist")


@mcp.tool()
async def get_netlist_components(
    netlist_path: str,
    filter_ref: str = "",
) -> str:
    """Get all components from netlist with their network connections.

    Args:
        netlist_path: Path to .xml netlist file
        filter_ref: Optional reference filter (e.g., 'R', 'U', 'C')

    Returns:
        List of components with their net connections
    """
    try:
        parser = await asyncio.to_thread(NetlistParser, netlist_path)
        components = parser.get_components()

        lines = [
            f"## Components from Netlist: {netlist_path}",
            "",
            f"**Total Components:** {len(components)}",
            "",
        ]

        shown = []
        for ref, comp in sorted(components.items()):
            if filter_ref and not ref.startswith(filter_ref):
                continue
            shown.append(ref)

            lines.append(f"### {ref}")
            lines.append(f"**Value:** {comp.value}")
            lines.append(f"**Library:** {comp.library}")
            if comp.footprint:
                lines.append(f"**Footprint:** {comp.footprint}")

            if comp.nodes:
                lines.append("")
                lines.append("**Pin Connections:**")
                lines.append("")
                lines.append("| Pin | Net |")
                lines.append("|-----|-----|")
                # Each node: a pin number on more than one net has more rows.
                for node in comp.sorted_nodes():
                    lines.append(f"| {node.number} | {node.net} |")

            lines.append("")

        if note := parser.pins_on_more_nets_note(shown):
            lines.append(note)
        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error reading netlist")
