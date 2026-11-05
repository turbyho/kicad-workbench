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

from ..utils.kicad_lock import FileStamp, file_stamp
from ..utils.kicad_text import KiCadEncodingError, read_as_kicad
from ..utils.sch_hierarchy import TopLevelSheet, expand_text_variables
from .kicad_fields import FieldRules, rules_for_file, sheet_fields
from .kicad_versions import schematic_version
from .sch_geometry import Point, definitions, node_point
from .sexpr import Node, SExprError, parse

_UUID_RE = re.compile(
    r"\A(\{?)([0-9a-fA-F]{8})(-?)([0-9a-fA-F]{4})\3([0-9a-fA-F]{4})\3"
    r"([0-9a-fA-F]{4})\3([0-9a-fA-F]{12})(\}?)\Z"
)
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_PAGE_SPACES = str.maketrans("", "", "\r\n\t ")
_LONG_MIN, _LONG_MAX = -(2**63), 2**63 - 1


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


def to_long(text: str) -> int | None:
    """Return the integer of a text as ``wxString::ToLong`` reads it (base 10, a
    64-bit long), or None.

    ``wcstol`` skips the white space at the start, reads a sign and digits,
    and the full text must be the number.
    """
    match = re.fullmatch(r"[ \t\n\v\f\r]*([+-]?[0-9]+)", text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if _LONG_MIN <= value <= _LONG_MAX else None


def to_int(value: int) -> int:
    """Return a long as C++ casts it to a 32-bit int."""
    return (value + 2**31) % 2**32 - 2**31


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

    file: Path
    root: Node
    stamp: FileStamp  # before the read (kicad_lock.file_stamp)
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


@dataclass(eq=False)
class SheetItem:
    """A sheet symbol of a screen."""

    node: Node
    kiid: Kiid
    uuid: str | None
    name: str
    raw_file: str  # the Sheetfile field, as the file holds it
    at: Point
    # The pages of its last (instances ...): (the path of the parent, the page).
    pages: list[tuple[tuple[Kiid, ...], str]]
    screen: Screen | None = None  # None: an empty screen
    loaded: bool = False  # the loader gave it a screen


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
    screens: dict[Path, Screen]
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


def item_kiid(screen: Screen, item: Node) -> tuple[Kiid, str | None]:
    """Return the KIID of an item after parseKIID, and the text of its last (uuid ...).
    An item with no (uuid ...) gets a random KIID."""
    nodes = item.children("uuid")
    if not nodes:
        return RandomKiid(None), None
    node = nodes[-1]
    return screen.kiids.get(node.start, kiid(node.value(0, ""))), node.value(0)


def _sheet_item(screen: Screen, sheet: Node, rules: FieldRules) -> SheetItem:
    """Return a sheet symbol of a file.

    Raises:
        KicadFileError: KiCad cannot read the sheet fields.
    """
    fields = sheet_fields(sheet, rules)
    sheet_kiid, uuid = item_kiid(screen, sheet)
    at = sheet.first("at")
    pages: list[tuple[tuple[Kiid, ...], str]] = []
    for instances in sheet.children("instances"):
        # KiCad keeps the instances of the last (instances ...) (setInstances).
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
    return SheetItem(
        sheet,
        sheet_kiid,
        uuid,
        fields["Sheetname"],
        fields["Sheetfile"],
        node_point(at) if at is not None else (0, 0),
        pages,
    )


def _read_screen(file: Path, kicad: int) -> tuple[Screen, str | None]:
    """Read a schematic file as the parser does. Return the screen and the page of
    its (sheet_instances (path "/" ...)), or None.

    Raises:
        OSError, KiCadEncodingError, SExprError: The file cannot be read.
        KicadFileError: The file has a version that the server does not know,
            or KiCad cannot read a sheet field.
    """
    stamp = file_stamp(file)
    root = parse(read_as_kicad(file))
    rules = rules_for_file(root)
    screen = Screen(file, root, stamp, schematic_version(root), rules, definitions(root))
    screen.kiids = parsed_kiids(root, kicad)
    screen.sheets = [_sheet_item(screen, sheet, rules) for sheet in root.children("sheet")]
    root_page = None
    for instances in root.children("sheet_instances"):
        for path in instances.children("path"):
            page_node = path.first("page")
            page = page_text(page_node.value(0, "")) if page_node is not None else ""
            key = kiid_path(path.value(0, ""))
            if key:
                screen.sheet_instances.append((key, page))
            else:
                root_page = page
    return screen, root_page


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
        if _sheet_order(a, b) == 0:
            walk.problems.append(
                f"The sheets {a.name!r} and {b.name!r} of {screen.file} have the same "
                "position and a UUID that KiCad makes at random: their order in the "
                "sheet list is not known."
            )
    for child in children:
        if child.raw_file == parent_file:
            continue  # A child with the file name of its parent.
        if isinstance(child.kiid, RandomKiid):
            walk.problems.append(
                f"The sheet {child.name!r} of {screen.file} has no valid UUID: KiCad gives "
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
    tops: list[TopLevelSheet], project_dir: Path, variables: dict[str, str], kicad: int
) -> SheetList:
    """Load the top-level sheets and give the sheet list, as KiCad does.

    Args:
        kicad: The major version of the KiCad that loads the files (parsed_kiids).

    Raises:
        KicadFileError: A file has a version that the server does not know, or
            KiCad cannot read a sheet field of a file.
    """
    screens: dict[Path, Screen] = {}
    pages: dict[object, str] = {}
    problems: list[str] = []

    def load(file: Path) -> Screen | None:
        try:
            screen, _root_page = _read_screen(file, kicad)
        except FileNotFoundError:
            return None  # KiCad shows an empty sheet.
        except (OSError, KiCadEncodingError, SExprError) as e:
            problems.append(f"{file} cannot be read: {e}. KiCad shows an empty sheet.")
            return None
        screens[file] = screen
        return screen

    def load_children(screen: Screen, ancestors: list[str]) -> None:
        for item in screen.sheets:
            if item.loaded:
                continue
            item.loaded = True
            name = expand_text_variables(item.raw_file, variables)
            child = Path(os.path.normpath(Path(screen.file.parent) / name)) if name else None
            if child is None or str(child) in ancestors:
                if child is not None:
                    problems.append(
                        f"The sheet {item.name!r} of {screen.file} holds its own ancestor "
                        f"{child}: KiCad shows it as empty."
                    )
                continue
            if child in screens:
                item.screen = screens[child]
                continue
            item.screen = load(child)
            if item.screen is not None:
                load_children(item.screen, [*ancestors, str(child)])

    top_screens: list[Screen] = []
    for index, top in enumerate(tops):
        file = Path(os.path.normpath(top.file))
        if file in screens:
            # The loader finds the screen of an other top-level sheet, and does not
            # read the file again (loadHierarchy, m_loadedRootSheets).
            top_screens.append(screens[file])
            continue
        try:
            screen, root_page = _read_screen(file, kicad)
        except (OSError, KiCadEncodingError, SExprError) as e:
            raise ValueError(f"KiCad cannot load the top-level sheet {file}: {e}") from e
        screens[file] = screen
        top_screens.append(screen)
        if root_page is not None:
            pages[("top", index)] = root_page
        load_children(screen, [str(file)])

    paths: list[SheetPath] = []
    for index, top in enumerate(tops):
        screen = top_screens[index]
        if top.uuid is not None:
            top_kiid, top_uuid = kiid(top.uuid), top.uuid
        else:
            top_kiid, top_uuid = item_kiid(screen, screen.root)
        if top_uuid is None:
            problems.append(
                f"{top.file} has no UUID: KiCad gives it a new UUID at each load, "
                "thus no instance in the files matches its sheet paths."
            )
        top_name = () if len(tops) == 1 else (top.name,)

        # The loader gives a top-level sheet its file name relative to the project.
        relative = os.path.relpath(os.path.normpath(top.file), project_dir)
        _walk(
            _Walk(paths, problems, index),
            screen,
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
