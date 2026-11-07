"""KiCad netlist parser for accurate component network tracking."""

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from ..errors import InputError
from .kicad_versions import version_triple
from .sheet_list import str_num_cmp

# The order of the pin numbers of KiCad (StrNumCmp).
_NUMBER_KEY = cmp_to_key(str_num_cmp)


@dataclass(frozen=True)
class NetlistNode:
    """A node of a net (<node>): one pin number of a component on the net."""

    reference: str
    number: str
    net: str
    # The pin name and the electrical type that KiCad gives the node
    # (pin_name_of_node; the "+no_connect" of a pintype is not a type).
    name: str
    type: str


@dataclass
class NetlistComponent:
    """Component from netlist file."""

    reference: str
    value: str
    library: str
    footprint: str | None = None
    # The nodes of the component in the order of the netlist. A pin number can be
    # on more than one net: two library pins with the same number, or a stacked pin
    # number of KiCad 10 ("[1-2]") and a pin "2" (NETLIST_EXPORTER_XML::makeListOfNets
    # removes only the nodes of one net with the same reference and the same number
    # text).
    nodes: list[NetlistNode] = field(default_factory=list)
    # The sheet path of the component (<sheetpath>): "names"
    # (SCH_SHEET_PATH::PathHumanReadable, for example "/Power/") and "tstamps"
    # (SCH_SHEET_PATH::PathAsString). Empty: a symbol with no library symbol (no
    # <comp>, only nodes).
    sheet_names: str = ""
    sheet_tstamps: str = ""

    def sorted_nodes(self) -> list[NetlistNode]:
        """Return the nodes in the order of the pin numbers of KiCad (StrNumCmp); the
        nodes of one number keep the netlist order."""
        return sorted(self.nodes, key=lambda node: _NUMBER_KEY(node.number))

    def numbers(self) -> list[str]:
        """Return the pin numbers of the nodes, each one time, in the netlist order."""
        return list(dict.fromkeys(node.number for node in self.nodes))

    def pins_on_more_nets(self) -> dict[str, list[str]]:
        """Return each pin number on more than one net, with its nets."""
        nets: dict[str, list[str]] = {}
        for node in self.nodes:
            nets.setdefault(node.number, [])
            if node.net not in nets[node.number]:
                nets[node.number].append(node.net)
        return {number: names for number, names in nets.items() if len(names) > 1}


@dataclass
class NetlistSheet:
    """A sheet path of the design (<design><sheet>), as kicad-cli writes it."""

    names: str  # "name": SCH_SHEET_PATH::PathHumanReadable
    tstamps: str  # SCH_SHEET_PATH::PathAsString
    source: str  # <title_block><source>: the file name, with no folder


def pin_name_of_node(pinfunction: str, number: str, kicad: str | None) -> str:
    """Return the pin name (SCH_PIN::GetShownName) of the pinfunction of a netlist node.

    KiCad 8 and 9 write the name. KiCad 10 writes "<name>_<number>", the number for
    a stacked pin with no name, and no pinfunction for an other pin with no name
    (NETLIST_EXPORTER_XML, 10.0.0 to 10.0.7 and branch master). With no known KiCad
    version, the text stays (netlist_note).
    """
    numbers = version_triple(kicad) if kicad else None
    if numbers is None or numbers[0] < 10:
        return pinfunction
    if pinfunction == number:
        return ""
    suffix = f"_{number}"
    return pinfunction[: -len(suffix)] if pinfunction.endswith(suffix) else pinfunction


@dataclass
class NetlistNet:
    """Net from netlist file."""

    name: str
    code: int
    nodes: list[NetlistNode] = field(default_factory=list)  # the nodes of pins

    @property
    def pins(self) -> list[tuple[str, str]]:
        """The (reference, pin number) of each node."""
        return [(node.reference, node.number) for node in self.nodes]


class NetlistParser:
    """Parser for KiCad netlist files (.xml).

    The constructors read and parse the XML: they block, thus an async tool makes
    the parser off the event loop (asyncio.to_thread). The methods of the parser use
    only the data in memory.
    """

    def __init__(self, file_path: str) -> None:
        """Read and parse a netlist file.

        Args:
            file_path: Path to .xml netlist file

        Raises:
            FileNotFoundError: If file doesn't exist
            InputError: If file is not a .xml file, or not valid XML
            OSError: The file cannot be read.
        """
        self.file_path: Path | None = Path(file_path)
        self._origin = str(self.file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"Netlist file not found: {file_path}")

        if self.file_path.suffix != ".xml":
            raise InputError(f"Netlist file must be .xml, got: {self.file_path.suffix}")

        self._data = self._parse(self.file_path.read_bytes())

    @classmethod
    def from_bytes(cls, data: bytes, origin: str = "of kicad-cli") -> "NetlistParser":
        """Return a parser of the data of an XML netlist (for example of kicad-cli).

        Args:
            origin: The origin of the data for the messages ("The netlist <origin>
                is not valid XML"), for example "of kicad-cli for <schematic>".

        Raises:
            InputError: The data is not valid XML.
        """
        parser = cls.__new__(cls)
        parser.file_path = None  # no file: the origin names the data
        parser._origin = origin
        parser._data = parser._parse(data)
        return parser

    def _parse(self, data: bytes) -> dict[str, Any]:
        """Parse the XML of the netlist.

        Returns:
            Parsed data structure

        Raises:
            InputError: The data is not valid XML.
        """
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise InputError(f"The netlist {self._origin} is not valid XML: {e}") from e

        # <design><tool>Eeschema 10.0.6</tool>: the KiCad that wrote the netlist.
        tool = root.findtext("design/tool") or ""
        kicad_version = tool.removeprefix("Eeschema ").strip() or None

        # Parse components (KiCad 9.0+ uses <comp> not <component>)
        components = {}
        # A reference of more than one <comp>: the annotation is not complete
        # (an unannotated "U?" or the same reference two times). The dict keeps one.
        duplicates: list[str] = []
        for comp in root.findall(".//comp"):
            ref = comp.get("ref")
            if ref in components and ref not in duplicates:
                duplicates.append(ref)
            value = comp.findtext("value", "")
            # KiCad XML: footprint is a direct <footprint>Lib:Footprint</footprint>
            # element (NOT footprint/libpart), and libsource is an attribute-only
            # element <libsource lib="..." part="..."/> (NOT libsource/libpart).
            footprint = comp.findtext("footprint") or ""
            libsource = comp.find("libsource")
            library = libsource.get("lib", "") if libsource is not None else ""

            sheetpath = comp.find("sheetpath")
            # The nodes come from the nets below.
            components[ref] = NetlistComponent(
                reference=ref,
                value=value,
                library=library,
                footprint=footprint,
                sheet_names=sheetpath.get("names", "") if sheetpath is not None else "",
                sheet_tstamps=sheetpath.get("tstamps", "") if sheetpath is not None else "",
            )

        # Parse nets and populate component pins
        nets = {}
        for net in root.findall(".//net"):
            code_attr = net.get("code")
            code = int(code_attr) if code_attr and code_attr.lstrip("-").isdigit() else -1
            name = net.get("name")
            if name is None:
                # KiCad gives each net a name (an unnamed net gets "Net-(...)").
                raise InputError(f"The netlist {self._origin} has a net with no name.")

            nodes = []
            for node in net.findall("node"):
                ref = node.get("ref")
                pin_num = node.get("pin")
                if ref and pin_num:
                    item = NetlistNode(
                        reference=ref,
                        number=pin_num,
                        net=name,
                        name=pin_name_of_node(node.get("pinfunction", ""), pin_num, kicad_version),
                        type=node.get("pintype", "").replace("+no_connect", ""),
                    )
                    nodes.append(item)
                    # KiCad writes no <comp> for a symbol with no library symbol, but its
                    # pins are in the nets: the component has only these nodes.
                    if ref not in components:
                        components[ref] = NetlistComponent(reference=ref, value="", library="")
                    components[ref].nodes.append(item)

            nets[name] = NetlistNet(name=name, code=code, nodes=nodes)

        sheets = [
            NetlistSheet(
                names=sheet.get("name", ""),
                tstamps=sheet.get("tstamps", ""),
                source=sheet.findtext("title_block/source", ""),
            )
            for sheet in root.findall("design/sheet")
        ]

        return {
            "components": components,
            "nets": nets,
            "sheets": sheets,
            "duplicates": duplicates,
            "kicad_version": kicad_version,
        }

    @property
    def kicad_version(self) -> str | None:
        """The version of the KiCad that wrote the netlist (<design><tool>), or None."""
        return self._data["kicad_version"]

    def netlist_note(self) -> str:
        """Return the warning when the netlist has no KiCad version that the server can
        read, or ""."""
        if self.kicad_version is not None and version_triple(self.kicad_version):
            return ""
        return (
            f"⚠️ The netlist has no KiCad version that the server can read "
            f"({self.kicad_version!r}): a pin name of KiCad 10 can have "
            '"_<number>" at the end (NETLIST_EXPORTER_XML).'
        )

    def get_components(self) -> dict[str, NetlistComponent]:
        """Get all components from netlist.

        Returns:
            Dictionary mapping reference to component
        """
        data = self._data
        return data["components"]

    def get_duplicate_references(self) -> list[str]:
        """Get the references of more than one <comp> (get_components keeps one)."""
        return self._data["duplicates"]

    def get_sheets(self) -> list[NetlistSheet]:
        """Get the sheet paths of the design, in the order of the netlist."""
        return self._data["sheets"]

    def get_nets(self) -> dict[str, NetlistNet]:
        """Get all nets from netlist.

        Returns:
            Dictionary mapping net name to net
        """
        data = self._data
        return data["nets"]

    def pins_on_more_nets_note(
        self, references: Iterable[str] | None = None, number: str | None = None
    ) -> str:
        """Return the warning about the pin numbers on more than one net, or "".

        A symbol whose duplicate pin numbers are jumpers (KiCad 10) has one net for
        the pins with one number text, but a stacked pin number ("[1-2]") and a pin
        "2" can still be on two nets: the warning is also for such a symbol.

        Args:
            references: The components of the answer; None: all components.
            number: Only this pin number (the answer of one pin); None: all pins.
        """
        components = self.get_components()
        selected = components if references is None else dict.fromkeys(references)
        # A reference of more than one <comp> has the nodes of each of these symbols:
        # its pins on more nets are an annotation error, not two pins with one number.
        duplicates = set(self.get_duplicate_references())
        items = []
        shared = []
        for reference in selected:
            component = components.get(reference)
            if component is None:
                continue
            for pin, nets in component.pins_on_more_nets().items():
                if number is None or pin == number:
                    item = f"{reference}.{pin} ({', '.join(nets)})"
                    (shared if reference in duplicates else items).append(item)
        notes = []
        if items:
            notes.append(
                "⚠️ The netlist has these pin numbers on more than one net: "
                f"{', '.join(items)}. The tool gives each net of such a pin. The ERC of "
                "KiCad 9 and 10 gives the error duplicate_pins for two pins with the "
                "same number on different nets (ERC_TESTER::TestDuplicatePinNets), "
                "except for a symbol whose duplicate pin numbers are jumpers."
            )
        if shared:
            notes.append(
                "⚠️ More than one component of the netlist has the reference of these "
                f"pins (an annotation error): {', '.join(shared)}. The tool gives the "
                "nets of all these components. Annotate the schematic."
            )
        return "\n\n".join(notes)

    def trace_connection(self, reference: str, pin_number: str | None = None) -> dict[str, Any]:
        """Trace connections from a component pin.

        Args:
            reference: Component reference
            pin_number: Optional pin number (if None, trace all pins)

        Returns:
            Connection information. With pin_number: "component", "pin" and "nets",
            a list with "net" and "connected_to" for each net of the pin (a pin
            number can be on more than one net). With no pin_number: "component"
            and "nets", the net name -> a list with "pin" and "connected_to" for each
            pin of the component on the net. "connected_to" is the (reference, pin)
            of each node of an other component on the net. Two nodes of one pin
            number on one net (KiCad 10: "[1-2]" and "2") are one entry. An unknown
            component or pin gives "error".
        """
        components = self.get_components()
        if reference not in components:
            return {"error": f"Component {reference} not found in netlist"}

        comp = components[reference]
        nets = self.get_nets()

        def connected_to(net_name: str) -> list[tuple[str, str]]:
            net = nets.get(net_name)
            nodes = net.nodes if net is not None else []
            return [(node.reference, node.number) for node in nodes if node.reference != reference]

        # Each (pin number, net) one time, in the netlist order.
        pairs = list(dict.fromkeys((node.number, node.net) for node in comp.nodes))
        if pin_number:
            pin_nets = [net for number, net in pairs if number == pin_number]
            if not pin_nets:
                return {"error": f"Pin {pin_number} not found in component {reference}"}
            return {
                "component": reference,
                "pin": pin_number,
                "nets": [{"net": net, "connected_to": connected_to(net)} for net in pin_nets],
            }

        # A component can have several pins on the same net (relay contacts, more
        # GND pins): a list for each net.
        net_connections: dict[str, list[dict[str, Any]]] = {}
        for number, net in pairs:
            net_connections.setdefault(net, []).append(
                {"pin": number, "connected_to": connected_to(net)}
            )
        return {"component": reference, "nets": net_connections}
