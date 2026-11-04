"""The settings of the project file that the schematic tools use.

KiCad 8.0.9, 9.0.9 and 10.0.6 read them from the .kicad_pro file:

- ``schematic.connection_grid_size``: the connection grid in mils
  (SCHEMATIC_SETTINGS, PARAM_SCALED). KiCad rounds it to IU (KiROUND), and
  uses the default of 50 mils when the value is not a number, or is less
  than 25 or more than 10000 mils.
- ``erc.rule_severities.endpoint_off_grid``: the severity of the ERC check
  that finds a pin or a wire end off the connection grid (ERC_SETTINGS).
  "warning" and "ignore" are these severities, all other texts are "error"
  (SeverityFromString). The default is "warning", also for a value that is
  not a text.
- ``net_settings.classes[].wire_width``: the wire width of each net class in
  mils (NET_SETTINGS, ``getInSchUnits``). The default net class has 6 mils.
  The junction rule of KiCad 10 uses the pen width of a bus entry, which can
  come from its net class (sch_junctions).

KiCad reads the file with comments and a byte order mark (read_kicad_json).
When the project file does not exist, or is not JSON, KiCad uses the default
values (JSON_SETTINGS::LoadFromFile).
"""

from dataclasses import dataclass
from pathlib import Path

from ..parsers.sch_geometry import IU_PER_MIL, kiround
from .kicad_text import read_kicad_json

DEFAULT_CONNECTION_GRID_MILS = 50
_GRID_LIMITS_MILS = (25, 10000)
DEFAULT_OFF_GRID_SEVERITY = "warning"
DEFAULT_WIRE_WIDTH_MILS = 6


@dataclass(frozen=True)
class SchematicSettings:
    connection_grid: int  # In IU.
    off_grid_severity: str  # "error", "warning" or "ignore".
    problem: str | None = None  # Why KiCad uses the default values.
    # The lowest and the highest wire width of the net classes, in IU.
    wire_widths: tuple[int, int] = (
        DEFAULT_WIRE_WIDTH_MILS * IU_PER_MIL,
        DEFAULT_WIRE_WIDTH_MILS * IU_PER_MIL,
    )


def _value(data: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def schematic_settings(project_file: Path) -> SchematicSettings:
    """Return the connection grid and the severity of the off-grid check of a project."""
    default_grid = DEFAULT_CONNECTION_GRID_MILS * IU_PER_MIL
    if not project_file.is_file():
        return SchematicSettings(default_grid, DEFAULT_OFF_GRID_SEVERITY)
    try:
        data = read_kicad_json(project_file)
    except (OSError, ValueError) as e:
        return SchematicSettings(
            default_grid,
            DEFAULT_OFF_GRID_SEVERITY,
            f"The project file {project_file} cannot be read ({e}), thus KiCad uses "
            "the default connection grid and ERC severities.",
        )
    grid = default_grid
    mils = _value(data, "schematic", "connection_grid_size")
    if isinstance(mils, int | float) and not isinstance(mils, bool):
        # KiCad rounds first, then compares with the limits (PARAM_SCALED::Load).
        # A value out of the range of a double is out of the limits.
        try:
            value = kiround(float(mils) * IU_PER_MIL)
        except (OverflowError, ValueError):
            value = None
        low, high = (limit * IU_PER_MIL for limit in _GRID_LIMITS_MILS)
        if value is not None and low <= value <= high:
            grid = value
    severity = _value(data, "erc", "rule_severities", "endpoint_off_grid")
    if not isinstance(severity, str):
        # KiCad cannot read a value that is not a text, and keeps the default
        # (JSON_SETTINGS::Load skips the parameter).
        severity = DEFAULT_OFF_GRID_SEVERITY
    elif severity not in ("warning", "ignore"):
        severity = "error"
    return SchematicSettings(grid, severity, None, _wire_widths(data))


def _wire_widths(data: object) -> tuple[int, int]:
    """Return the lowest and the highest wire width (IU) of the net classes.

    A net class without a wire width gets the width of the Default class (6
    mils when the file does not give it), thus these widths give the range.
    """
    default = DEFAULT_WIRE_WIDTH_MILS * IU_PER_MIL
    widths = {}
    classes = _value(data, "net_settings", "classes")
    for entry in classes if isinstance(classes, list) else []:
        mils = entry.get("wire_width") if isinstance(entry, dict) else None
        if isinstance(mils, int | float) and not isinstance(mils, bool):
            try:
                widths[entry.get("name")] = kiround(float(mils) * IU_PER_MIL)
            except (OverflowError, ValueError):
                continue
    # A class without a wire width gets the width of the Default class.
    values = set(widths.values()) | {widths.get("Default", default)}
    return min(values), max(values)
