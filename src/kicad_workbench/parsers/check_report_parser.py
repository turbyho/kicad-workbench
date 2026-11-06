"""Parsers for the ERC and DRC JSON reports of kicad-cli (KiCad 8+).

The tools run kicad-cli in English (a copy of the KiCad settings with the
language English, see ``utils.kicad_cli``), so the item text of their reports is
English. The rules for English text come first. A report that KiCad wrote in an
other language (for example a report of the user): language-neutral fallback
rules get the reference and the position, and the pin fields stay empty.
"""

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import InputError
from ..models.types import DRCError, DRCItem, ERCError, ERCItem
from ..utils.kicad_text import KiCadEncodingError, read_as_kicad
from .kicad_versions import kicad_release, version_triple
from .sch_geometry import Point, node_point
from .sexpr import SExprError, parse

# A reference designator: "U13", "AN-GNSS1", "#PWR055", "R1A".
_REF = r"#?[A-Za-z][\w+-]*\d\w*"
_REF_RE = re.compile(rf"^{_REF}$")
# The reference of a symbol that is not annotated: the prefix and "?"
# (UTIL::GetRefDesUnannotated), for example "R?" or "#PWR?".
_UNANNOTATED_RE = re.compile(r"^#?[A-Za-z][\w+-]*\?$")

# "Symbol U13 Pin D6 [P0.00/XL1, Tri-state, Line]", or "Pin 1 [???, ?]" when
# the library symbol is missing.
_ERC_PIN_EN = re.compile(r"^(?:Symbol (?P<ref>\S+) )?Pin (?P<pin>\S+) \[(?P<attrs>.*)\]$")
# "Symbol U11 [LSM6DSOTR]"
_ERC_SYMBOL_EN = re.compile(r"^Symbol (?P<ref>\S+) \[(?P<value>.*)\]$")
# Localized forms of the two rules above ("Symbol U13 Vývod D6 [...]").
_ERC_PIN_ANY = re.compile(r"^\S+ (?P<ref>\S+) \S+ (?P<pin>\S+) \[(?P<attrs>.*)\]$")
_ERC_SYMBOL_ANY = re.compile(r"^\S+ (?P<ref>\S+) \[(?P<value>.*)\]$")
# "Global Label 'VBAT'", "Label 'SDA'", "Hierarchical Label 'X'"
_QUOTED = re.compile(r"'(?P<text>[^']*)'")

# DRC: "Pad 10 [NET] of U7 on F.Cu", "Reference field of L4", "Footprint C18"
_DRC_BRACKETS = re.compile(r"\[[^\]]*\]")
_DRC_OF_REF = re.compile(rf"\bof (?P<ref>{_REF})\b")
_DRC_FOOTPRINT_REF = re.compile(rf"^Footprint (?P<ref>{_REF})\b")

# IU_PER_MM of schIUScale (100 nm) and of pcbIUScale (1 nm).
_SCH_IU_PER_MM = 10_000
_PCB_IU_PER_MM = 1_000_000


def _attrs_to_pin(attrs: str) -> tuple[str, str]:
    """Split the pin attributes into (pin name, pin type).

    The attributes are "name, type, shape" or "type, shape" for a pin without
    a name. The split starts at the right, because a pin name can contain ", ".
    """
    parts = attrs.rsplit(", ", 2)
    if len(parts) == 3:
        return parts[0], parts[1]
    if len(parts) == 2:
        return "", parts[0]
    return "", ""


def _valid_ref(ref: str | None) -> str:
    """The reference of a localized item text, or "": the structure of the text is not
    known, thus only a text with the form of a reference is one."""
    return ref if ref and (_REF_RE.match(ref) or _UNANNOTATED_RE.match(ref)) else ""


@dataclass(frozen=True)
class ErcUnits:
    """How kicad-cli wrote the values of an ERC JSON report (ERC_REPORT::WriteJsonReport).

    KiCad 8.0, 9.0 and 10.0.0 to 10.0.6 write the report with pcbIUScale, not
    with schIUScale: the positions and the lengths in the item texts are 100
    times too small. KiCad 10.0.7 corrects it ("Fix erc json report using wrong
    unit scale"), and branch master too.
    """

    # The values have pcbIUScale: the rules of MessageTextFromValue of the
    # version for the item texts (_value_text), "8", "9.0.0", "9.0.4" or
    # "10.0.0". None: the values are in mm, or the rules are not known.
    value_rules: str | None = None
    # False: the positions are not known (not in mm, or no rules).
    positions: bool = True
    note: str | None = None  # The limit of the values, for the output.

    @property
    def pcb_scale(self) -> bool:
        """True when the values have pcbIUScale (100 times too small)."""
        return self.value_rules is not None


_NO_RULES = (
    "The server has no rules for the ERC JSON report of KiCad {version}, thus it gives "
    "no positions, and the lengths in the item texts are as the report gives them. KiCad "
    "8.0, 9.0 and 10.0.0 to 10.0.6 write them 100 times too small "
    "(ERC_REPORT::WriteJsonReport)."
)


def erc_report_units(report: dict[str, Any]) -> ErcUnits:
    """Return the units of the values of an ERC JSON report, from its KiCad version.

    The rules come from the KiCad source of each release (ERC_REPORT,
    EDA_UNIT_UTILS::UI::MessageTextFromValue): 8.0.0 to 8.0.9, 9.0.0 to 9.0.9
    and 10.0.0 to 10.0.7. A nightly build gets the rule of branch master.
    """
    version = str(report.get("kicad_version", ""))
    if report.get("coordinate_units", "mm") != "mm":
        return ErcUnits(
            positions=False,
            note=f"The ERC report has the units {report.get('coordinate_units')!r}, not mm: "
            "the server does not give its positions.",
        )
    numbers = version_triple(version)
    if numbers is None:
        return ErcUnits(positions=False, note=_NO_RULES.format(version=version or "(no version)"))
    if kicad_release(version) is None:
        if numbers[0] == 10 and numbers[1] == 99:
            return ErcUnits(
                note=f"KiCad {version} is a nightly build: the server reads its ERC report with "
                "the rule of branch master (the values are in mm). A nightly build before the "
                "correction of KiCad (2026-09) writes them 100 times too small.",
            )
        return ErcUnits(positions=False, note=_NO_RULES.format(version=version))
    major, _minor, patch = numbers
    if major == 8 and patch <= 9:
        return ErcUnits("8")
    if major == 9 and patch <= 9:
        return ErcUnits("9.0.0" if patch <= 3 else "9.0.4")
    if major == 10 and patch <= 6:
        return ErcUnits("10.0.0")
    if major == 10 and patch == 7:
        return ErcUnits()
    return ErcUnits(positions=False, note=_NO_RULES.format(version=version))


def _value_text(value_iu: float, pcb_scale: bool, rules: str, point: str) -> str:
    """Return a distance in mm as MessageTextFromValue gives it, with no unit.

    The rules of each version:

    - "8": "%.4f" (common is built with no EESCHEMA, thus also the schematic
      scale gives 4 decimals).
    - "9.0.0" (9.0.0 to 9.0.3): "%.2f" for schIUScale, "%.4f" for pcbIUScale.
    - "9.0.4" (9.0.4 to 9.0.9): "%.3f" for schIUScale, and the last "0" of the
      3 decimals goes away; "%.4f" for pcbIUScale.
    - "10.0.0" (10.0.0 to 10.0.6): as "9.0.4", and a value that is not 0 but
      gives only zeros has "%.3e".

    Args:
        point: The decimal point of the locale of KiCad.
    """
    value = value_iu / (_PCB_IU_PER_MM if pcb_scale else _SCH_IU_PER_MM)
    short_form = not pcb_scale and rules != "8"
    if rules == "8" or pcb_scale:
        text = f"{value:.4f}"
    elif rules == "9.0.0":
        text = f"{value:.2f}"
    else:
        text = f"{value:.3f}"
    if rules == "10.0.0" and value != 0.0 and not any("1" <= ch <= "9" for ch in text):
        text = f"{value:.3e}"
    if short_form and rules != "9.0.0" and len(text) > 4 and text[-4] == "." and text[-1] == "0":
        text = text[:-1]
    return text.replace(".", point)


def _line_length(start: Point, end: Point, rules: str) -> float:
    """Return the length (IU) of a SCH_LINE as SCH_LINE::GetItemDescription computes it.

    KiCad 8: EuclideanNorm of trigo.h (hypot). KiCad 9 and 10: VECTOR2::Distance,
    whose EuclideanNorm gives |x| * sqrt(2) at 45 degrees, |x| or |y| for a
    horizontal or vertical line, else hypot.
    """
    dx, dy = float(end[0] - start[0]), float(end[1] - start[1])
    if rules == "8":
        return math.hypot(dx, dy)
    if abs(dx) == abs(dy):
        return abs(dx) * math.sqrt(2)
    if dx == 0:
        return abs(dy)
    if dy == 0:
        return abs(dx)
    return math.hypot(dx, dy)


def corrected_length_text(
    description: str, start: Point, end: Point, units: ErcUnits
) -> str | None:
    """Return the item text of a wire, a bus or a graphic line with its correct length.

    SCH_LINE::GetItemDescription ends with the length and " mm". The function
    computes the text that KiCad wrote with pcbIUScale and replaces it with the
    text of schIUScale. The decimal point is the point of the locale of KiCad:
    "." or ",".

    Returns:
        The corrected text, or None when the text does not end with the length
        that KiCad wrote for this line.
    """
    if units.value_rules is None:
        return None
    length = _line_length(start, end, units.value_rules)
    for point in (".", ","):
        wrong = f" {_value_text(length, True, units.value_rules, point)} mm"
        if description.endswith(wrong):
            right = f" {_value_text(length, False, units.value_rules, point)} mm"
            return description[: -len(wrong)] + right
    return None


def schematic_lines(schematic_files: Iterable[Path]) -> dict[str, tuple[Point, Point] | None]:
    """Return the ends (IU) of each wire, bus and graphic line by its UUID.

    A UUID that two lines with different ends have gives None: the line of
    the report is not known. A file that the server cannot read, and a line
    with a point that KiCad cannot read, add no line.
    """
    lines: dict[str, tuple[Point, Point] | None] = {}
    for path in schematic_files:
        try:
            root = parse(read_as_kicad(path))
        except (OSError, KiCadEncodingError, SExprError):
            continue
        for head in ("wire", "bus", "polyline"):
            for node in root.children(head):
                pts = node.first("pts")
                try:
                    ends = [node_point(xy) for xy in pts.children("xy")] if pts else []
                except InputError:
                    continue
                uuid = node.first("uuid")
                # A polyline with two points is a SCH_LINE (a graphic line).
                if len(ends) != 2 or uuid is None:
                    continue
                key = uuid.value(0, "")
                line = (ends[0], ends[1])
                lines[key] = line if lines.get(key, line) == line else None
    return lines


_NOT_KICAD = "The report is not a report of KiCad"
_KIND_NAMES = {list: "a list", dict: "a JSON object", str: "a text", bool: "a boolean"}


def _coordinate(value: Any, where: str = "a coordinate") -> float:
    """Return a coordinate of a report. KiCad writes it as a JSON number
    (RC_ITEM::GetJsonViolation: a double).

    Raises:
        InputError: The value is not a finite number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputError(f"{_NOT_KICAD}: {where} is {value!r}, not a number.")
    return float(value)


def parse_erc_item(item: dict[str, Any], pcb_scale: bool = False) -> ERCItem:
    """Parse one item of an ERC violation (RC_JSON::AFFECTED_ITEM: uuid, description
    and pos, as _violations checks them).

    Args:
        pcb_scale: The positions have pcbIUScale (ErcUnits).

    Raises:
        KeyError: The item is not an item of KiCad (the reports check it first).
        InputError: A coordinate is not a number.
    """
    desc = item["description"]
    out = ERCItem(description=desc, uuid=item["uuid"])
    x, y = _coordinate(item["pos"]["x"]), _coordinate(item["pos"]["y"])
    if pcb_scale:
        # The value is IU / 1e6 with schematic IU: the IU are exact integers.
        out.x_mm = round(x * _PCB_IU_PER_MM) / _SCH_IU_PER_MM
        out.y_mm = round(y * _PCB_IU_PER_MM) / _SCH_IU_PER_MM
    else:
        out.x_mm, out.y_mm = x, y

    # The English item text of a symbol and of its pin has the text of the Reference
    # field as it is (SCH_SYMBOL::GetItemDescription, SCH_PIN::GetItemDescription):
    # also "R?" or "R" of a symbol that is not annotated.
    m = _ERC_PIN_EN.match(desc)
    if m:
        out.reference = m["ref"] or ""
        out.pin_number = m["pin"]
        out.pin_name, out.pin_type = _attrs_to_pin(m["attrs"])
        return out

    m = _ERC_SYMBOL_EN.match(desc)
    if m:
        out.reference = m["ref"]
        out.value = m["value"]
        return out

    m = _QUOTED.search(desc)
    if m:
        out.label = m["text"]
        return out

    # Localized text: get only the structure. The pin type is not English,
    # thus it stays empty.
    m = _ERC_PIN_ANY.match(desc)
    if m and _valid_ref(m["ref"]):
        out.reference = m["ref"]
        out.pin_number = m["pin"]
        out.pin_name, _ = _attrs_to_pin(m["attrs"])
        return out

    m = _ERC_SYMBOL_ANY.match(desc)
    if m and _valid_ref(m["ref"]):
        out.reference = m["ref"]
        out.value = m["value"]
    return out


def _require(data: Any, key: str, kind: type, where: str) -> Any:
    """Return data[key] of a report, of the type of RC_JSON (include/rc_json_schema.h
    of KiCad 8, 9 and 10).

    Raises:
        InputError: The value is not there, or it has an other type.
    """
    if not isinstance(data, dict):
        raise InputError(f"{_NOT_KICAD}: {where} is not a JSON object.")
    if key not in data:
        raise InputError(f"{_NOT_KICAD}: {where} has no {key!r}.")
    if not isinstance(data[key], kind):
        raise InputError(f"{_NOT_KICAD}: {key!r} of {where} is not {_KIND_NAMES[kind]}.")
    return data[key]


def _violations(container: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    """Return the violations of a list of a report, after the check of their keys.

    KiCad writes type, description, severity and items for each violation, and
    excluded and comment only for an excluded violation (RC_JSON::to_json of
    VIOLATION). Each item has uuid, description and pos (AFFECTED_ITEM).

    Raises:
        InputError: The list is not the list of KiCad.
    """
    violations = _require(container, key, list, where)
    for number, violation in enumerate(violations, 1):
        # The places count from 1: "violation 1" is the first violation of the list.
        at = f"violation {number} of {key!r} of {where}"
        for name in ("type", "description", "severity"):
            _require(violation, name, str, at)
        for name, kind in (("excluded", bool), ("comment", str)):
            if name in violation:
                _require(violation, name, kind, at)
        for index, item in enumerate(_require(violation, "items", list, at), 1):
            item_at = f"item {index} of {at}"
            _require(item, "uuid", str, item_at)
            _require(item, "description", str, item_at)
            pos = _require(item, "pos", dict, item_at)
            for axis in ("x", "y"):
                if axis not in pos:
                    raise InputError(f"{_NOT_KICAD}: 'pos' of {item_at} has no {axis!r}.")
                _coordinate(pos[axis], f"{axis!r} of 'pos' of {item_at}")
    return violations


@dataclass
class ErcReport:
    """The violations of an ERC JSON report, and the limits of its values."""

    errors: list[ERCError]
    notes: list[str] = field(default_factory=list)
    units: ErcUnits = field(default_factory=ErcUnits)


def read_erc_report(
    report: dict[str, Any], schematic_files: Iterable[Path] = (), source: str = "the ERC report"
) -> ErcReport:
    """Read the data of an ERC JSON report of kicad-cli, by the RC_JSON structs of
    KiCad (_violations).

    The positions get the units of the KiCad version (erc_report_units). With
    the board scale, an item text that ends with " mm" has a length: only
    SCH_LINE::GetItemDescription gives a value in an ERC item text. The text of
    a wire, a bus or a graphic line of the schematic files gets its correct
    length (corrected_length_text); a note counts the other texts.

    Args:
        source: The report for the messages, for example "the ERC report <path>".

    Raises:
        InputError: The data is not an ERC report of KiCad (the message gives the
            place).
    """
    sheets = _require(report, "sheets", list, source)
    units = erc_report_units(report)
    errors = []
    lengths = []  # The items whose text has a length in the board scale.
    for number, sheet in enumerate(sheets, 1):
        where = f"sheet {number} of {source}"
        path = _require(sheet, "path", str, where)
        for v in _violations(sheet, "violations", where):
            items = []
            for raw in v["items"]:
                item = parse_erc_item(raw, units.pcb_scale)
                if not units.positions:
                    item.x_mm = item.y_mm = None
                if units.pcb_scale and item.description.endswith(" mm"):
                    lengths.append(item)
                items.append(item)
            errors.append(
                ERCError(
                    severity=v["severity"],
                    type=v["type"],
                    description=v["description"],
                    components=[i.reference for i in items if i.reference],
                    items=items,
                    excluded=v.get("excluded", False),
                    comment=v.get("comment", ""),
                    sheet=path,
                )
            )
    not_corrected = 0
    lines = schematic_lines(schematic_files) if lengths else {}
    for item in lengths:
        line = lines.get(item.uuid)
        text = corrected_length_text(item.description, *line, units) if line else None
        if text is None:
            not_corrected += 1
        else:
            item.description = text
    notes = [units.note] if units.note else []
    if not_corrected:
        notes.append(
            f"KiCad {report.get('kicad_version')} writes the lengths of the item texts 100 "
            f"times too small: the server could not correct {not_corrected} item text(s) "
            "(not a wire, a bus or a graphic line of the schematic files, two lines with one "
            "UUID, or a text that is not the text of KiCad). Their lengths stay 100 times "
            "too small."
        )
    return ErcReport(errors, notes, units)


def read_report_file(path: Path, kind: str) -> dict[str, Any]:
    """Return the data of a JSON report file of kicad-cli (ERC or DRC). kicad-cli
    writes it as UTF-8 JSON (ERC_REPORT::WriteJsonReport, DRC_REPORT).

    Raises:
        OSError: The file cannot be read.
        InputError: The file is not UTF-8 JSON, or not a JSON object.
    """
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    # RecursionError: JSON that is nested too deep for the parser.
    except (ValueError, RecursionError) as e:
        raise InputError(f"The {kind} report {path} is not UTF-8 JSON: {e}") from e
    if not isinstance(data, dict):
        raise InputError(f"The {kind} report {path} is not a JSON object.")
    return data


def parse_erc_report(report: dict[str, Any]) -> list[ERCError]:
    """Parse the data of an ERC JSON report into ERC errors (read_erc_report with no
    schematic files: the item texts of lines keep the length of the report)."""
    return read_erc_report(report).errors


def parse_drc_item(item: dict[str, Any]) -> DRCItem:
    """Parse one item of a DRC violation (RC_JSON::AFFECTED_ITEM, as _violations
    checks it).

    Raises:
        KeyError: The item is not an item of KiCad (the reports check it first).
        InputError: A coordinate is not a number.
    """
    desc = item["description"]
    out = DRCItem(description=desc, uuid=item["uuid"])
    out.x_mm = _coordinate(item["pos"]["x"])
    out.y_mm = _coordinate(item["pos"]["y"])

    # Remove the net names in brackets first: a net name can contain " of ".
    plain = _DRC_BRACKETS.sub("", desc)
    m = _DRC_OF_REF.search(plain) or _DRC_FOOTPRINT_REF.match(plain)
    if m:
        out.reference = m["ref"]
    return out


_DRC_CATEGORIES = (
    ("violations", "violation"),
    ("unconnected_items", "unconnected"),
    ("schematic_parity", "schematic_parity"),
)


def parse_drc_report(report: dict[str, Any], source: str = "the DRC report") -> list[DRCError]:
    """Parse the data of a DRC JSON report into DRC errors.

    The DRC report has the top-level lists ``violations``,
    ``unconnected_items`` and ``schematic_parity``. Each list is parsed.

    Args:
        source: The report for the messages, for example "the DRC report <path>".

    Raises:
        InputError: The data is not a DRC report of KiCad (the message gives the
            place).
    """
    if not isinstance(report, dict):
        raise InputError(f"{_NOT_KICAD}: {source} is not a JSON object.")
    errors = []
    for key, category in _DRC_CATEGORIES:
        for v in _violations(report, key, source):
            items = [parse_drc_item(i) for i in v["items"]]
            first = items[0] if items else None
            location = (first.x_mm or 0.0, first.y_mm or 0.0) if first is not None else (0.0, 0.0)
            refs = list(dict.fromkeys(i.reference for i in items if i.reference))
            errors.append(
                DRCError(
                    severity=v["severity"],
                    type=v["type"],
                    description=v["description"],
                    location=location,
                    category=category,
                    components=refs,
                    items=items,
                    excluded=v.get("excluded", False),
                    comment=v.get("comment", ""),
                )
            )
    return errors
