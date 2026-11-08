"""Pin analysis and connectivity tools for KiCad Workbench.

This module provides advanced pin analysis capabilities including:
- Pin function inference (GPIO, I2C, SPI, UART, etc.)
- Pin conflict detection
- Pin multiplexing configuration extraction
- MCU-specific pin mapping support
"""

import collections
import re
from pathlib import Path

from ..parsers.kicad_fields import with_field_rules_note
from ..server import mcp
from ..utils.file_versions import with_schematic_versions
from ..utils.tool_errors import error_answer
from .netlist import current_netlist
from .schematic import BaseNames, components_of_project

# MCU family component patterns
MCU_PATTERNS = {
    "stm32": r"STM32[FHL]\d[A-Za-z0-9]*",
    "esp32": r"ESP32(?:-[A-Za-z0-9]+)?",
    "nrf52": r"nRF5[2-9][\d A-Za-z0-9]*",  # nRF52/nRF53/nRF54 (e.g. nRF54LM20B)
    "atmega": r"ATmega\d+[A-Za-z]*",
    "samd": r"ATSAMD\d+[A-Za-z]*",
    "rp2040": r"RP2040",
}


# Pin function inference patterns based on net names.
# IMPORTANT: use alternation groups (SDA|SCL), NOT character classes [SDA|SCL] —
# a character class matches any single char in the set {S,D,A,|,C,L} and would
# match almost every net name, producing nonsense matches.
NET_FUNCTION_PATTERNS = {
    "I2C": [
        r"I2C[_\d]*(?:SDA|SCL)",
        r"SDA\d*",
        r"SCL\d*",
        r"TWI[_\d]*(?:SDA|SCL)",
    ],
    "SPI": [
        r"SPI[_\d]*(?:MISO|MOSI|SCK|CS)",
        r"MISO\d*",
        r"MOSI\d*",
        r"SCK\d*",
        r"CS\d*",
        r"NSS\d*",
    ],
    "UART": [
        r"UART[_\d]*(?:TX|RX|CTS|RTS)",
        r"USART[_\d]*(?:TX|RX|CTS|RTS)",
        r"TX\d*",
        r"RX\d*",
        r"Serial\d*",
    ],
    "GPIO": [
        r"GPIO[_\d]+",
        r"IO[_\d]+",
        r"PA\d+",
        r"PB\d+",
        r"PC\d+",
        r"P\d+",
    ],
    "ADC": [
        r"ADC[_\d]+",
        r"AIN\d+",
        r"AN\d+",
    ],
    "PWM": [
        r"PWM[_\d]+",
        r"TIM[_\d]*CH*\d+",
    ],
    "USB": [
        r"USB[_\d]*(?:DM|DP|D-|D\+)",
        r"UDM",
        r"UDP",
        r"D-",
        r"D\+",
    ],
    "INTERRUPT": [
        r"INT\d*",
        r"IRQ\d*",
        r".*_INT",
    ],
}


def _infer_pin_function_from_net(net_name: str) -> str | None:
    """Infer pin function from net name pattern.

    Args:
        net_name: Net name to analyze

    Returns:
        Inferred function or None if unknown
    """
    if not net_name:
        return None

    net_name_upper = net_name.upper()

    # Check each function pattern
    for function, patterns in NET_FUNCTION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, net_name_upper, re.IGNORECASE):
                return function

    return None


def _identify_mcu_family(component_value: str) -> str | None:
    """Identify MCU family from component value.

    Args:
        component_value: Component value/part number

    Returns:
        MCU family identifier or None if not an MCU
    """
    if not component_value:
        return None

    component_value_upper = component_value.upper()

    for family, pattern in MCU_PATTERNS.items():
        if re.search(pattern, component_value_upper, re.IGNORECASE):
            return family

    return None


# The port and the pin of an STM32 pin name "PXn" (for example PA0, PB12).
STM32_PIN = re.compile(r"P([A-Z])(\d+)", re.IGNORECASE)


def _get_mcu_pin_mapping(mcu_family: str, pin_name: str) -> dict:
    """Get MCU-specific pin mapping information.

    Args:
        mcu_family: MCU family identifier
        pin_name: Pin name to map

    Returns:
        Dictionary with pin mapping information
    """
    # Basic pin mappings for common MCU families
    # This would be expanded with comprehensive pin databases

    pin_mapping = {
        "function": None,
        "alternate_functions": [],
        "max_current": 0.0,
        "is_5v_tolerant": False,
    }

    if mcu_family == "stm32":
        # STM32 pin naming convention: PXn (e.g., PA0, PB12)
        if STM32_PIN.match(pin_name):
            pin_mapping["function"] = "GPIO"
            pin_mapping["alternate_functions"] = [
                "GPIO", "ADC", "TIM", "USART", "SPI", "I2C", "CAN"
            ]
            pin_mapping["max_current"] = 25.0  # mA
            pin_mapping["is_5v_tolerant"] = False  # Most STM32 pins are not 5V tolerant

    elif mcu_family == "esp32":
        # ESP32 pin naming: IOx, GPIOx
        if re.match(r"(?:IO|GPIO)[\d]+", pin_name, re.IGNORECASE):
            pin_mapping["function"] = "GPIO"
            pin_mapping["alternate_functions"] = [
                "GPIO", "ADC", "DAC", "I2C", "SPI", "UART", "TOUCH"
            ]
            pin_mapping["max_current"] = 40.0  # mA
            pin_mapping["is_5v_tolerant"] = False

    elif mcu_family == "nrf52":
        # nRF52 pin naming: P0.xx, P1.xx
        if re.match(r"P[01]\.[\d]+", pin_name, re.IGNORECASE):
            pin_mapping["function"] = "GPIO"
            pin_mapping["alternate_functions"] = [
                "GPIO", "ADC", "SPI", "I2C", "UART", "PWM", "QSPI"
            ]
            pin_mapping["max_current"] = 5.0  # mA (typical for nRF52)
            pin_mapping["is_5v_tolerant"] = False

    return pin_mapping


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def analyze_pin_functions(
    schematic_path: str,
    reference: str = "",
) -> str:
    """Give the function of the pins of the components of a schematic (heuristic).

    The tool finds the function of a pin (I2C, SPI, UART, GPIO, ADC, PWM, USB,
    INTERRUPT) from the name of its net, and the family of a microcontroller from
    its value. See docs/firmware.md of the repository for the limits.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.
        reference: Optional reference of one component, for example "U1".
            Empty: all components.

    Returns:
        The pins of each component with their nets and functions.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # The netlist, then the components of the project with the KiCad of the netlist
        # (components_of_project: the pins of each unit).
        netlist_parser = await current_netlist(sch_path)

        # Get components to analyze
        # Each reference of the project, with the references and units of KiCad.
        all_components = await components_of_project(sch_path, netlist_parser.kicad_version)
        if reference:
            # Analyze specific component
            components = [comp for comp in all_components if comp.reference == reference]
            if not components:
                return f"❌ **Component not found:** {reference}"
        else:
            # Analyze all components
            components = all_components

        # Analyze pin functions. The pin -> net map, the pin names and the pin types
        # come from the netlist (as KiCad computes them); the schematic gives the base
        # names of the library pins (SchematicComponent.pins).
        sch_components = {c.reference: c for c in all_components}
        nl_components = netlist_parser.get_components()
        pin_analysis = []

        for component in components:
            comp_ref = component.reference
            comp_value = component.value
            mcu_family = _identify_mcu_family(comp_value)

            nl_comp = nl_components.get(comp_ref)
            if not nl_comp or not nl_comp.nodes:
                continue  # not in netlist / nothing connected

            # The name (with the alternate) and the type of each pin are the ones that
            # KiCad gives the nodes of the netlist. The nodes of one pin (number, name and
            # type) are one row with all their nets: a pin number can be on more than one
            # net. The base name (the name of the library pin, for the MCU pin names)
            # comes from the schematic.
            base_names = BaseNames(sch_components.get(comp_ref))
            pins: dict[tuple[str, str, str], list] = {}
            for node in nl_comp.nodes:
                pins.setdefault((node.number, node.name, node.type), []).append(node)
            for (number, name, kind), nodes in pins.items():
                nets = [node.net for node in nodes if node.net]
                inferred_functions = list(
                    dict.fromkeys(f for f in map(_infer_pin_function_from_net, nets) if f)
                )

                # The MCU pin names (PA7, GPIO4) are the names of the library pins.
                pin_base = base_names.of(nodes[0])
                mcu_mapping = _get_mcu_pin_mapping(mcu_family, pin_base) if mcu_family else None

                pin_analysis.append(
                    {
                        "component": comp_ref,
                        "component_value": comp_value,
                        "mcu_family": mcu_family,
                        "pin_number": number,
                        "pin_name": name or number,
                        "pin_type": kind,
                        "net_names": nets,
                        "inferred_functions": inferred_functions,
                        "mcu_mapping": mcu_mapping,
                    }
                )

        if not pin_analysis:
            return f"""⚠️ **No Pin Analysis Available**

**Schematic:** {schematic_path}
{'**Component:** ' + reference if reference else ''}

Unable to extract pin information. This could be due to:
- No components found in schematic
- Missing symbol definitions
- Netlist not available

**Next Steps:**
1. Ensure schematic has components with pins
2. Generate netlist first: `generate_netlist()`
3. Verify schematic file is valid KiCad 9.0+ format"""

        # Format results
        result = f"""# Pin Function Analysis

**Schematic:** {schematic_path}
{'**Component:** ' + reference if reference else ''}
**Total Pins Analyzed:** {len(pin_analysis)}

## Pin Details

| Component | Pin | Type | Nets | Inferred Functions | MCU Family |
|-----------|-----|------|------|-------------------|------------|
"""

        for pin in pin_analysis[:50]:  # Limit to first 50 pins
            comp = pin["component"]
            pin_name = pin["pin_name"]
            pin_type = pin["pin_type"]
            nets = ", ".join(pin["net_names"][:3]) if pin["net_names"] else "N/A"
            if len(pin["net_names"]) > 3:
                nets += f" (+{len(pin['net_names']) - 3} more)"
            functions = ", ".join(pin["inferred_functions"]) if pin["inferred_functions"] else "Unknown"
            mcu_family = pin["mcu_family"] if pin["mcu_family"] else "N/A"

            result += f"| {comp} | {pin_name} ({pin['pin_number']}) | {pin_type} | {nets} | {functions} | {mcu_family} |\n"

        if len(pin_analysis) > 50:
            result += f"\n*... and {len(pin_analysis) - 50} more pins*\n"

        # Add MCU-specific details if available
        mcu_pins = [p for p in pin_analysis if p["mcu_family"] and p["mcu_mapping"]]
        if mcu_pins:
            result += "\n## MCU Pin Details\n\n"

            for pin in mcu_pins[:10]:  # Limit to first 10 MCU pins
                mapping = pin["mcu_mapping"]
                result += f"**{pin['component']} - {pin['pin_name']} ({pin['pin_number']})**\n"
                result += f"- Primary Function: {mapping['function']}\n"
                result += f"- Alternate Functions: {', '.join(mapping['alternate_functions'])}\n"
                result += f"- Max Current: {mapping['max_current']} mA\n"
                result += f"- 5V Tolerant: {'Yes' if mapping['is_5v_tolerant'] else 'No'}\n\n"

        if note := netlist_parser.pins_on_more_nets_note(c.reference for c in components):
            result += f"\n{note}\n"
        if note := netlist_parser.netlist_note():
            result += f"\n{note}\n"
        return result

    except Exception as e:
        return error_answer(e, f"Pin analysis of {schematic_path} failed")


def _extract_pin_info_from_symbol_details(details_text: str) -> list[dict]:
    """Extract pin information from get_symbol_details output.

    Args:
        details_text: Formatted output from get_symbol_details

    Returns:
        List of pin information dictionaries
    """
    pins = []

    # Parse the get_symbol_details markdown table, e.g.:
    #   | Pin | Name | Type |
    #   | --- | ---- | ---- |
    #   | 1 | VDD | power_in |
    lines = details_text.split("\n")

    for line in lines:
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c != ""]  # drop empty edge cells
        if len(cells) < 3:
            continue
        first = cells[0].lower()
        # skip header / separator rows
        if first in ("pin", "number", "#", "no.", "ref"):
            continue
        if set(cells[0]) <= set("-:="):
            continue
        pins.append({"number": cells[0], "name": cells[1], "type": cells[2]})

    return pins


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def detect_pin_conflicts(
    schematic_path: str,
) -> str:
    """Find the nets with pin conflicts in a schematic.

    The pin types come from the netlist of kicad-cli. A net with more than one
    output or driver pin is an error. A net with one pin is a warning. A pin with no
    connection is information. Run run_erc for the full electrical check of KiCad.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.

    Returns:
        The errors, the warnings and the information, with a count of each.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"
        return _conflict_report(schematic_path, await _pin_conflicts(sch_path))
    except Exception as e:
        return error_answer(e, f"Conflict detection of {schematic_path} failed")


async def _pin_conflicts(sch_path: Path) -> list[dict]:
    """Return the pin conflicts of the netlist of a schematic.

    Each item has the type, the severity ("error": more than one driver on a
    net, "warning": a net with one pin, "info": an unconnected pin), the net
    and the description.

    Raises:
        AmbiguousRootError: More than one root schematic holds the sheet.
        NetlistError: kicad-cli did not write the netlist.
        InputError: The netlist is not valid XML.
    """
    # The netlist has the electrical type of each node as KiCad gives it (with the
    # alternate of the pin).
    netlist_parser = await current_netlist(sch_path)

    output_types = {"output", "power_out", "open_collector", "open_emitter", "tri_state"}

    # Detect conflicts
    conflicts = []

    # Check for multiple output/driver pins on the same net.
    # (Multiple power_in pins on a rail like +3V3 are normal and are NOT
    # reported — only multiple drivers are a conflict.) The type of each node is the
    # type of its pin on this net (a pin number can be on more than one net).
    all_nets = netlist_parser.get_nets()

    for net_name, net in all_nets.items():
        drivers = [node for node in net.nodes if node.type.lower() in output_types]
        # Two driver nodes of one pin number (two library pins with one number, or
        # "[1-2]" and "2" in KiCad 10) get the pin names.
        counts = collections.Counter((node.reference, node.number) for node in drivers)
        output_pins = [
            f"{node.reference}:{node.number}"
            + (f" ({node.name})" if counts[node.reference, node.number] > 1 and node.name else "")
            for node in drivers
        ]

        if len(output_pins) > 1:
            conflicts.append(
                {
                    "type": "multiple_outputs",
                    "severity": "error",
                    "net": net_name,
                    "description": f"Multiple output/driver pins on same net: {', '.join(output_pins)}",
                }
            )

    # Check for single-pin nets (potential unconnected)
    for net_name, net in all_nets.items():
        if len(net.pins) == 1 and not net_name.startswith("unconnected-"):
            ref, pin_num = net.pins[0]
            conflicts.append(
                {
                    "type": "single_pin_net",
                    "severity": "warning",
                    "net": net_name,
                    "description": f"Net '{net_name}' has only one pin: {ref}:{pin_num}",
                }
            )

    # Check for unconnected input pins
    all_components = netlist_parser.get_components()

    for ref, comp in all_components.items():
        for node in comp.nodes:
            if node.net.startswith("unconnected-"):
                conflicts.append(
                    {
                        "type": "unconnected_pin",
                        "severity": "info",
                        "net": node.net,
                        "description": f"{ref} pin {node.number} is unconnected ({node.net})",
                    }
                )
    return conflicts


def _conflict_report(schematic_path: str, conflicts: list[dict]) -> str:
    """Return the report of detect_pin_conflicts for the conflicts of _pin_conflicts."""
    # The title gives the worst severity: an error is a conflict, a warning can be
    # one, an unconnected pin is only information.
    errors = [c for c in conflicts if c["severity"] == "error"]
    warnings = [c for c in conflicts if c["severity"] == "warning"]
    infos = [c for c in conflicts if c["severity"] == "info"]
    icons = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}

    def table(items: list[dict]) -> str:
        text = "| Severity | Type | Location | Description |\n"
        text += "|----------|------|----------|-------------|\n"
        for conflict in items[:50]:  # Limit to first 50
            location = conflict.get("net", conflict.get("component", "Unknown"))
            text += (
                f"| {icons[conflict['severity']]} {conflict['severity']} "
                f"| {conflict['type']} | {location} | {conflict['description']} |\n"
            )
        if len(items) > 50:
            text += f"\n*... and {len(items) - 50} more*\n"
        return text

    if not errors and not warnings:
        unconnected = (
            f"ℹ️ {len(infos)} unconnected pin(s), listed below"
            if infos
            else "✅ No unconnected pins"
        )
        result = f"""✅ **No Pin Conflicts Detected**

**Schematic:** {schematic_path}

The schematic has been analyzed and no pin conflicts were found.

**Checked:**
- ✅ No multiple output/driver pins on the same net
- ✅ No single-pin (potentially unconnected) nets
- {unconnected}
"""
        if infos:
            result += f"\n## Unconnected Pins\n\n{table(infos)}"
        return result

    # Format conflicts
    title = "❌ **Pin Conflicts Detected**" if errors else "⚠️ **Possible Pin Conflicts**"
    result = f"""{title}

**Schematic:** {schematic_path}
- Errors: {len(errors)}
- Warnings: {len(warnings)}
- Unconnected pins (info): {len(infos)}

## Conflicts

{table(errors + warnings + infos)}"""

    result += "\n## Recommendations\n\n"
    result += "1. **Fix all errors** before proceeding to PCB layout\n"
    result += "2. Review warnings - some may be acceptable design choices\n"
    result += "3. Verify pin connections in KiCad Eeschema\n"
    result += "4. Re-run analysis after fixes\n"

    return result


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def extract_pinmux_config(
    schematic_path: str,
    component_type: str = "",
) -> str:
    """Give the peripheral of each pin of the microcontrollers of a schematic (heuristic).

    The tool finds the peripheral of a pin (I2C, SPI, UART, GPIO, ADC, PWM, USB,
    INTERRUPT) from the name of its net. See docs/firmware.md of the repository for
    the supported microcontrollers and the limits.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.
        component_type: Optional family of the microcontroller, for example
            "stm32", "esp32" or "nrf52". Empty: all families.

    Returns:
        The pins of each microcontroller with their peripherals.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # The netlist, then the components of the project with the KiCad of the netlist.
        netlist_parser = await current_netlist(sch_path)

        # Find MCU components
        components = await components_of_project(sch_path, netlist_parser.kicad_version)
        mcu_components = []

        for component in components:
            comp_ref = component.reference
            comp_value = component.value

            # Check if this is an MCU
            mcu_family = _identify_mcu_family(comp_value)

            if mcu_family:
                # Filter by component_type if specified
                if component_type and component_type.lower() != mcu_family.lower():
                    continue

                mcu_components.append({
                    "reference": comp_ref,
                    "value": comp_value,
                    "mcu_family": mcu_family,
                    "library": component.library_id,
                })

        if not mcu_components:
            return f"""⚠️ **No MCU Components Found**

**Schematic:** {schematic_path}
{'**Component Type Filter:** ' + component_type if component_type else ''}

No MCU components were found in the schematic.

**Supported MCU families:**
- STM32 (STM32F, STM32H, STM32L series)
- ESP32 (ESP32, ESP32-S2, ESP32-S3, etc.)
- nRF52 (nRF52832, nRF52840, etc.)
- ATmega (ATmega328P, ATmega2560, etc.)
- SAMD (ATSAMD21, ATSAMD51, etc.)
- RP2040

**Next Steps:**
1. Ensure schematic contains MCU components
2. Check component values match supported patterns
3. Try without component_type filter"""

        # pin number -> name from schematic lib_symbols
        sch_components = {c.reference: c for c in components}

        # Extract pinmux configuration for each MCU
        pinmux_configs = []

        for mcu in mcu_components:
            mcu_ref = mcu["reference"]
            mcu_family = mcu["mcu_family"]

            config = {
                "component": mcu_ref,
                "mcu_family": mcu_family,
                "part_number": mcu["value"],
                "pins": [],
            }

            # The base name of each pin (the name of the library pin, for the MCU pin
            # names) comes from the schematic; the name with the alternate comes from the
            # netlist. One row for each node: a pin number can be on more than one net.
            base_names = BaseNames(sch_components.get(mcu_ref))
            comp = netlist_parser.get_components().get(mcu_ref)
            if comp:
                for node in comp.nodes:
                    pin_base = base_names.of(node)
                    mcu_mapping = _get_mcu_pin_mapping(mcu_family, pin_base)
                    pin_config = {
                        "pin_number": node.number,
                        "pin_name": node.name or node.number,
                        "base_name": pin_base,
                        "net": node.net,
                        "peripheral": _infer_pin_function_from_net(node.net),
                        "alternate_functions": (mcu_mapping or {}).get("alternate_functions", []),
                    }
                    config["pins"].append(pin_config)

            pinmux_configs.append(config)

        # Format results
        result = f"""# Pin Multiplexing Configuration

**Schematic:** {schematic_path}
{'**Component Type Filter:** ' + component_type if component_type else ''}
**Total MCUs Analyzed:** {len(pinmux_configs)}

"""

        for mcu_config in pinmux_configs:
            result += f"""## {mcu_config['component']}: {mcu_config['part_number']}

**MCU Family:** {mcu_config['mcu_family']}

| Pin | Net | Peripheral | Alternate Functions |
|-----|-----|------------|---------------------|
"""

            for pin in mcu_config["pins"][:30]:  # Limit to first 30 pins per MCU
                pin_name = pin["pin_name"]
                net = pin["net"]
                peripheral = pin["peripheral"] if pin["peripheral"] else "GPIO"
                alt_funcs = ", ".join(pin["alternate_functions"]) if pin["alternate_functions"] else "N/A"

                result += f"| {pin_name} ({pin['pin_number']}) | {net} | {peripheral} | {alt_funcs} |\n"

            if len(mcu_config["pins"]) > 30:
                result += f"\n*... and {len(mcu_config['pins']) - 30} more pins*\n"

            result += "\n"

        # Add code generation suggestions
        result += "## Code Generation Suggestions\n\n"

        for mcu_config in pinmux_configs:
            result += f"""### {mcu_config['component']} ({mcu_config['mcu_family']})

```c
// Pin configuration for {mcu_config['part_number']}
"""

            for pin in mcu_config["pins"][:10]:  # Show first 10 pins as example
                if pin["peripheral"]:
                    result += f"// {pin['pin_name']}: {pin['peripheral']} ({pin['net']})\n"
                    stm32_pin = STM32_PIN.match(pin["base_name"])
                    if mcu_config["mcu_family"] == "stm32" and stm32_pin:
                        port, number = stm32_pin.group(1).upper(), stm32_pin.group(2)
                        result += f"// GPIO_{port}_{number} // {pin['peripheral']}_AF\n"
                    elif mcu_config["mcu_family"] == "esp32":
                        result += f"// GPIO{pin['pin_number']} // {pin['peripheral']}\n"

            result += "```\n\n"

        if note := netlist_parser.pins_on_more_nets_note(m["reference"] for m in mcu_components):
            result += f"\n{note}\n"
        if note := netlist_parser.netlist_note():
            result += f"\n{note}\n"
        return result

    except Exception as e:
        return error_answer(e, f"Pinmux extraction of {schematic_path} failed")
