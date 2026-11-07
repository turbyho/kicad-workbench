"""The sheet list of a project, as KiCad builds it after the load.

The rules come from the source of KiCad 8.0.9, 9.0.9 and 10.0.6:

1. The loader (``SCH_IO_KICAD_SEXPR::loadHierarchy``) reads each top-level
   sheet, then each sheet symbol of a file in the order of the file. It
   expands the text variables of a sheet file name, and makes a relative
   name absolute with the directory of the parent file. A file that is an
   ancestor of the sheet, a missing file and a file that the loader cannot
   read give an empty screen. A file that the loader read before gives the
   same screen (``SearchHierarchy``): the loader does not read its sheets
   again.
2. ``SCH_SHEET_LIST::BuildSheetList`` adds each sheet path in the order of a
   depth-first walk. It takes the sheets of a screen in the order of
   ``SCH_SCREEN::GetSheets`` (x, then y, then the UUID). It does not walk
   into a child sheet with the file name of its parent sheet. The DFS count
   of a path is its place in the list of its top-level sheet (1 is the
   top-level sheet). It is the virtual page number before the sort.
3. ``SCH_SHEET_LIST::SortByPageNumbers`` sorts the paths with
   ``SCH_SHEET::ComparePageNum``, then the DFS count, then a hash of the path
   (``GetCurrentHash``). Then the virtual page numbers are 1, 2, 3 and more
   in the new order.

The page of a sheet path is the (page ...) of the sheet symbol for the path
of the parent (``SCH_SHEET::getPageNumber``, the first match). The page of a
top-level sheet is the (sheet_instances (path "/" (page ...))) of its file
(``parseSchSheetInstances``, the last entry). Such an entry in an other file
sets a page that no path uses. The parser changes an empty page to "#" and
removes "\\r", "\\n", "\\t" and " ".

The loader compares the file names as texts (sheet_file_names): two names of one
file (a symbolic link, "/./") are two screens, as in KiCad.

A tolerant load (``build_sheet_list`` with ``tolerant``) is for the tools that
need the files of a hierarchy, not its items: the lock check and the search of
the project of a file (utils/sch_hierarchy). It does not stop at a file that the
server cannot read: a file of an unknown version gives the sheets of the rules
of KiCad 8 and 9 and of KiCad 10, a parse error gives a screen with no sheets,
and each gives a problem. Thus it gives more files than KiCad loads, never
fewer.

Limit: the hash of a path depends on the build of boost. The sort uses it
only for two paths with the same page and the same DFS count: these are in
different top-level sheets of KiCad 10. ``SheetList.hash_ties`` gives these
paths.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from ..errors import InputError, KicadFileError, KicadParseError, with_file
from ..utils.file_stamp import FileStamp, read_stamped
from ..utils.kicad_project import (
    NIL_UUID,
    TopLevelSheet,
    expand_text_variables,
    top_level_file,
)
from ..utils.kicad_text import KiCadEncodingError, decode_as_kicad
from .c_numbers import to_int, to_long
from .kicad_fields import FieldRules, rules_for_file, sheet_fields
from .kicad_versions import file_version, schematic_version
from .sch_geometry import Point, definitions, node_point
from .sexpr import Node, SExprError, parse
from .sheet_file_names import (
    WINDOWS,
    KicadFileName,
    SheetFileError,
    sheet_file_name,
    uses_home,
)

_UUID_RE = re.compile(
    r"\A(\{?)([0-9a-fA-F]{8})(-?)([0-9a-fA-F]{4})\3([0-9a-fA-F]{4})\3"
    r"([0-9a-fA-F]{4})\3([0-9a-fA-F]{12})(\}?)\Z"
)
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_PAGE_SPACES = str.maketrans("", "", "\r\n\t ")


@dataclass(frozen=True, eq=False)
class RandomKiid:
    """A KIID that KiCad makes at random: the text in the file is not a UUID
    (``KIID::KIID``), or the item has no UUID. It is equal only to itself."""

    text: str | None


Kiid = bytes | RandomKiid


def kiid(text: str | None) -> Kiid:
    """Return the KIID of a UUID text, as ``KIID::KIID( const std::string& )`` reads it.

    A text of 1 to 8 hex digits is a legacy time stamp: the last 4 bytes. The
    boost string generator reads a UUID with or without the dashes and the
    braces. Other texts give a new random KIID.
    """
    if text is None:
        return RandomKiid(None)
    data = text.encode("utf-8")
    if data and len(data) <= 8 and all(chr(b) in _HEX_DIGITS for b in data):
        octets = bytearray(16)
        for i in range(4):
            start = len(data) - 8 + i * 2
            end = start + 2
            start = max(0, start)
            octet = data[start : max(start, end)]
            octets[12 + i] = int(octet, 16) if octet else 0
        return bytes(octets)
    match = _UUID_RE.match(text)
    if match is None or (match.group(1) == "{") != (match.group(8) == "}"):
        return RandomKiid(text)
    return bytes.fromhex("".join(match.group(i) for i in (2, 4, 5, 6, 7)))


def kiid_text(value: Kiid) -> str | None:
    """``KIID::AsString``: the UUID in lower case with dashes. None for a KIID that
    KiCad makes at random (its text is not known)."""
    if isinstance(value, RandomKiid):
        return None
    text = value.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def kiid_path(text: str) -> tuple[Kiid, ...]:
    """Return the KIIDs of a path text "/<uuid>/<uuid>" (``KIID_PATH::KIID_PATH``).
    "/" is the empty path."""
    return tuple(kiid(part) for part in text.split("/") if part)


def kiid_less(a: Kiid, b: Kiid) -> bool | None:
    """Return ``a < b`` as ``KIID::operator<``, or None when KiCad makes one of the
    KIIDs at random (the order is not known)."""
    if isinstance(a, RandomKiid) or isinstance(b, RandomKiid):
        return None
    return a < b


def page_text(raw: str) -> str:
    """Return a page as the parser keeps it: "#" for an empty page, and no "\\r",
    "\\n", "\\t" and " "."""
    return "#" if raw == "" else raw.translate(_PAGE_SPACES)


def str_num_cmp(text1: str, text2: str) -> int:
    """Compare two texts as ``StrNumCmp`` with case (the numbers in the texts by
    value, with a 32-bit int)."""
    i = j = 0
    while i < len(text1) and j < len(text2):
        c1, c2 = text1[i], text2[j]
        if "0" <= c1 <= "9" and "0" <= c2 <= "9":
            nb1 = nb2 = 0
            while True:
                nb1 = to_int(nb1 * 10 + ord(text1[i]) - ord("0"))
                i += 1
                if i >= len(text1) or not "0" <= text1[i] <= "9":
                    break
            while True:
                nb2 = to_int(nb2 * 10 + ord(text2[j]) - ord("0"))
                j += 1
                if j >= len(text2) or not "0" <= text2[j] <= "9":
                    break
            if nb1 != nb2:
                return -1 if nb1 < nb2 else 1
            c1 = text1[i] if i < len(text1) else "\0"
            c2 = text2[j] if j < len(text2) else "\0"
        if c1 != c2:
            return -1 if c1 < c2 else 1
        if i < len(text1):
            i += 1
        if j < len(text2):
            j += 1
    if i >= len(text1) and j < len(text2):
        return -1
    if i < len(text1) and j >= len(text2):
        return 1
    return 0


def compare_page_num(page_a: str, page_b: str) -> int | None:
    """Compare two pages as ``SCH_SHEET::ComparePageNum``: integers first, by
    value, then the texts with ``StrNumCmp``.

    Returns None for two different texts of the same integer ("1" and "01"):
    KiCad then gives "greater" in the two directions, thus the order of
    ``std::sort`` is not known.
    """
    if page_a == page_b:
        return 0
    long_a, long_b = to_long(page_a), to_long(page_b)
    if long_a is not None and long_b is not None:
        if long_a == long_b:
            return None
        return -1 if long_a < long_b else 1
    if long_a is not None:
        return -1
    if long_b is not None:
        return 1
    result = str_num_cmp(page_a, page_b)
    return 0 if result == 0 else (1 if result > 0 else -1)


@dataclass
class Screen:
    """A schematic file that the loader read (``SCH_SCREEN``)."""

    file: Path  # the file that the loader opens: the text of name
    # SCH_SCREEN::GetFileName: the loader compares its text (key) with the names of
    # the ancestors and of the other screens.
    name: KicadFileName
    root: Node
    stamp: FileStamp  # of the bytes that the parser read (file_stamp.read_stamped)
    version: int
    rules: FieldRules
    library: dict[str, Node]  # the definitions of lib_symbols
    # The KIID of each (uuid ...) that the parser reads with parseKIID, by the
    # offset of the (uuid ...): see parsed_kiids.
    kiids: dict[int, Kiid] = field(default_factory=dict)
    sheets: list["SheetItem"] = field(default_factory=list)
    # The (sheet_instances (path ...)) entries with a path that is not "/":
    # (path, page). kicad-cli uses the entries of the root file
    # (SCH_SHEET_LIST::UpdateSheetInstanceData).
    sheet_instances: list[tuple[tuple[Kiid, ...], str]] = field(default_factory=list)
    # A screen of a tolerant load only: why its sheets can be not complete (a parse
    # error, a version that the server does not know), or "". A screen of a tolerant
    # load has no parse tree (root is empty), no library and no sheet instances.
    partial: str = ""
    stopped: bool = False  # a tolerant load: the parser of KiCad stops in the file
    # A tolerant load: the KIID and the UUID text of the file (item_kiid of the root).
    file_kiid: tuple[Kiid, str | None] | None = None

    @property
    def key(self) -> str:
        """The text of the file name that KiCad gives the screen."""
        return self.name.text

    @property
    def kicad(self) -> int:
        """The field rules of the file: 9 (KiCad 8 and 9) or 10."""
        return self.rules.kicad


# The reasons of an empty sheet (SheetItem.empty). SCH_IO_KICAD_SEXPR::loadHierarchy
# gives such a sheet a new, empty screen: also for an empty file name (the directory
# of the parent, which loadFile cannot read) and an ancestor (the name is emptied).
# The first sheet path decides for a sheet of a shared screen (the sheet object gets
# one screen), as here (SheetItem.loaded). Measured with kicad-cli 8.0.9 to 10.0.7
# (spike/kicad-versions/sheet_fields_check.py): no file name and an empty expansion.
EMPTY_REASONS = {
    "no file name": "the sheet has no file name",
    "empty expansion": "the file name is empty after the expansion of the text variables",
    "ancestor": "the file holds its own ancestor",
    "not there": "the file is not there",
    "cannot be read": "the system cannot open the file",
    # A tolerant load only: the strict load gives an error (sheet_file_names).
    "working directory": "KiCad opens the file relative to its working directory",
}


@dataclass(eq=False)
class SheetItem:
    """A sheet symbol of a screen."""

    node: Node | None  # None: a tolerant load (no parse tree)
    index: int  # the place of the sheet in the sheets of its file
    kiid: Kiid
    uuid: str | None
    name: str
    raw_file: str  # the Sheetfile field, as the file holds it
    at: Point
    # The pages of its last (instances ...): (the path of the parent, the page).
    pages: list[tuple[tuple[Kiid, ...], str]]
    screen: Screen | None = None  # None: an empty screen
    loaded: bool = False  # the loader gave it a screen
    # The file name that the loader gives the sheet (KicadFileName.text), after the
    # expansion of the text variables; None: an empty file name.
    file: str | None = None
    # Why KiCad shows an empty sheet (no screen), or "" (EMPTY_REASONS).
    empty: str = ""


@dataclass(eq=False)
class SheetPath:
    """One sheet path of the sheet list (``SCH_SHEET_PATH``)."""

    screen: Screen | None  # None: an empty screen
    kiids: tuple[Kiid, ...]  # ``SCH_SHEET_PATH::Path``
    uuids: tuple[str | None, ...]  # the UUID texts of the path
    names: tuple[str, ...]  # the name of each sheet below the top level
    item: SheetItem | None  # the sheet symbol; None for a top-level sheet
    top: int  # the index of the top-level sheet
    dfs: int  # the DFS count in its top-level sheet

    @property
    def path(self) -> str:
        """The UUID path, as KiCad writes it: "/<uuid>/<uuid>"."""
        return "/" + "/".join(uuid or "?" for uuid in self.uuids)

    @property
    def shown(self) -> str:
        """The sheet names, for example "/Power/Regulator/". The root is "/"."""
        return "/" + "".join(f"{name}/" for name in self.names)

    @property
    def parent(self) -> tuple[Kiid, ...]:
        return self.kiids[:-1]


@dataclass
class SheetList:
    """The sheet paths of a project, the pages, and the problems of the load."""

    paths: list[SheetPath]  # in the order of BuildSheetList
    screens: dict[str, Screen]  # by the text of the file name (Screen.key)
    # The page of each sheet path: by (sheet item, path of the parent), or by the
    # index of the top-level sheet. KiCad keeps the page in the sheet symbol for
    # the path of its parent, thus two paths with the same KIIDs share it.
    pages: dict[object, str]
    problems: list[str]

    def page_key(self, path: SheetPath) -> object:
        if path.item is None:
            return ("top", path.top)
        return (id(path.item), path.parent)

    def page(self, path: SheetPath) -> str:
        """``SCH_SHEET_PATH::GetPageNumber``: "" when the sheet has no page."""
        return self.pages.get(self.page_key(path), "")

    def set_page(self, path: SheetPath, page: str) -> None:
        """``SCH_SHEET_PATH::SetPageNumber``."""
        self.pages[self.page_key(path)] = page

    def hash_ties(self, page: Callable[[SheetPath], str] | None = None) -> list[list[SheetPath]]:
        """Return the groups of paths with the same page and the same DFS count. The
        sort of KiCad puts them in the order of a hash that the tool cannot know.

        Args:
            page: The page of a path. None: the pages of the files.
        """
        page = page or self.page
        groups: dict[tuple[str, int], list[SheetPath]] = {}
        for path in self.paths:
            groups.setdefault((page(path), path.dfs), []).append(path)
        return [group for group in groups.values() if len(group) > 1]


# The items whose (uuid ...) the parser reads with parseKIID (KiCad 8.0.9, 9.0.9 and
# 10.0.6): the items of a sheet, and the pins of a sheet or a placed symbol.
# parseSchText reads the text and each label.
_KIID_ITEMS = {
    "sheet",
    "symbol",
    "image",
    "junction",
    "no_connect",
    "bus_entry",
    "polyline",
    "wire",
    "bus",
    "text",
    "label",
    "global_label",
    "hierarchical_label",
    "directive_label",
    "netclass_flag",
}
_KIID_ITEMS_10 = {"table", "group"}  # parseSchTable and parseGroup of KiCad 10


def _increment(value: bytes) -> bytes:
    """``KIID::Increment``: the 16 bytes plus 1, with no carry out of the first byte."""
    return ((int.from_bytes(value, "big") + 1) % 2**128).to_bytes(16, "big")


def parsed_kiids(root: Node, kicad: int) -> dict[int, Kiid]:
    """Return the KIIDs that the parser gives the items of a schematic file.

    ``SCH_IO_KICAD_SEXPR_PARSER::parseKIID`` increments a UUID that the file used
    before, in the order of the text, until it is new. The KIID of each item thus
    is unique in its file. The paths of the instances are not changed.

    Args:
        kicad: The major version of the KiCad that reads the file.

    Returns:
        The KIID of each (uuid ...), by the offset of the (uuid ...).
    """
    items = _KIID_ITEMS | (_KIID_ITEMS_10 if kicad >= 10 else set())
    nodes = list(root.children("uuid"))
    for item in root.children():
        if item.head in items:
            nodes += item.children("uuid")
            if item.head in ("sheet", "symbol"):
                for pin in item.children("pin"):
                    nodes += pin.children("uuid")
        elif item.head == "rule_area":
            # parseSchRuleArea reads its polyline with parseSchPolyLine.
            for polyline in item.children("polyline"):
                nodes += polyline.children("uuid")
    used: set[Kiid] = set()
    result: dict[int, Kiid] = {}
    for node in sorted(nodes, key=lambda n: n.start):
        value = kiid(node.value(0, ""))
        while value in used and isinstance(value, bytes):
            value = _increment(value)
        used.add(value)
        result[node.start] = value
    return result


def _item_kiid(kiids: dict[int, Kiid], item: Node) -> tuple[Kiid, str | None]:
    """item_kiid with the KIIDs of parsed_kiids."""
    nodes = item.children("uuid")
    if not nodes:
        return RandomKiid(None), None
    node = nodes[-1]
    return kiids.get(node.start, kiid(node.value(0, ""))), node.value(0)


def item_kiid(screen: Screen, item: Node) -> tuple[Kiid, str | None]:
    """Return the KIID of an item after parseKIID, and the text of its last (uuid ...).
    An item with no (uuid ...) gets a random KIID."""
    return _item_kiid(screen.kiids, item)


def file_kiid(screen: Screen) -> tuple[Kiid, str | None]:
    """Return the KIID and the UUID text of the file of a screen (its root item)."""
    if screen.file_kiid is not None:
        return screen.file_kiid
    return item_kiid(screen, screen.root)


def _sheet_pages(sheet: Node) -> list[tuple[tuple[Kiid, ...], str]]:
    """The pages of the last (instances ...) of a sheet (setInstances)."""
    pages: list[tuple[tuple[Kiid, ...], str]] = []
    for instances in sheet.children("instances"):
        pages = []
        for project in instances.children("project"):
            for path in project.children("path"):
                page = path.first("page")
                pages.append(
                    (
                        kiid_path(path.value(0, "")),
                        page_text(page.value(0, "")) if page is not None else "",
                    )
                )
    return pages


def _sheet_item(screen: Screen, sheet: Node, rules: FieldRules, index: int) -> SheetItem:
    """Return a sheet symbol of a file.

    Raises:
        KicadFileError: KiCad cannot read the sheet fields.
        InputError: A number of the position is not a number that KiCad reads.
    """
    fields = sheet_fields(sheet, rules)
    sheet_kiid, uuid = item_kiid(screen, sheet)
    at = sheet.first("at")
    return SheetItem(
        sheet,
        index,
        sheet_kiid,
        uuid,
        fields["Sheetname"],
        fields["Sheetfile"],
        node_point(at) if at is not None else (0, 0),
        _sheet_pages(sheet),
    )


def _sheet_instances(root: Node) -> tuple[list[tuple[tuple[Kiid, ...], str]], str | None]:
    """Return the (sheet_instances (path ...)) entries of a file with a path that is
    not "/", and the page of its (path "/" ...) or None."""
    entries: list[tuple[tuple[Kiid, ...], str]] = []
    root_page = None
    for instances in root.children("sheet_instances"):
        for path in instances.children("path"):
            page_node = path.first("page")
            page = page_text(page_node.value(0, "")) if page_node is not None else ""
            key = kiid_path(path.value(0, ""))
            if key:
                entries.append((key, page))
            else:
                root_page = page
    return entries, root_page


def _read_screen(name: KicadFileName, kicad: int | None) -> tuple[Screen, str | None]:
    """Read a schematic file as the parser does. Return the screen and the page of
    its (sheet_instances (path "/" ...)), or None.

    Raises:
        OSError: The file cannot be read.
        KiCadEncodingError, SExprError: The parser of KiCad stops (a byte that is
            not UTF-8, unbalanced parentheses).
        KicadParseError: The parser of KiCad stops: a sub-symbol name, or KiCad 10
            and a sheet with no name field.
        KicadFileError: The file has a version that the server does not know, or
            KiCad 8 or 9 and a sheet with less than two fields.
        LocaleNumberError: A number of the file depends on the locale of KiCad.
    """
    file = Path(name.text)
    data, stamp = read_stamped(file)
    root = parse(decode_as_kicad(data, name.text))
    rules = rules_for_file(root)
    screen = Screen(file, name, root, stamp, schematic_version(root), rules, definitions(root))
    screen.kiids = parsed_kiids(root, kicad if kicad is not None else rules.kicad)
    screen.sheets = [
        _sheet_item(screen, sheet, rules, index)
        for index, sheet in enumerate(root.children("sheet"))
    ]
    screen.sheet_instances, root_page = _sheet_instances(root)
    return screen, root_page


@dataclass(frozen=True)
class _TolerantFile:
    """The parts of a file that a tolerant load needs. The cache keeps no parse tree."""

    version: int
    rules: FieldRules
    note: str  # a version that the server does not know, or ""
    file_kiid: tuple[Kiid, str | None]
    root_page: str | None
    # The sheet items: (index, KIID, UUID text, name, file, position, pages). A sheet
    # of a file of an unknown version has an item for each file of the two rule sets.
    sheets: tuple[tuple, ...]
    errors: tuple[str, ...]  # the sheets that the server cannot read


# The parts of the files of a tolerant load, by the stamp of the content and the KiCad
# of parsed_kiids. One tool call loads the same hierarchy more than one time (the
# candidate roots of a file), and a large schematic takes a long time to parse.
_TOLERANT_CACHE: dict[tuple[str, int | None], _TolerantFile] = {}
_TOLERANT_CACHE_SIZE = 1024


def _tolerant_file(data: bytes, key: str, kicad: int | None) -> _TolerantFile:
    """Read the parts of a file for a tolerant load. key: the file name of KiCad, for
    the messages.

    Raises:
        KiCadEncodingError, SExprError: The parser of KiCad stops.
    """
    root = parse(decode_as_kicad(data, key))
    version = file_version(root) or 0
    try:
        all_rules = [rules_for_file(root)]
        note = ""
    except KicadFileError as e:
        # The KiCad that loads the file is not known: the kicad-cli of PATH is not
        # always the KiCad of the user. The sheets of the two rule sets.
        all_rules = [FieldRules(9, version), FieldRules(10, version)]
        note = f"{e} The sheets are the sheets of the rules of KiCad 8, 9 and 10."
    kiids = parsed_kiids(root, kicad if kicad is not None else all_rules[-1].kicad)
    sheets: list[tuple] = []
    errors: list[str] = []
    for index, sheet in enumerate(root.children("sheet")):
        sheet_kiid, uuid = _item_kiid(kiids, sheet)
        at = sheet.first("at")
        try:
            point = node_point(at) if at is not None else (0, 0)
        except InputError as e:
            errors.append(str(e))
            continue
        names: list[tuple[str, str]] = []
        failures: list[str] = []
        for rules in all_rules:
            try:
                fields = sheet_fields(sheet, rules)
            except KicadFileError as e:
                failures.append(str(e))
                continue
            # One sheet item for each file: the two rule sets can give one sheet two
            # names, but KiCad gives it one sheet path.
            if all(raw != fields["Sheetfile"] for _name, raw in names):
                names.append((fields["Sheetname"], fields["Sheetfile"]))
        if not names:
            errors.append(failures[0])  # Only this sheet: the other sheets stay.
        pages = tuple(_sheet_pages(sheet))
        sheets += [(index, sheet_kiid, uuid, name, raw, point, pages) for name, raw in names]
    _entries, root_page = _sheet_instances(root)
    return _TolerantFile(
        version,
        all_rules[0],
        note,
        _item_kiid(kiids, root),
        root_page,
        tuple(sheets),
        tuple(errors),
    )


# The root of a screen of a tolerant load: the load keeps no parse tree.
_EMPTY_ROOT = parse("(kicad_sch)")


def _read_tolerant(name: KicadFileName, kicad: int | None) -> tuple[Screen, str | None, list[str]]:
    """Read a schematic file for a tolerant load: the screen, the page of its
    (sheet_instances (path "/" ...)) or None, and the problems.

    Raises:
        OSError: The file cannot be read.
    """
    file = Path(name.text)
    data, stamp = read_stamped(file)
    parts = _TOLERANT_CACHE.get((stamp, kicad))
    if parts is None:
        try:
            parts = _tolerant_file(data, name.text, kicad)
        except (KiCadEncodingError, SExprError) as e:
            # Not in the cache: the message can name the file.
            reason = f"the parser of KiCad stops: {e}"
            screen = Screen(file, name, _EMPTY_ROOT, stamp, 0, FieldRules(10, 0), {})
            screen.partial, screen.stopped = reason, True
            screen.file_kiid = (RandomKiid(None), None)
            problem = reason if name.text in str(e) else f"{name.text}: {reason}"
            return screen, None, [problem]
        if len(_TOLERANT_CACHE) >= _TOLERANT_CACHE_SIZE:
            _TOLERANT_CACHE.clear()
        _TOLERANT_CACHE[(stamp, kicad)] = parts
    screen = Screen(file, name, _EMPTY_ROOT, stamp, parts.version, parts.rules, {})
    screen.partial, screen.file_kiid = parts.note, parts.file_kiid
    screen.sheets = [
        SheetItem(None, index, sheet_kiid, uuid, sheet_name, raw, point, list(pages))
        for index, sheet_kiid, uuid, sheet_name, raw, point, pages in parts.sheets
    ]
    problems = [f"{name.text}: {parts.note}"] if parts.note else []
    problems += [f"{name.text}: {error}" for error in parts.errors]
    return screen, parts.root_page, problems


def screens_by_file(screens: list[Screen]) -> dict[Path, list[Screen]]:
    """Return the screens of each file of the disk (Path.resolve), in their order. Two
    names of one file (a symbolic link, "/./") are two screens of KiCad."""
    result: dict[Path, list[Screen]] = {}
    for screen in screens:
        try:
            real = screen.file.resolve()
        except (OSError, RuntimeError):
            # RuntimeError: a loop of symbolic links (Python 3.12 and earlier).
            real = screen.file
        result.setdefault(real, []).append(screen)
    return result


def _sheet_order(a: SheetItem, b: SheetItem) -> int:
    """``SCH_SCREEN::GetSheets``: x, then y, then the KIID."""
    if a.at[0] != b.at[0]:
        return -1 if a.at[0] < b.at[0] else 1
    if a.at[1] != b.at[1]:
        return -1 if a.at[1] < b.at[1] else 1
    less = kiid_less(a.kiid, b.kiid)
    if less is None:
        return 0
    return -1 if less else (1 if kiid_less(b.kiid, a.kiid) else 0)


@dataclass
class _Walk:
    """The state of the walk of one top-level sheet (BuildSheetList)."""

    paths: list[SheetPath]
    problems: list[str]
    top: int
    count: int = 0


def _walk(
    walk: _Walk,
    screen: Screen | None,
    kiids: tuple[Kiid, ...],
    uuids: tuple[str | None, ...],
    names: tuple[str, ...],
    item: SheetItem | None,
    parent_file: str,
) -> None:
    """Add a sheet path and the paths below it, as ``SCH_SHEET_LIST::BuildSheetList``."""
    walk.count += 1
    walk.paths.append(SheetPath(screen, kiids, uuids, names, item, walk.top, walk.count))
    if screen is None:
        return
    children = sorted(screen.sheets, key=cmp_to_key(_sheet_order))
    for a, b in zip(children, children[1:], strict=False):
        # Two items of one sheet: the two rule sets of a tolerant load.
        if a.index != b.index and _sheet_order(a, b) == 0:
            walk.problems.append(
                f"The sheets {a.name!r} and {b.name!r} of {screen.key} have the same "
                "position and a UUID that KiCad makes at random: their order in the "
                "sheet list is not known."
            )
    for child in children:
        if child.raw_file == parent_file:
            continue  # A child with the file name of its parent.
        if isinstance(child.kiid, RandomKiid):
            walk.problems.append(
                f"The sheet {child.name!r} of {screen.key} has no valid UUID: KiCad gives "
                "it a new UUID at each load, thus no instance in the files matches its "
                "sheet paths."
            )
        _walk(
            walk,
            child.screen,
            (*kiids, child.kiid),
            (*uuids, child.uuid),
            (*names, child.name),
            child,
            child.raw_file,
        )


def build_sheet_list(
    tops: list[TopLevelSheet],
    project_dir: Path,
    variables: dict[str, str],
    kicad: int | None,
    tolerant: bool = False,
    nil_top_uuid: bool = False,
) -> SheetList:
    """Load the top-level sheets and give the sheet list, as KiCad does.

    Args:
        kicad: The major version of the KiCad that loads the files (parsed_kiids).
            None: not known, the KiCad of the version of each file.
        tolerant: Do not stop at a file that the server cannot read (see the module).
            A top-level sheet that the system cannot open gives no sheet paths.
        nil_top_uuid: A top-level sheet with no UUID gets NIL_UUID (KiCad gives it a
            new UUID at each load). Use such paths for the netlist only, not for the
            instances of the symbols.

    Raises:
        InputError: KiCad cannot load a top-level sheet.
        KicadParseError: The parser of KiCad stops in a sub-sheet: KiCad then loads
            only a part of it, which the server does not model.
        KicadFileError: A file has a version that the server does not know, or
            KiCad 8 or 9 cannot read a sheet field of a file.
        SheetFileError: KiCad opens a sheet file relative to its working directory.
    """
    # The screens by the text of their file name (SearchHierarchy compares the texts).
    screens: dict[str, Screen] = {}
    # The files that give an empty sheet, with the reason: one load for each file.
    empty: dict[str, str] = {}
    pages: dict[object, str] = {}
    problems: list[str] = []

    def read(name: KicadFileName) -> tuple[Screen, str | None]:
        if not tolerant:
            return _read_screen(name, kicad)
        screen, root_page, notes = _read_tolerant(name, kicad)
        problems.extend(notes)
        return screen, root_page

    def load(name: KicadFileName) -> tuple[Screen | None, str]:
        """Return the screen of a file, or None and the reason of an empty sheet
        (EMPTY_REASONS). The messages give the file name of KiCad (name.text): Path
        removes "/./"."""
        key = name.text
        try:
            screen, _page = read(name)
        except FileNotFoundError:
            return None, "not there"  # KiCad shows an empty sheet.
        except OSError as e:
            # KiCad cannot open the file: nothing of it.
            problems.append(f"{key} cannot be read: {e}. KiCad shows an empty sheet.")
            return None, "cannot be read"
        except (KiCadEncodingError, SExprError, KicadParseError) as e:
            # A parse error: KiCad keeps the items of the file before the error, and
            # loads the other sheets (measured with kicad-cli 8.0.9, 9.0.9, 10.0.6 and
            # 10.0.7: unbalanced parentheses, a byte that is not UTF-8, a sheet with no
            # name, a sub-symbol name). The server does not model the part of a file
            # (user decision, 2026-11-07).
            reason = str(e).strip()
            if key not in reason:
                reason = f"{key}: {reason}"
            if not reason.endswith("."):
                reason += "."
            raise KicadParseError(
                f"The parser of KiCad stops in a sub-sheet: {reason} KiCad then keeps "
                "only the items of the file before the error, and loads the other "
                "sheets. The server does not model this: repair the file."
            ) from e
        except KicadFileError as e:
            raise with_file(e, key) from e
        screens[key] = screen
        return screen, ""

    def load_children(screen: Screen, ancestors: list[str]) -> None:
        for item in screen.sheets:
            if item.loaded:
                continue
            item.loaded = True
            expanded = expand_text_variables(item.raw_file, variables)
            if not expanded:
                item.empty = "empty expansion" if item.raw_file else "no file name"
                continue
            try:
                child = sheet_file_name(expanded, screen.name)
            except SheetFileError as e:
                if not tolerant:
                    raise with_file(e, screen.key) from e
                item.empty = "working directory"
                problems.append(f"{screen.key}: {e} KiCad shows an empty sheet or the file.")
                continue
            item.file = child.text
            if uses_home(expanded, WINDOWS):
                problems.append(
                    f"The sheet {item.name!r} of {screen.key} has the file {item.raw_file!r}: "
                    "KiCad replaces its first directory with the home directory of the "
                    f"user of the KiCad process. The server uses its own: {child.text}. "
                    "When KiCad runs with an other home directory, it loads an other file."
                )
            if child.text in ancestors:
                item.empty = "ancestor"
                problems.append(
                    f"The sheet {item.name!r} of {screen.key} holds its own ancestor "
                    f"{child.text}: KiCad shows it as empty."
                )
                continue
            if child.text in screens:
                item.screen = screens[child.text]
                continue
            if child.text in empty:
                item.empty = empty[child.text]
                continue
            item.screen, item.empty = load(child)
            if item.screen is None:
                empty[child.text] = item.empty
            else:
                load_children(item.screen, [*ancestors, child.text])

    top_screens: list[Screen | None] = []
    # The file name of the root sheet of each top-level sheet (SCH_SHEET::GetFileName):
    # relative to the project, in the notation of Unix (SCH_SHEET::SetFileName).
    relatives = [
        os.path.relpath(os.path.normpath(top.file), project_dir).replace("\\", "/") for top in tops
    ]
    for index, top in enumerate(tops):
        if not top.file.is_file():
            # KiCad loads only a top-level sheet whose file is there (FileExists of
            # OpenProjectFiles and LoadSchematic, before the expansion of its name).
            if not tolerant:
                raise InputError(f"KiCad cannot load the top-level sheet {top.file}: no such file.")
            top_screens.append(None)
            continue
        try:
            name = top_level_file(top.file, project_dir, variables)
        except SheetFileError as e:
            if not tolerant:
                raise with_file(e, top.file) from e
            problems.append(f"{top.file}: {e}")
            top_screens.append(None)
            continue
        if uses_home(expand_text_variables(relatives[index], variables), WINDOWS):
            problems.append(
                f"The top-level sheet {top.file}: KiCad replaces the first directory of its "
                "name with the home directory of the user of the KiCad process. The server "
                f"uses its own: {name.text}. When KiCad runs with an other home directory, it "
                "loads an other file."
            )
        if name.text in screens:
            # The loader finds the screen of an other top-level sheet, and does not
            # read the file again (loadHierarchy, m_loadedRootSheets).
            top_screens.append(screens[name.text])
            continue
        try:
            screen, root_page = read(name)
        except FileNotFoundError as e:
            if not tolerant:
                raise InputError(f"KiCad cannot load the top-level sheet {name.text}: {e}") from e
            top_screens.append(None)
            continue
        except OSError as e:
            if not tolerant:
                raise InputError(f"KiCad cannot load the top-level sheet {name.text}: {e}") from e
            problems.append(f"{name.text} cannot be read: {e}")
            top_screens.append(None)
            continue
        except (KiCadEncodingError, SExprError, KicadParseError) as e:
            raise InputError(f"KiCad cannot load the top-level sheet {name.text}: {e}") from e
        except KicadFileError as e:
            raise with_file(e, name.text) from e
        if screen.stopped:
            # A parse error of a top-level sheet: KiCad does not load the schematic
            # (loadHierarchy gives the error of the root to the caller).
            top_screens.append(None)
            continue
        screens[name.text] = screen
        top_screens.append(screen)
        if root_page is not None:
            pages[("top", index)] = root_page
        load_children(screen, [name.text])

    paths: list[SheetPath] = []
    # SCH_SHEET_PATH::PathHumanReadable puts the name of the top-level sheet into the
    # path when KiCad loaded more than one top-level sheet (GetTopLevelSheets).
    several = sum(screen is not None for screen in top_screens) > 1
    for index, top in enumerate(tops):
        top_screen = top_screens[index]
        if top_screen is None:
            continue
        # The UUID of the entry of the project, unless it is the nil UUID: then the
        # UUID of the file (OpenProjectFiles, LoadSchematic).
        entry_kiid = kiid(top.uuid) if top.uuid is not None else None
        if entry_kiid is None or entry_kiid == kiid(NIL_UUID):
            top_kiid, top_uuid = file_kiid(top_screen)
            where = f"{top_screen.key} has no UUID"
        else:
            top_kiid = entry_kiid
            # KIID::AsString: the UUID as KiCad writes it in the instances.
            top_uuid = kiid_text(entry_kiid)
            where = (
                f"The entry {top.name!r} ({top.file.name}) of top_level_sheets has no valid UUID"
            )
        if top_uuid is None and nil_top_uuid:
            top_kiid, top_uuid = kiid(NIL_UUID), NIL_UUID
        elif top_uuid is None:
            problems.append(
                f"{where}: KiCad gives it a new UUID at each load, thus no instance in "
                "the files matches its sheet paths."
            )
        top_name = (top.name,) if several else ()
        relative = relatives[index]
        _walk(
            _Walk(paths, problems, index),
            top_screen,
            (top_kiid,),
            (top_uuid,),
            top_name,
            None,
            relative,
        )

    sheet_list = SheetList(paths, screens, pages, problems)
    for path in paths:
        if path.item is not None:
            page = next((p for key, p in path.item.pages if key == path.parent), None)
            if page is not None:
                sheet_list.set_page(path, page)
    return sheet_list


def sort_by_page_numbers(
    paths: list[SheetPath],
    page: Callable[[SheetPath], str],
    rank: Callable[[SheetPath], int],
) -> list[SheetPath]:
    """Sort the paths as ``SCH_SHEET_LIST::SortByPageNumbers``.

    Args:
        page: The page of a path.
        rank: The order that the tool uses where KiCad gives no known order: in
            place of the hash of a path, and for two texts of one integer page
            (see unknown_page_order).
    """

    def compare(a: SheetPath, b: SheetPath) -> int:
        page_a, page_b = page(a), page(b)
        result = compare_page_num(page_a, page_b)
        if result is None:
            rank_a, rank_b = rank(a), rank(b)
            if rank_a != rank_b:
                return -1 if rank_a < rank_b else 1
            return -1 if page_a < page_b else 1
        if result:
            return result
        if a.dfs != b.dfs:
            return -1 if a.dfs < b.dfs else 1
        rank_a, rank_b = rank(a), rank(b)
        return (rank_a > rank_b) - (rank_a < rank_b)

    return sorted(paths, key=cmp_to_key(compare))


def unknown_page_order(
    paths: list[SheetPath], page: Callable[[SheetPath], str]
) -> list[list[SheetPath]]:
    """Return the groups of paths with different texts of one integer page ("1" and
    "01"). ``ComparePageNum`` gives "greater" in the two directions, thus the
    order of ``std::sort`` is not known."""
    by_value: dict[int, list[SheetPath]] = {}
    for path in paths:
        value = to_long(page(path))
        if value is not None:
            by_value.setdefault(value, []).append(path)
    return [group for group in by_value.values() if len({page(path) for path in group}) > 1]
