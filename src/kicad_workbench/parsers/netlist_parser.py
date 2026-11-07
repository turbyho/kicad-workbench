"""KiCad netlist parser for accurate component network tracking."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import InputError
from .kicad_versions import version_triple


@dataclass
class NetlistComponent:
    """Component from netlist file."""

    reference: str
    value: str
    library: str
    footprint: str | None = None
    pins: dict[str, str] = field(default_factory=dict)  # pin_number -> net_name
    # pin_number -> the pin name and the electrical type that KiCad gives the node
    # (pin_name_of_node; the "+no_connect" of a pintype is not a type).
    pin_names: dict[str, str] = field(default_factory=dict)
    pin_types: dict[str, str] = field(default_factory=dict)
    # The sheet path of the component (<sheetpath>): "names"
    # (SCH_SHEET_PATH::PathHumanReadable, for example "/Power/") and "tstamps"
    # (SCH_SHEET_PATH::PathAsString). Empty: a symbol with no library symbol (no
    # <comp>, only nodes).
    sheet_names: str = ""
    sheet_tstamps: str = ""


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
    pins: list[tuple[str, str]] = field(default_factory=list)  # (reference, pin_number)


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
            # Pins will be populated from nets later
            components[ref] = NetlistComponent(
                reference=ref,
                value=value,
                library=library,
                footprint=footprint,
                pins={},  # Empty initially, will populate from nets
                sheet_names=sheetpath.get("names", "") if sheetpath is not None else "",
                sheet_tstamps=sheetpath.get("tstamps", "") if sheetpath is not None else "",
            )

        # Parse nets and populate component pins
        nets = {}
        for net in root.findall(".//net"):
            code_attr = net.get("code")
            code = int(code_attr) if code_attr and code_attr.lstrip("-").isdigit() else -1
            name = net.get("name")

            pins_list = []
            for node in net.findall("node"):
                ref = node.get("ref")
                pin_num = node.get("pin")
                if ref and pin_num:
                    pins_list.append((ref, pin_num))

                    # Populate component pins. KiCad writes no <comp> for a symbol with
                    # no library symbol, but its pins are in the nets: the component has
                    # only these pins.
                    if ref not in components:
                        components[ref] = NetlistComponent(reference=ref, value="", library="")
                    components[ref].pins[pin_num] = name
                    components[ref].pin_names[pin_num] = pin_name_of_node(
                        node.get("pinfunction", ""), pin_num, kicad_version
                    )
                    components[ref].pin_types[pin_num] = node.get("pintype", "").replace(
                        "+no_connect", ""
                    )

            nets[name] = NetlistNet(name=name, code=code, pins=pins_list)

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

    def get_component_nets(self, reference: str) -> dict[str, list[str]]:
        """Get all networks for a component.

        Args:
            reference: Component reference (e.g., "R16")

        Returns:
            Dictionary mapping net name to list of connected pins
        """
        components = self.get_components()
        if reference not in components:
            return {}

        comp = components[reference]
        net_pins = {}

        for pin_num, net_name in comp.pins.items():
            if net_name not in net_pins:
                net_pins[net_name] = []
            net_pins[net_name].append(pin_num)

        return net_pins

    def get_net_components(self, net_name: str) -> list[tuple[str, str]]:
        """Get all components connected to a net.

        Args:
            net_name: Net name (e.g., "PMIC_I2C_SCL")

        Returns:
            List of (reference, pin_number) tuples
        """
        nets = self.get_nets()
        if net_name not in nets:
            return []

        return nets[net_name].pins

    def trace_connection(self, reference: str, pin_number: str | None = None) -> dict[str, Any]:
        """Trace connections from a component pin.

        Args:
            reference: Component reference
            pin_number: Optional pin number (if None, trace all pins)

        Returns:
            Connection information
        """
        components = self.get_components()
        if reference not in components:
            return {"error": f"Component {reference} not found in netlist"}

        comp = components[reference]

        if pin_number:
            # Trace specific pin
            if pin_number not in comp.pins:
                return {"error": f"Pin {pin_number} not found in component {reference}"}

            net_name = comp.pins[pin_number]
            nets = self.get_nets()
            connected = nets[net_name].pins if net_name in nets else []

            return {
                "component": reference,
                "pin": pin_number,
                "net": net_name,
                "connected_to": [(ref, pin) for ref, pin in connected if ref != reference],
            }

        else:
            # Trace all pins. A component can have several pins on the same net
            # (relay contacts, multiple GND pins), so group as a list per net
            # instead of overwriting earlier pins.
            net_connections: dict[str, list[dict[str, Any]]] = {}
            for pin_num, net_name in comp.pins.items():
                nets = self.get_nets()
                connected = nets[net_name].pins if net_name in nets else []
                net_connections.setdefault(net_name, []).append(
                    {
                        "pin": pin_num,
                        "connected_to": [
                            (ref, pin) for ref, pin in connected if ref != reference
                        ],
                    }
                )

            return {
                "component": reference,
                "nets": net_connections,
            }
