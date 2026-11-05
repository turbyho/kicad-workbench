"""Text parser for KiCad schematic files (.kicad_sch)."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.file_handlers import validate_kicad_file
from ..utils.kicad_text import read_as_kicad
from ..utils.sch_hierarchy import sheet_files
from .kicad_fields import (
    FieldRules,
    record_field_rules,
    rules_for_file,
    sheet_fields,
    symbol_fields,
)
from .kicad_versions import KicadFileError, file_text
from .lib_symbols import LibrarySymbol, pin_number, sub_symbols, symbol_name
from .sch_geometry import definition_key as _lib_key
from .sexpr import Node, parse

# KiCad-defined boolean flags (fixed set, not user-extensible)
KICAD_SYMBOL_FLAGS = ("dnp", "in_bom", "on_board", "exclude_from_sim")
KICAD_FLAG_DEFAULTS = {"dnp": False, "in_bom": True, "on_board": True, "exclude_from_sim": False}


@dataclass
class SchematicComponent:
    """Component from schematic file."""

    reference: str
    value: str
    library_id: str
    footprint: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    position: tuple[float, float] = (0.0, 0.0)
    unit: int | None = None
    pins: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=lambda: dict(KICAD_FLAG_DEFAULTS))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchematicComponent":
        """Create from a dict of the parser (_parse_file)."""
        # Extract properties
        properties = {}
        for prop in data.get("properties", []):
            key = prop.get("key", "")
            value = prop.get("value", "")
            if key and value:
                properties[key] = value

        # Get position
        at = data.get("at", {})
        position = (float(at.get("x", 0)), float(at.get("y", 0)))

        # Get unit
        unit = data.get("unit")

        return cls(
            reference=data.get("reference", ""),
            value=properties.get("Value", data.get("value", "")),
            library_id=data.get("lib_id", ""),
            footprint=properties.get("Footprint"),
            properties=properties,
            position=position,
            unit=unit,
            pins=data.get("pins", []),
            flags=data.get("flags", dict(KICAD_FLAG_DEFAULTS)),
        )


@dataclass
class SchematicNet:
    """Net from schematic file."""

    name: str
    code: int
    node_count: int = 0
    pins: list[str] = field(default_factory=list)
    type: str = "unknown"
    position: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchematicNet":
        """Create from a dict of the parser (_parse_file)."""
        return cls(
            name=data.get("name", ""),
            code=data.get("code", 0),
            type=data.get("type", "unknown"),
            position=data.get("position", (0.0, 0.0)),
        )


@dataclass
class SchematicPin:
    """Pin definition from symbol."""

    number: str
    name: str
    type: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchematicPin":
        """Create from a dict of the parser (_parse_file)."""
        return cls(
            number=data.get("number", ""),
            name=data.get("name", ""),
            type=data.get("electrical_type", ""),
        )


def _merge_duplicate_references(
    components: list[SchematicComponent],
) -> list[SchematicComponent]:
    """Merge multi-unit symbol instances that share a reference.

    KiCad stores each unit of a multi-unit symbol as its own symbol block
    (often spread across sub-sheets), but the physical component is one
    entity — netlist lookups and per-reference reports expect it once. Pin
    metadata from all units is unioned by pin number; the first instance's
    value/footprint wins (units share them by construction).
    """
    merged: dict[str, SchematicComponent] = {}
    for comp in components:
        key = comp.reference or f"__unannotated_{len(merged)}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = comp
            continue
        known_pins = {str(p.get("number", "")) for p in existing.pins}
        for p in comp.pins:
            if str(p.get("number", "")) not in known_pins:
                existing.pins.append(p)
    return list(merged.values())


# The escapes that KiCad writes in the text of a label, and their characters.
_TEXT_ESCAPES = {
    "dblquote": '"',
    "quote": "'",
    "lt": "<",
    "gt": ">",
    "backslash": "\\",
    "slash": "/",
    "bar": "|",
    "comma": ",",
    "colon": ":",
    "space": " ",
    "dollar": "$",
    "tab": "\t",
    "return": "\n",
    "brace": "{",
}


def _shown_text(text: str) -> str:
    """Return the text of a label as KiCad shows it and names its net.

    KiCad writes some characters of a label as escapes, for example "{slash}"
    for "/". Braces after "$", "~", "^" or "_" are markup (a variable,
    an overbar, a superscript, a subscript) and stay. An unknown escape and
    a brace with no end also stay.
    """
    result = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            result.append(text[i])
            i += 1
            continue
        depth = 1
        end = i + 1
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        token = text[i + 1 : end]
        if end >= len(text):
            result.append("{" + _shown_text(token))
        elif i > 0 and text[i - 1] in "$~^_":
            result.append("{" + _shown_text(token) + "}")
        elif token in _TEXT_ESCAPES:
            result.append(_TEXT_ESCAPES[token])
        else:
            result.append("{" + _shown_text(token) + "}")
        i = end + 1
    return "".join(result)


def _definition_pins(definition: Node, file: Path, version: int) -> list[dict[str, Any]]:
    """Return the pins of a symbol definition, with their unit and body style.

    The pins of a unit are in the sub-symbol "<name>_<unit>_<style>" (the name
    rule of lib_symbols.sub_symbols). A pin outside a sub-symbol is in unit 1
    with body style 1 (parseLibSymbol of KiCad 8, 9 and 10). Unit 0 and style 0
    are common to all units and styles. A pin with no (number ...) has an empty
    number. The name and the number are the texts of the netlist (file_text: KiCad
    8 and 9 give "~" as an empty text with GetShownName and GetShownNumber).

    Raises:
        KicadFileError: KiCad cannot read the name of a sub-symbol.
    """
    symbol = LibrarySymbol(symbol_name(definition.value(0, "")), definition, file, version)
    pins = []
    for unit, style, group in [(1, 1, definition), *sub_symbols(symbol)]:
        # (pin <electrical type> <graphic style> ... (name "X") (number "N"))
        for pin in group.children("pin"):
            name = pin.first("name")
            number = pin.first("number")
            pins.append({
                "unit": unit,
                "style": style,
                "number": pin_number(file_text(number.value(0, "") if number else "", version)),
                "name": file_text(name.value(0, "") if name else "", version),
                "electrical_type": pin.value(0, ""),
            })
    return pins


def _position(item: Node) -> tuple[float, float]:
    """Return the (at x y) position of an item, or (0, 0)."""
    at = item.first("at")
    return (at.number(0, 0.0), at.number(1, 0.0)) if at else (0.0, 0.0)


class SchematicParser:
    """Parser for KiCad schematic files (.kicad_sch)."""

    def __init__(self, file_path: str) -> None:
        """Initialize parser with schematic file.

        Args:
            file_path: Path to .kicad_sch file

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If file is not a .kicad_sch file
        """
        self.file_path = validate_kicad_file(file_path, ".kicad_sch")
        self._data: dict[str, Any] | None = None
        self._tree: Node | None = None
        self._items: dict[str | None, list[Node]] = {}
        # The pins of each symbol definition, by its key in lib_symbols.
        self._lib_pins: dict[str, list[dict[str, Any]]] | None = None
        self._power_keys: set[str] = set()
        self._power_net_keys: set[str] = set()
        self._rules: FieldRules | None = None

    def _root(self) -> Node:
        """Return the parsed tree of the file.

        The parser reads the file one time. A parser made before a change of
        the file does not see the change.

        Raises:
            KicadFileError: The file version is not of KiCad 8 or later, or
                it is newer than the versions that this server knows.
        """
        if self._tree is None:
            tree = parse(read_as_kicad(self.file_path))
            try:
                self._rules = rules_for_file(tree)
            except KicadFileError as e:
                raise KicadFileError(f"{self.file_path}: {e}") from e
            self._tree = tree
            for item in tree.children():
                self._items.setdefault(item.head, []).append(item)
        return self._tree

    @property
    def field_rules(self) -> FieldRules:
        """The rules of the KiCad version that saved the file (see kicad_fields)."""
        self._root()
        assert self._rules is not None
        return self._rules

    def _rules_for_fields(self) -> FieldRules:
        """Return the field rules, and record them for the note of the tool call.

        Each read records the rules: a parser can be made outside of the tool
        call that reads its fields.
        """
        rules = self.field_rules
        record_field_rules(rules)
        return rules

    def _symbol_fields(self, symbol: Node) -> dict[str, str]:
        return symbol_fields(symbol, self._rules_for_fields())

    def _sheet_fields(self, sheet: Node) -> dict[str, str]:
        try:
            return sheet_fields(sheet, self._rules_for_fields())
        except KicadFileError as e:
            raise KicadFileError(f"{self.file_path}: {e}") from e

    def _top(self, head: str) -> list[Node]:
        """Return the top-level items of the sheet with the head."""
        self._root()
        return self._items.get(head, [])

    def _parse_file(self) -> dict[str, Any]:
        """Parse the schematic file.

        Returns:
            Parsed data structure

        Raises:
            SExprError: The file is not a valid S-expression.
        """
        if self._data is not None:
            # The data holds fields: the note of their rules applies again.
            self._rules_for_fields()
            return self._data

        root = self._root()
        # Parse lib_symbols first so _parse_components can use it
        self._read_lib_symbols()

        self._data = {
            "path": str(self.file_path),
            "title_block": self._parse_title_block(root),
            "components": self._parse_components(),
            "nets": self._parse_nets(),
            "sheets": self._parse_sheets(),
        }

        return self._data

    def _read_lib_symbols(self) -> None:
        """Read the symbol definitions of the sheet one time.

        A power symbol (power, power global or power local in its definition)
        is not a component. It gives its Value as a net name only when it has
        a pin of the type power_in (SCH_PIN::IsGlobalPower and IsLocalPower of
        KiCad). It can come from any library. PWR_FLAG has a power_out pin
        and names no net.
        """
        if self._lib_pins is not None:
            return
        self._lib_pins = {}
        for lib_symbols in self._top("lib_symbols"):
            for definition in lib_symbols.children("symbol"):
                key = definition.value(0, "")
                try:
                    pins = _definition_pins(
                        definition, Path(self.file_path), self.field_rules.file_version
                    )
                except KicadFileError as e:
                    raise KicadFileError(f"{self.file_path}: {e}") from e
                self._lib_pins[key] = pins
                if definition.first("power") is not None:
                    self._power_keys.add(key)
                    if any(pin["electrical_type"] == "power_in" for pin in pins):
                        self._power_net_keys.add(key)

    def _instance_pins(self, symbol: Node) -> dict[str, dict[str, Any]]:
        """Return the pins of the definition of a symbol instance, by number.

        Only the pins of the unit and of the body style of the instance count
        (unit 0 and style 0 are common to all). The first pin of a number wins.
        """
        unit_node = symbol.first("unit")
        style_node = symbol.first("body_style") or symbol.first("convert")
        unit = int(unit_node.number(0, 1)) if unit_node else 1
        style = int(style_node.number(0, 1)) if style_node else 1
        pins: dict[str, dict[str, Any]] = {}
        for pin in self._lib_pins.get(_lib_key(symbol), []):
            if pin["unit"] in (0, unit) and pin["style"] in (0, style):
                pins.setdefault(pin["number"], pin)
        return pins

    def _parse_title_block(self, root: Node) -> dict[str, str]:
        """Parse title block from schematic."""
        title_block = {
            "title": "",
            "date": "",
            "rev": "",
            "company": "",
            "comment": "",
        }
        block = root.first("title_block")
        if block:
            for key in ("title", "date", "rev", "company"):
                item = block.first(key)
                if item:
                    title_block[key] = item.value(0, "")
            # (comment 1 "text"): the first comment.
            comment = block.first("comment")
            if comment:
                title_block["comment"] = comment.value(1, "")
        return title_block

    def _parse_components(self) -> list[dict[str, Any]]:
        """Parse the symbol instances of the sheet.

        A power symbol and a symbol with a reference that starts with "#"
        are not components.
        """
        components = []
        for symbol in self._top("symbol"):
            lib_id_node = symbol.first("lib_id")
            if lib_id_node is None or _lib_key(symbol) in self._power_keys:
                continue
            lib_id = lib_id_node.value(0, "")
            fields = self._symbol_fields(symbol)
            reference = fields.get("Reference", "")
            if not reference or reference.startswith("#"):
                continue
            value = fields.get("Value", "")
            footprint = fields.get("Footprint") or None
            x, y = _position(symbol)

            # Pins of the instance, with the data of the library definition
            lib_pin_data = self._instance_pins(symbol)
            pins = []
            for pin in symbol.children("pin"):
                # The (pin "N") of the file has the number of GetNumber: the netlist
                # number is file_text, as for the pins of the definition.
                pin_num = file_text(pin.value(0, ""), self.field_rules.file_version)
                pin_info = lib_pin_data.get(pin_num, {})
                pins.append({
                    "number": pin_num,
                    "name": pin_info.get("name", ""),
                    "electrical_type": pin_info.get("electrical_type", ""),
                })

            flags = dict(KICAD_FLAG_DEFAULTS)
            for flag in KICAD_SYMBOL_FLAGS:
                item = symbol.first(flag)
                if item is not None and item.value(0) in ("yes", "no"):
                    flags[flag] = item.value(0) == "yes"

            properties = [
                {"key": "Reference", "value": reference},
                {"key": "Value", "value": value},
            ]
            if footprint:
                properties.append({"key": "Footprint", "value": footprint})

            components.append({
                "lib_id": lib_id,
                "reference": reference,
                "value": value,
                "properties": properties,
                "at": {"x": x, "y": y},
                "pins": pins,
                "flags": flags,
            })
        return components

    def _labels(self, *kinds: str) -> list[dict[str, Any]]:
        """Return the labels of the kinds, in the order of the file.

        The kinds are "global_label", "label" and "hierarchical_label".
        """
        items = sorted(
            (item for kind in kinds for item in self._top(kind)), key=lambda item: item.start
        )
        labels = []
        for item in items:
            name = _shown_text(item.value(0, ""))
            if name:
                labels.append({"name": name, "position": _position(item)})
        return labels

    def _power_symbols(self) -> list[dict[str, Any]]:
        """Return the power symbols that name a net.

        The net name is the shown text of the Value field, as in KiCad.
        """
        self._read_lib_symbols()
        power = []
        for symbol in self._top("symbol"):
            if _lib_key(symbol) in self._power_net_keys:
                name = _shown_text(self._symbol_fields(symbol).get("Value", ""))
                if name:
                    power.append({"name": name, "position": _position(symbol)})
        return power

    def _parse_nets(self) -> list[dict[str, Any]]:
        """Parse the net names of the labels and of the power symbols.

        A name is in the list one time. The kinds come in this order: global
        labels, local labels, hierarchical labels, power symbols.
        """
        nets: dict[str, dict[str, Any]] = {}
        groups = (
            ("global", self._labels("global_label")),
            ("local", self._labels("label")),
            ("hierarchical", self._labels("hierarchical_label")),
            ("power", self._power_symbols()),
        )
        for net_type, items in groups:
            for item in items:
                if item["name"] not in nets:
                    nets[item["name"]] = {
                        "name": item["name"],
                        "code": len(nets),
                        "type": net_type,
                        "position": item["position"],
                    }
        return list(nets.values())

    def _parse_sheets(self) -> list[dict[str, str]]:
        """Parse hierarchical sheets.

        The name and the file of a sheet are the fields that the KiCad version
        of the file uses (see kicad_fields.sheet_fields).
        """
        sheets = []
        for sheet in self._top("sheet"):
            fields = self._sheet_fields(sheet)
            name = fields.get("Sheetname")
            file = fields.get("Sheetfile")
            if name and file:
                sheets.append({"name": name, "file": file})
        return sheets

    def get_components(self, recursive: bool = False) -> list[SchematicComponent]:
        """Get all components from schematic.

        Args:
            recursive: Also include components from hierarchical sub-sheets,
                following Sheetfile references (each file one time, cycle-safe).
                Real designs keep most components in sub-sheets, so callers
                that cross-reference a full-hierarchy netlist must pass True.

        Returns:
            List of components
        """
        data = self._parse_file()
        components = [SchematicComponent.from_dict(c) for c in data["components"]]
        if recursive:
            # sheet_files() resolves the sheet files as KiCad does. A missing
            # or unparsable sub-sheet is skipped: a broken reference must not
            # wipe out the components of the parent. Each sheet is read with
            # the rules of its own file version. A sheet that this server cannot
            # read (KicadFileError) is an error: the list would not be complete.
            for sheet in sheet_files(self.file_path)[1:]:
                try:
                    sub_data = SchematicParser(str(sheet))._parse_file()
                except (FileNotFoundError, ValueError, OSError):
                    continue
                components.extend(
                    SchematicComponent.from_dict(c) for c in sub_data["components"]
                )
            components = _merge_duplicate_references(components)
        return components

    def get_nets(self) -> list[SchematicNet]:
        """Get all nets from schematic.

        Returns:
            List of nets
        """
        data = self._parse_file()
        return [SchematicNet.from_dict(n) for n in data["nets"]]

    def get_title_block(self) -> dict[str, str]:
        """Get title block information.

        Returns:
            Dictionary with title, date, rev, company, comment
        """
        data = self._parse_file()
        return data["title_block"]

    def get_sheets(self) -> list[dict[str, str]]:
        """Get hierarchical sheets.

        Returns:
            List of sheet information
        """
        data = self._parse_file()
        return data["sheets"]

    def get_component_by_reference(self, reference: str) -> SchematicComponent | None:
        """Get a component by its reference designator.

        Args:
            reference: Component reference (e.g., "R1", "U1")

        Returns:
            Component if found, None otherwise
        """
        for component in self.get_components():
            if component.reference == reference:
                return component
        return None

    def search_components(self, pattern: str) -> list[SchematicComponent]:
        """Search for components by pattern.

        Args:
            pattern: Search pattern (matches reference, value, or library_id)

        Returns:
            List of matching components
        """
        regex = re.compile(pattern, re.IGNORECASE)
        results = []

        for component in self.get_components():
            if (
                regex.search(component.reference)
                or regex.search(component.value)
                or regex.search(component.library_id)
            ):
                results.append(component)

        return results

    def get_component_connections(self, reference: str) -> dict[str, Any]:
        """Get all network connections for a component.

        Args:
            reference: Component reference (e.g., "R16")

        Returns:
            Dictionary with connection information:
            {
                "nets": ["net_name1", "net_name2"],
                "labels": ["label1", "label2"],
                "connected_components": ["comp1", "comp2"]
            }
        """
        comp = self.get_component_by_reference(reference)
        if not comp:
            return {"error": f"Component {reference} not found"}

        pins = [pin["number"] for pin in comp.pins]
        cx, cy = comp.position

        # Find all labels and global labels, then the hierarchical labels
        labels = []
        for label in self._labels("label", "global_label") + self._labels("hierarchical_label"):
            lx, ly = label["position"]
            dist = ((lx - cx)**2 + (ly - cy)**2)**0.5
            labels.append({"name": label["name"], "position": (lx, ly), "distance": dist})

        # Filter to nearby labels (within 20mm)
        nearby_labels = [label for label in labels if label["distance"] < 20]

        # Find nearby components (within 15mm)
        components = self.get_components()
        nearby_comps = []
        for c in components:
            if c.reference == reference:
                continue
            dist = ((c.position[0] - cx)**2 + (c.position[1] - cy)**2)**0.5
            if dist < 15:
                nearby_comps.append({
                    "reference": c.reference,
                    "value": c.value,
                    "distance": dist,
                    "position": c.position
                })

        return {
            "component": reference,
            "position": comp.position,
            "pins": pins,
            "nearby_labels": nearby_labels[:10],
            "nearby_components": nearby_comps[:10],
        }

    def trace_net(self, reference: str, pin_number: str | None = None) -> dict[str, Any]:
        """Trace network connections from a component pin.

        Args:
            reference: Component reference (e.g., "R16")
            pin_number: Optional pin number to trace (if None, trace all pins)

        Returns:
            Network trace information
        """
        connections = self.get_component_connections(reference)

        if "error" in connections:
            return connections

        # Analyze the nearby data to infer network connections
        inferred_nets = []

        # Check for hierarchical labels that indicate function
        for label in connections["nearby_labels"]:
            label_name = label["name"]
            if any(keyword in label_name.upper() for keyword in
                   ["I2C", "SCL", "SDA", "SMBUS", "PMIC", "GPIO", "EN", "INT"]):
                inferred_nets.append({
                    "name": label_name,
                    "type": "signal",
                    "distance": label["distance"]
                })

        return {
            "component": reference,
            "position": connections["position"],
            "inferred_connections": inferred_nets,
            "nearby_components": connections["nearby_components"][:5],
        }

    def build_wire_network(self) -> dict[tuple[float, float], list[tuple[float, float]]]:
        """Build a graph of wire connections.

        Returns:
            Dictionary mapping each point to its connected neighbors
        """
        network = {}

        # (wire (pts (xy x1 y1) (xy x2 y2)) ...)
        for wire in self._top("wire"):
            pts = wire.first("pts")
            points = pts.children("xy") if pts else []
            if len(points) < 2:
                continue
            p1 = (points[0].number(0, 0.0), points[0].number(1, 0.0))
            p2 = (points[1].number(0, 0.0), points[1].number(1, 0.0))

            if p1 not in network:
                network[p1] = []
            if p2 not in network:
                network[p2] = []

            network[p1].append(p2)
            network[p2].append(p1)

        # Find junctions and merge connections
        for junction in self._top("junction"):
            jx, jy = _position(junction)
            # For a junction, all wires meeting at this point should be connected
            # Find all wire endpoints at this position (with small tolerance)
            tolerance = 0.01  # 0.01mm tolerance
            connected_points = [p for p in network if
                               abs(p[0] - jx) < tolerance and abs(p[1] - jy) < tolerance]

            # Merge all connections at junction
            all_neighbors = set()
            for p in connected_points:
                all_neighbors.update(network[p])

            for p in connected_points:
                network[p] = list(all_neighbors)

        return network

    def trace_wire_network(self, reference: str, max_depth: int = 20) -> dict[str, Any]:
        """Trace wire connections from a component.

        Args:
            reference: Component reference (e.g., "R16")
            max_depth: Maximum connection depth to trace

        Returns:
            Dictionary with traced connections and labels
        """
        # Get component position
        comp = self.get_component_by_reference(reference)
        if not comp:
            return {"error": f"Component {reference} not found"}

        cx, cy = comp.position

        # Build wire network
        network = self.build_wire_network()

        # Find the hierarchical labels and the global labels
        h_labels = self._labels("hierarchical_label")
        g_labels = self._labels("global_label")

        all_labels = h_labels + g_labels

        # Start from component position and trace
        max_tolerance = 20.0  # 20mm max tolerance (for components with pin offset)
        start_point = None
        min_dist = float('inf')

        # Find the nearest wire endpoint to component
        for point in network:
            dist = ((point[0] - cx)**2 + (point[1] - cy)**2)**0.5
            if dist < min_dist:
                min_dist = dist
                start_point = point

        # Only proceed if the nearest point is within tolerance
        if min_dist > max_tolerance:
            return {
                "component": reference,
                "position": comp.position,
                "connected_labels": [],
                "trace_path": [],
                "error": f"No wire found within {max_tolerance}mm (nearest: {min_dist:.2f}mm)",
            }

        # BFS trace through network
        visited = set()
        queue = [start_point]
        trace_path = []
        connected_labels = []

        while queue and len(visited) < max_depth:
            point = queue.pop(0)
            if point in visited:
                continue
            visited.add(point)

            trace_path.append(point)

            # Check if this point is near a label
            label_tolerance = 5.0  # 5mm tolerance for label matching
            for label in all_labels:
                lx, ly = label["position"]
                dist = ((point[0] - lx)**2 + (point[1] - ly)**2)**0.5
                if dist < label_tolerance and not any(lbl["name"] == label["name"] for lbl in connected_labels):
                    connected_labels.append({
                        "name": label["name"],
                        "position": label["position"],
                        "distance": dist,
                    })

            # Add neighbors to queue
            if point in network:
                for neighbor in network[point]:
                    if neighbor not in visited:
                        queue.append(neighbor)

        # Find nearby power symbols
        power_tolerance = 15.0  # 15mm tolerance for power symbols
        for power in self._power_symbols():
            power_name = power["name"]
            px, py = power["position"]
            dist = ((px - cx)**2 + (py - cy)**2)**0.5
            if dist < power_tolerance:
                connected_labels.append({
                    "name": f"POWER:{power_name}",
                    "position": (px, py),
                    "distance": dist,
                })

        return {
            "component": reference,
            "position": comp.position,
            "connected_labels": connected_labels,
            "trace_path": trace_path,
        }
