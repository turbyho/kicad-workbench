"""The pin conflict tool of KiCad Workbench: the pin items of the ERC of kicad-cli."""

from ..parsers.kicad_versions import version_numbers
from ..server import mcp
from ..utils.file_versions import with_schematic_versions
from ..utils.kicad_cli import get_kicad_version
from .validation import PIN_ERC_TYPES, erc_violations_answer


@mcp.tool()
@with_schematic_versions("schematic_path")
async def detect_pin_conflicts(
    schematic_path: str,
    severity: str = "",
) -> str:
    """Give the pin conflicts of a schematic: the items of the ERC of kicad-cli about the
    connections of the symbol pins.

    The tool runs the ERC of kicad-cli on the full project (as run_erc, with the
    severities, the pin map and the exclusions of the project) and gives only these
    items of the ERC of KiCad 10:

    - pin_to_pin: two connected pins whose electrical types conflict in the pin map of
      the project (an error or a warning). In the default pin map of KiCad, two open
      collector pins, two open emitter pins or two tri-state pins are OK: a wired OR or
      a tri-state bus is not a conflict.
    - pin_not_driven, power_pin_not_driven: an input or a power input pin with no
      output or power output pin on its net.
    - pin_not_connected, no_connect_connected: a pin with no connection and no "no
      connection" flag, or a pin with the flag that is connected.
    - isolated_pin_label: a label that connects to only one pin.
    - duplicate_pins, different_unit_net: pins of one number, or a shared pin of the
      units, on different nets.
    - ground_pin_not_ground: a ground pin that is not on a ground net.
    - missing_input_pin, missing_bidi_pin, missing_power_pin: the pins of a unit that
      is not placed.

    The other items of the ERC (for example a wire end off the grid) are in run_erc.
    The tool uses the last ERC report until a schematic file changes: then it runs the
    ERC again. An excluded
    violation shows with "(excluded)" and does not count. "✅" comes only when the
    schematic is fully annotated: KiCad connects the pins by their references. With
    KiCad 8 or 9 installed, the answer has a warning: the list of items is the list of
    KiCad 10.

    Args:
        schematic_path: A .kicad_sch file of the project.
        severity: Optional severity: "error" or "warning". Empty: all violations.

    Returns:
        The pin violations with their components, pins and positions.
    """
    answer = await erc_violations_answer(schematic_path, severity, PIN_ERC_TYPES)
    version = await get_kicad_version()
    numbers = version_numbers(version) if version else None
    if numbers is not None and numbers[0] < 10:
        answer += (
            f"\n\n⚠️ The item types are the types of the ERC of KiCad 10. The server did not "
            f"compare them with KiCad {version}: an item of an other type is not in the answer."
        )
    return answer
