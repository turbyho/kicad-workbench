"""Read the KiCad netlist of a board as pcbnew reads it.

"Update PCB from Schematic" gives pcbnew the netlist of NETLIST_EXPORTER_KICAD
with GNL_ALL | GNL_OPT_KICAD. "kicad-cli sch export netlist --format
kicadsexpr" writes the same netlist (KiCad 8.0.9, 9.0.9 and 10.0.6). It has
no power symbol, no symbol with a reference that starts with "#", and no
symbol that is excluded from the board. Each reference has one component with
the pins of all its units.

KICAD_NETLIST_PARSER reads the (ref ...), (value ...) and (footprint ...) of
each (comp ...). Each (node ...) of a (net ...) with a code of 1 or more adds
its (pin ...), (pinfunction ...) and (pintype ...) to the component of its
reference.
"""

from dataclasses import dataclass, field

from .c_numbers import strtol
from .sexpr import Node, parse


@dataclass(frozen=True)
class NetlistPin:
    number: str
    # The (pinfunction ...) of the node: the pin name in KiCad 8 and 9; in KiCad 10
    # "<name>_<number>", or the number of a stacked pin with no name
    # (NETLIST_EXPORTER_XML). "" for a pin with no name.
    function: str
    type: str  # The electrical type, for example "passive".


@dataclass
class NetlistComponent:
    reference: str
    value: str
    footprint: str  # The library ID of the Footprint field, or "".
    pins: list[NetlistPin] = field(default_factory=list)


def _text(node: Node, head: str) -> str:
    item = node.first(head)
    return item.value(0, "") if item is not None else ""


def read_board_netlist(text: str) -> list[NetlistComponent]:
    """Return the components of a netlist, with their pins.

    Raises:
        SExprError: The text is not an S-expression.
    """
    root = parse(text)
    components: dict[str, NetlistComponent] = {}
    section = root.first("components")
    for comp in section.children("comp") if section is not None else []:
        reference = _text(comp, "ref")
        # GetComponentByReference finds the first component of a reference.
        components.setdefault(
            reference, NetlistComponent(reference, _text(comp, "value"), _text(comp, "footprint"))
        )
    section = root.first("nets")
    for net in section.children("net") if section is not None else []:
        # strtol( code ) >= 1
        if strtol(_text(net, "code")) < 1:
            continue
        for node in net.children("node"):
            component = components.get(_text(node, "ref"))
            if component is not None:
                component.pins.append(
                    NetlistPin(
                        _text(node, "pin"), _text(node, "pinfunction"), _text(node, "pintype")
                    )
                )
    return list(components.values())


@dataclass(frozen=True)
class CvpcbPart:
    """What CvPcb knows of the library symbol of a component: the pin count and the
    footprint filters (COMPONENT::GetPinCount, GetFootprintFilters)."""

    pin_count: int
    filters: tuple[str, ...]


def read_cvpcb_parts(text: str) -> dict[str, CvpcbPart]:
    """Return the pin count and the footprint filters of each component of a netlist,
    by reference, as CvPcb reads them (KICAD_NETLIST_PARSER::parseLibPartList).

    The (libsource (lib ...) (part ...)) of a component names its (libpart ...). A
    libpart gives its pin count (the number of its (pin ...) items) and its filters
    (the (fp ...) items that are not empty) to each component of its lib and part, or
    of one of its (alias ...) names: a later libpart replaces the values of an earlier
    one. A component of no libpart has no pins and no filters (the COMPONENT
    constructor).

    Raises:
        SExprError: The text is not an S-expression.
    """
    root = parse(text)
    sources: dict[str, tuple[str, str]] = {}
    section = root.first("components")
    for comp in section.children("comp") if section is not None else []:
        libsource = comp.first("libsource")
        source = (
            (_text(libsource, "lib"), _text(libsource, "part"))
            if libsource is not None
            else ("", "")
        )
        # GetComponentByReference finds the first component of a reference.
        sources.setdefault(_text(comp, "ref"), source)
    parts = dict.fromkeys(sources, CvpcbPart(0, ()))
    section = root.first("libparts")
    for libpart in section.children("libpart") if section is not None else []:
        library, name = _text(libpart, "lib"), _text(libpart, "part")
        # The parser adds the items of each (footprints ...), (pins ...) and
        # (aliases ...) of the libpart.
        filters = tuple(
            value
            for footprints in libpart.children("footprints")
            for fp in footprints.children("fp")
            if (value := fp.value(0, ""))
        )
        count = sum(len(pins.children("pin")) for pins in libpart.children("pins"))
        names = {name} | {
            alias.value(0, "")
            for aliases in libpart.children("aliases")
            for alias in aliases.children("alias")
        }
        for reference, (source_library, source_name) in sources.items():
            if source_library == library and source_name in names:
                parts[reference] = CvpcbPart(count, filters)
    return parts
