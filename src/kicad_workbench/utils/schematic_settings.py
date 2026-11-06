"""The settings of the project file that the schematic tools use.

KiCad 8.0.9, 9.0.9 and 10.0.6 read them from the .kicad_pro file:

- ``schematic.connection_grid_size``: the connection grid in mils
  (SCHEMATIC_SETTINGS, PARAM_SCALED). KiCad rounds it to IU (KiROUND), and
  uses the default of 50 mils when the value is not a number, or is less
  than 25 or more than 10000 mils.
- ``erc.rule_severities.endpoint_off_grid``: the severity of the ERC check
  that finds a pin or a wire end off the connection grid (ERC_SETTINGS).
  "warning" and "ignore" are these severities, all other texts are "error"
  (SeverityFromString). The default is "warning". The installed KiCad reads
  the keys of "rule_severities" in its order, and a value that is not a text
  stops the read (rule_severities): the keys after it keep their defaults.
- ``net_settings.classes[].wire_width``: the wire width of each net class in
  mils (NET_SETTINGS, ``getInSchUnits``). The default net class has 6 mils.
  The junction rule of KiCad 10 uses the pen width of a bus entry, which can
  come from its net class (sch_junctions).

KiCad reads the file with comments and a byte order mark (read_kicad_json).
When the project file does not exist, or is not JSON, KiCad uses the default
values (JSON_SETTINGS::LoadFromFile).
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InputError
from ..parsers.rule_severity_keys import SeverityKeys, all_pre_keys, severity_keys
from ..parsers.sch_geometry import IU_PER_MIL, kiround
from .kicad_text import read_kicad_json

DEFAULT_CONNECTION_GRID_MILS = 50
_GRID_LIMITS_MILS = (25, 10000)
DEFAULT_OFF_GRID_SEVERITY = "warning"
DEFAULT_WIRE_WIDTH_MILS = 6


@dataclass(frozen=True)
class Severity:
    """The severity of an ERC check: "error", "warning" or "ignore". None when it is
    not known, with the reason and the severities that KiCad can give."""

    value: str | None
    reason: str | None = None
    possible: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchematicSettings:
    """The settings of the projects of a schematic (usually one project)."""

    connection_grid: int  # In IU.
    problem: str | None = None  # Why KiCad uses the default values.
    # The lowest and the highest wire width of the net classes, in IU.
    wire_widths: tuple[int, int] = (
        DEFAULT_WIRE_WIDTH_MILS * IU_PER_MIL,
        DEFAULT_WIRE_WIDTH_MILS * IU_PER_MIL,
    )
    # The path of each project file and its "erc.rule_severities" as the file has it
    # (None: no value). off_grid_severity reads them for a KiCad version. The JSON
    # values cannot be hashed.
    erc_severities: tuple[tuple[str, object], ...] = field(default=(("", None),), hash=False)


def _value(data: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def schematic_settings(project_file: Path) -> SchematicSettings:
    """Return the connection grid, the wire widths and the ERC severities of a project."""
    default_grid = DEFAULT_CONNECTION_GRID_MILS * IU_PER_MIL
    defaults = ((str(project_file), None),)
    if not project_file.is_file():
        return SchematicSettings(default_grid, erc_severities=defaults)
    try:
        data = read_kicad_json(project_file)
    except (OSError, InputError) as e:
        return SchematicSettings(
            default_grid,
            f"The project file {project_file} cannot be read ({e}), thus KiCad uses "
            "the default connection grid and ERC severities.",
            erc_severities=defaults,
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
    return SchematicSettings(
        connection_grid=grid,
        problem=None,
        wire_widths=_wire_widths(data),
        erc_severities=((str(project_file), _value(data, "erc", "rule_severities")),),
    )


def off_grid_severity(
    settings: SchematicSettings, version: str | None, kicad: str | None = None
) -> Severity:
    """Return the severity of endpoint_off_grid of the projects of a schematic in a
    KiCad version.

    Args:
        version: The version of the KiCad that runs ERC, or None when it is not
            known. A severity that each order gives is known also with no version.
        kicad: The text for the KiCad of the reason, when it is not "KiCad
            <version>" (unknown_severity_reason).
    """
    defaults = {"endpoint_off_grid": DEFAULT_OFF_GRID_SEVERITY}
    found = [
        (name, _outcomes(severities, defaults, "erc", version)["endpoint_off_grid"])
        for name, severities in settings.erc_severities
    ]
    reason = unknown_severity_reason(version, kicad)
    if len({frozenset(possible) for _name, possible in found}) == 1:
        possible = found[0][1]
        if len(possible) == 1:
            return Severity(next(iter(possible)))
        return Severity(None, reason, tuple(sorted(possible)))
    # The projects give different results.
    parts = [
        f"{name}: {next(iter(possible)) if len(possible) == 1 else 'not known'}"
        for name, possible in found
    ]
    text = f"the projects have different severities ({'; '.join(parts)})"
    if any(len(possible) > 1 for _name, possible in found):
        text += f"; {reason}"
    return Severity(None, text, tuple(sorted(set().union(*(p for _n, p in found)))))


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


# SCHEMATIC_SETTINGS: PARAM<int>( "subpart_id_separator", 0, 0, 126 ) and
# PARAM<int>( "subpart_first_id", 'A', '1', 'z' ) (KiCad 8.0.9, 9.0.9, 10.0.6).
_SUBPART_SEPARATOR = (0, 0, 126)
_SUBPART_FIRST_ID = (ord("A"), ord("1"), ord("z"))


def _int_param(data: object, key: str, limits: tuple[int, int, int]) -> int:
    """Return an int parameter of the "schematic" settings as PARAM<int>::Load reads it.

    nlohmann gives an int for a number or a boolean (a float is cut to an
    integer). Other values keep the default. A value out of the limits gives
    the default. C++ does not define the cast of a float out of the int range:
    the tool then gives the default.
    """
    default, low, high = limits
    value = _value(data, "schematic", key)
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value == value and abs(value) < 2**31:
        number = int(value)
    else:
        return default
    return number if low <= number <= high else default


@dataclass(frozen=True)
class SubpartSettings:
    """The unit letters of the project (SCHEMATIC_SETTINGS::SubReference)."""

    separator: int  # The character before the unit letters; 0: none.
    first_id: int  # The first letter ("A" or "a"), or a digit: the units are numbers.
    problem: str | None = None  # Why KiCad uses the default values.


def subpart_settings(project_file: Path) -> SubpartSettings:
    """Return the unit letter settings of a project. A project file that does not
    exist or cannot be read gives the default values (no separator, "A")."""
    problem = None
    try:
        data = read_kicad_json(project_file) if project_file.is_file() else None
    except (OSError, InputError) as e:
        data = None
        problem = (
            f"The project file {project_file} cannot be read ({e}), thus KiCad uses "
            "the default unit letters."
        )
    return SubpartSettings(
        _int_param(data, "subpart_id_separator", _SUBPART_SEPARATOR),
        _int_param(data, "subpart_first_id", _SUBPART_FIRST_ID),
        problem,
    )


def _project_data(project_file: Path) -> object:
    """The JSON of a project file, or None when KiCad uses the defaults (no file, or
    a file that is not JSON)."""
    if not project_file.is_file():
        return None
    try:
        return read_kicad_json(project_file)
    except (OSError, InputError):
        return None


def _severity(value: str) -> str:
    """SeverityFromString: "warning" and "ignore", each other text is "error"."""
    return value if value in ("warning", "ignore") else "error"


def unknown_severity_reason(version: str | None, kicad: str | None = None) -> str:
    """Why rule_severities gives None for a check.

    Args:
        version: The KiCad version that reads the project file (None: not known).
        kicad: The text for this KiCad, when it is not "KiCad <version>".
    """
    if kicad is None:
        kicad = f"KiCad {version}" if version else "the installed KiCad (the version is not known)"
    return (
        f"the server does not know which keys {kicad} reads in which order, and the "
        "severities of the project file give a different result for a different order"
    )


def rule_severities(
    project_file: Path,
    keys: tuple[str, ...],
    defaults: dict[str, str],
    kind: str,
    version: str | None,
) -> dict[str, str | None]:
    """Return the severity of some checks in the project file: "error", "warning" or
    "ignore".

    Args:
        keys: The path of the severities, for example ("erc", "rule_severities").
        defaults: The default severity of each check name.
        kind: "erc" or "drc" (rule_severity_keys).
        version: The version of the KiCad that reads the file, or None when it is
            not known.

    KiCad reads the keys in its order with SeverityFromString. A value that is
    not a text stops the read: the keys after it keep their defaults. When the
    server does not know the order of the version (rule_severity_keys), a
    severity is None (unknown_severity_reason) when a different order gives a
    different severity. With no project file, or a file that is not JSON, KiCad
    uses the defaults.
    """
    data = _project_data(project_file)
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return {
        name: next(iter(possible)) if len(possible) == 1 else None
        for name, possible in _outcomes(data, defaults, kind, version).items()
    }


def _outcomes(
    data: object, defaults: dict[str, str], kind: str, version: str | None
) -> dict[str, set[str]]:
    """Return the severities that a KiCad version can give to each check of defaults,
    from the "rule_severities" object of a project file (_severity_outcomes)."""
    order = severity_keys(kind, version) if version else None
    return _severity_outcomes(data, defaults, order, all_pre_keys(kind))


def _severity_outcomes(
    data: object,
    defaults: dict[str, str],
    order: SeverityKeys | None,
    pre_keys: tuple[tuple[str, str], ...],
) -> dict[str, set[str]]:
    """Return the severities that KiCad can give to each check of defaults, from the
    "rule_severities" object of a project file. A known order gives one severity.

    Args:
        order: The keys of the KiCad version in its order, or None when they are not
            known.
        pre_keys: For no known order: the keys that a version can read before
            the loop (rule_severity_keys.all_pre_keys).
    """
    if not isinstance(data, dict):
        return {name: {default} for name, default in defaults.items()}
    if order is None:
        stops = any(not isinstance(value, str) for value in data.values())
        result = {}
        for name, default in defaults.items():
            own = data.get(name)
            # The key of the check comes after the keys before the loop: with no
            # value that stops the read, its text applies in each order.
            if isinstance(own, str) and not stops:
                result[name] = {_severity(own)}
                continue
            # Else KiCad can stop the read before the key, or not read it.
            possible = {default}
            texts = [own] + [data.get(key) for key, check in pre_keys if check == name]
            possible.update(_severity(text) for text in texts if isinstance(text, str))
            result[name] = possible
        return result
    result = {name: {default} for name, default in defaults.items()}
    pre, loop = order
    for json_key, check in [*pre, *((key, key) for key in loop)]:
        if json_key not in data:
            continue
        value = data[json_key]
        if not isinstance(value, str):
            break
        if check in result:
            result[check] = {_severity(value)}
    return result


@dataclass(frozen=True)
class ErcExclusions:
    """The ERC exclusions of a project (``ERC_SETTINGS::m_ErcExclusions``)."""

    comments: dict[str, str]  # The serialized marker: its comment ("" for none).
    problem: str | None = None  # Why KiCad does not read all entries.


def erc_exclusions(project_file: Path, kicad: int) -> ErcExclusions:
    """Return the ERC exclusions of a project ("erc.erc_exclusions"), as KiCad reads them.

    KiCad 8 skips an empty entry (null, [] or {}) and reads each other entry as a
    text. KiCad 9 and 10 read a text, and an array [text, comment]: they keep the
    text before they read the comment. They skip each other entry. An entry that
    KiCad cannot read throws: KiCad keeps the exclusions before it and ignores
    the others (JSON_SETTINGS::Load). An array with less than two items has no
    defined result in KiCad (an index out of the range).

    Args:
        kicad: The major version of the KiCad that reads the file.
    """
    data = _value(_project_data(project_file), "erc", "erc_exclusions")
    comments: dict[str, str] = {}
    if not isinstance(data, list):
        return ErcExclusions(comments)

    def stop(entry: object, what: str) -> ErcExclusions:
        return ErcExclusions(
            comments,
            f"KiCad {kicad} {what} the ERC exclusion {entry!r} of {project_file}: it ignores "
            "the exclusions after it.",
        )

    for entry in data:
        if kicad == 8:
            if entry is None or entry == [] or entry == {}:
                continue
            if not isinstance(entry, str):
                return stop(entry, "cannot read")
            comments[entry] = ""
        elif isinstance(entry, str):
            comments.setdefault(entry, "")
        elif isinstance(entry, list):
            if len(entry) < 2:
                return stop(entry, "has no defined result for")
            if not isinstance(entry[0], str):
                return stop(entry, "cannot read")
            comments[entry[0]] = entry[1] if isinstance(entry[1], str) else ""
            if not isinstance(entry[1], str):
                return stop(entry, "cannot read the comment of")
    return ErcExclusions(comments)
