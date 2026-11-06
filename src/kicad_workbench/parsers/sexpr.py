"""S-expression helpers for KiCad files.

The rules come from the KiCad source: ``OUTPUTFORMATTER::Quotes`` in
``common/richio.cpp`` writes a string, and ``DSNLEXER`` in
``common/dsnlexer.cpp`` reads it. This module is an own implementation of
these rules. It does not use code of KiCad or of other S-expression
libraries.

``parse()`` gives a tree of ``Node`` (a list) and ``Atom`` (a token) items.
Each item has its offsets in the source text, thus a later write can change
only one part of a file. A list reads its items at the first use, thus a
large board needs little memory.
"""

import math
import re
import sys
from array import array
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass

from ..errors import InputError

# KiCad escapes these characters in a quoted string. The lexer reads the file
# line by line, thus a literal line break in a string stops the load with
# "Unterminated delimited string".
_ESCAPES = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r"})

# Regex pattern of one quoted string token. A backslash escapes the character
# that follows it, thus '\"' does not end the token.
STRING = r'"(?:[^"\\]|\\.)*"'

# The escape sequences of the lexer: a hex value with 1 or 2 digits, an octal
# value with 1 to 3 digits, or one character.
_ESCAPE_RE = re.compile(rb"\\(x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|.)", re.DOTALL)

_SIMPLE_ESCAPES = {
    b'"': b'"',
    b"\\": b"\\",
    b"a": b"\x07",
    b"b": b"\x08",
    b"f": b"\x0c",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\x0b",
    b"x": b"x",
}


def quote(value: str) -> str:
    """Return ``value`` as a quoted S-expression string, as KiCad writes it."""
    return '"' + value.translate(_ESCAPES) + '"'


def _unescape(match: re.Match[bytes]) -> bytes:
    seq = match.group(1)
    if seq in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[seq]
    if seq.startswith(b"x"):
        return bytes([int(seq[1:], 16)])
    if seq[0] in b"01234567":
        # The lexer keeps the low byte of a value larger than 0o377.
        return bytes([int(seq, 8) & 0xFF])
    # The lexer reads an unknown escape as a backslash and the character.
    return b"\\" + seq


def unquote(token: str) -> str:
    """Return the value of a quoted string token that matches ``STRING``.

    The escapes give bytes, as in the lexer. The result is UTF-8. A hex or octal
    escape can give a byte that is not valid UTF-8. That byte becomes a lone
    surrogate (``surrogateescape``), thus the value stays different from each
    valid string and the read does not stop.
    """
    raw = _ESCAPE_RE.sub(_unescape, token[1:-1].encode("utf-8"))
    return raw.decode("utf-8", errors="surrogateescape")


# The lexer of KiCad reads the file line by line, and a line ends at LF.
# Spaces are " ", tab, CR, LF and NUL. A line whose first character that is
# not a blank is "#" is a comment. A string starts with '"' only at the start
# of a token and cannot hold a line break. A bare token ends only at a space
# or a parenthesis, thus it can hold a '"'.
_SPACE = " \t\r\n\0"
_COMMENT = r"(?m:^[ \t\0]*#[^\n]*)"
_STRING_TOKEN = r'"(?:[^"\\\n]|\\[^\n])*"'
_BARE_TOKEN = rf"[^{_SPACE}()\"][^{_SPACE}()]*"

# The first pass finds the parentheses. It skips comments and strings, and the
# rare bare tokens that hold a '"'. A '"' that starts no complete string
# stops the parse.
_STRUCTURE_RE = re.compile(
    rf"{_COMMENT}"
    rf"|{_STRING_TOKEN}"
    rf"|(?P<open>\()|(?P<close>\))"
    rf'|(?<![^{_SPACE}()])[^{_SPACE}()"][^{_SPACE}()"]*"[^{_SPACE}()]*'
    rf'|(?P<bad>")'
)
# The items of one list.
_ITEM_RE = re.compile(
    rf"(?P<space>[{_SPACE}]+)|(?P<open>\()|(?P<token>{_STRING_TOKEN}|{_BARE_TOKEN})"
)
# Blanks and comment lines. A comment is tried first at the start of a line,
# before the blanks that come before its "#".
_SKIP = rf"(?:{_COMMENT}|[{_SPACE}])*"
_HEAD_RE = re.compile(rf"{_SKIP}({_BARE_TOKEN})")
_OUTSIDE_RE = re.compile(_SKIP)
# A number that all KiCad versions read in the same way. DSNLEXER::parseDouble
# of KiCad 10 uses fast_float::from_chars, KiCad 8 and 9 use std::from_chars
# or strtod (by compiler). Each one reads only the start of the token, and
# they do not agree on "+1", "1_0", "0x1p3", "inf" or " 1". Thus this server
# reads only the decimal form that they agree on.
_NUMBER = re.compile(r"(?P<mantissa>-?(?:\d+\.?\d*|\.\d+))(?:[eE][+-]?\d+)?")
# The start of a file: "(<head> (version <value>)".
_FILE_HEADER_RE = re.compile(
    rf"{_SKIP}\({_SKIP}(?P<head>{_BARE_TOKEN}){_SKIP}\({_SKIP}version(?=[{_SPACE}()])"
    rf"{_SKIP}(?P<value>{_STRING_TOKEN}|{_BARE_TOKEN})?{_SKIP}\)"
)


def file_header(text: str) -> tuple[str, str | None] | None:
    """Return the head of the top-level list and the value of its first item (version ...).

    Only the start of the text is read, thus a large file needs no parse.

    Returns:
        (head, value), with value None for "(version)". None when the first
        item of the top-level list is not a (version ...) list.
    """
    match = _FILE_HEADER_RE.match(text)
    if not match:
        return None
    value = match.group("value")
    return match.group("head"), unquote(value) if value and value.startswith('"') else value


class SExprError(InputError):
    """The text is not a valid KiCad S-expression."""

    def __init__(self, message: str, offset: int):
        super().__init__(f"{message} at offset {offset}")
        self.offset = offset


@dataclass(frozen=True, slots=True)
class Atom:
    """A token that is not a parenthesis: a quoted string or a bare token.

    ``text`` is the token as it is in the source text, and ``start`` and
    ``end`` are its offsets there.
    """

    text: str
    start: int
    end: int

    @property
    def quoted(self) -> bool:
        return self.text.startswith('"')

    @property
    def value(self) -> str:
        """The text of the token, with the quotes and escapes removed."""
        return unquote(self.text) if self.quoted else self.text


class _Document:
    """The source text and the offsets of its parentheses.

    ``opens[i]`` is the offset of the "(" of list i, in the order of the
    text, and ``closes[i]`` the offset of its ")". A list holds the lists
    i + 1 to j - 1, where j is the first list after its ")".
    """

    __slots__ = ("text", "opens", "closes")

    def __init__(self, text: str, opens: array, closes: array):
        self.text = text
        self.opens = opens
        self.closes = closes

    def after(self, index: int) -> int:
        """Return the index of the first list after the ")" of list index."""
        return bisect_right(self.opens, self.closes[index], lo=index + 1)


class Node:
    """A list ``( ... )`` of a parsed text.

    ``start`` is the offset of "(", ``end`` the offset after ")". The items
    are read from the text at the first use, thus a large file needs memory
    only for the lists that a caller reads.
    """

    __slots__ = ("_doc", "_index", "_items", "_head", "_atoms")

    def __init__(self, doc: _Document, index: int):
        self._doc = doc
        self._index = index
        self._items: list[Node | Atom] | None = None
        self._head: str | None | bool = False
        self._atoms: list[Atom] | None = None

    @property
    def source(self) -> str:
        """The parsed text that holds the list."""
        return self._doc.text

    @property
    def start(self) -> int:
        return self._doc.opens[self._index]

    @property
    def end(self) -> int:
        return self._doc.closes[self._index] + 1

    @property
    def head(self) -> str | None:
        """The first item when it is a bare token (for example "footprint")."""
        if self._head is False:
            match = _HEAD_RE.match(self._doc.text, self.start + 1, self.end - 1)
            self._head = match.group(1) if match else None
        return self._head

    @property
    def items(self) -> list["Node | Atom"]:
        """The tokens and the lists in this list, in the order of the text."""
        if self._items is None:
            self._items = self._read_items()
        return self._items

    def _read_items(self) -> list["Node | Atom"]:
        doc = self._doc
        text = doc.text
        items: list[Node | Atom] = []
        pos = self.start + 1
        end = self.end - 1
        child = self._index + 1
        while pos < end:
            match = _ITEM_RE.match(text, pos, end)
            kind = match.lastgroup
            if kind == "space":
                pos = match.end()
            elif kind == "open":
                items.append(Node(doc, child))
                pos = doc.closes[child] + 1
                child = doc.after(child)
            elif text[pos] == "#" and _starts_line(text, pos):
                pos = _line_end(text, pos)
            else:
                items.append(Atom(match.group(), pos, match.end()))
                pos = match.end()
        return items

    def children(self, head: str | None = None) -> list["Node"]:
        """Return the lists in this list, or only those with the head."""
        return [
            item
            for item in self.items
            if isinstance(item, Node) and (head is None or item.head == head)
        ]

    def first(self, head: str) -> "Node | None":
        """Return the first list in this list with the head, or None."""
        for item in self.items:
            if isinstance(item, Node) and item.head == head:
                return item
        return None

    def atoms(self) -> list[Atom]:
        """Return the tokens in this list after the head."""
        if self._atoms is None:
            items = self.items[1:] if self.head is not None else self.items
            self._atoms = [item for item in items if isinstance(item, Atom)]
        return self._atoms

    def value(self, index: int = 0, default: str | None = None) -> str | None:
        """Return the value of a token after the head, or the default."""
        atoms = self.atoms()
        return atoms[index].value if index < len(atoms) else default

    def number(self, index: int = 0, default: float | None = None) -> float | None:
        """Return a token after the head as a number, or the default.

        Raises InputError when the token is not a number that all KiCad versions
        read as the same number (_NUMBER).
        """
        text = self.value(index)
        if text is None:
            return default
        match = _NUMBER.fullmatch(text)
        value = float(text) if match else math.nan
        # Out of the range of a double: strtod or from_chars gives an error, or
        # a value that is not the same in each build.
        if not math.isfinite(value) or (
            abs(value) < sys.float_info.min and match.group("mantissa").strip("-0.")
        ):
            raise InputError(f"{text!r} is not a number that KiCad reads")
        return value

    def walk(self, head: str | None = None) -> Iterator["Node"]:
        """Yield this list and all lists in it, in the order of the text, or
        only those with the head."""
        doc = self._doc
        for index in range(self._index, doc.after(self._index)):
            node = Node(doc, index)
            if head is None or node.head == head:
                yield node


def _starts_line(text: str, offset: int) -> bool:
    """Return True if only blanks are between the start of the line and offset."""
    line_start = text.rfind("\n", 0, offset) + 1
    return text[line_start:offset].strip(" \t\0") == ""


def _line_end(text: str, offset: int) -> int:
    end = text.find("\n", offset)
    return len(text) if end < 0 else end


def parse(text: str) -> Node:
    """Parse the text of a KiCad file and return its one top-level list.

    Raises:
        SExprError: The parentheses are not balanced, a string has no end
            on its line, the text holds no list or more than one top-level
            list, or a token is outside the top-level list.
    """
    opens = array("q")
    closes = array("q")
    stack: list[int] = []
    for match in _STRUCTURE_RE.finditer(text):
        kind = match.lastgroup
        if kind == "open":
            if not stack and opens:
                raise SExprError("a second top-level list", match.start())
            stack.append(len(opens))
            opens.append(match.start())
            closes.append(-1)
        elif kind == "close":
            if not stack:
                raise SExprError("')' with no '('", match.start())
            closes[stack.pop()] = match.start()
        elif kind == "bad":
            raise SExprError("a string with no end on its line", match.start())
    if stack:
        raise SExprError("'(' with no ')'", opens[stack[-1]])
    if not opens:
        raise SExprError("no list", len(text))
    for start, end in ((0, opens[0]), (closes[0] + 1, len(text))):
        outside = _OUTSIDE_RE.match(text, start, end)
        if outside.end() != end:
            raise SExprError("a token outside the top-level list", outside.end())
    return Node(_Document(text, opens, closes), 0)


def edited(node: Node, edits: list[tuple[int, int, str]]) -> str:
    """Return the text of a list with edits.

    Each edit is (start, end, text), with offsets of the parsed text, inside
    the list. Two insertions at one offset are in the text in the order of
    the edit list.

    Raises:
        ValueError: Two edits overlap: a replaced range holds the start of an
            other edit, or an insertion is at the start of a replaced range.
            The result would then depend on the order of the edits.
    """
    by_start = sorted(edits, key=lambda e: (e[0], e[1]))
    for (start_a, end_a, _), (start_b, end_b, _) in zip(by_start, by_start[1:], strict=False):
        inside = start_b < end_a
        at_start = start_a == end_a == start_b and end_b > start_b
        if inside or at_start:
            raise ValueError(
                f"Two edits of the text overlap: {start_a}-{end_a} and {start_b}-{end_b}."
            )
    text = node.source[node.start : node.end]
    order = sorted(range(len(edits)), key=lambda i: (edits[i][0], i), reverse=True)
    for start, end, replacement in (edits[i] for i in order):
        text = text[: start - node.start] + replacement + text[end - node.start :]
    return text


def indent(text: str, depth: int) -> str:
    """Indent the lines of the text of a list with tabs, as KiCad writes a file.

    A line gets one tab for each list that holds its start, plus ``depth``
    tabs. A line that starts with ")" gets the indent of the list that it
    closes. Blank lines are removed. Blanks have no meaning between the tokens
    and a string cannot hold a line break, thus KiCad reads the text in the
    same way.
    """
    lines = []
    level = 0
    for line in text.split("\n"):
        stripped = line.strip(_SPACE)
        if not stripped:
            continue
        opens = closes = 0
        for match in _STRUCTURE_RE.finditer(stripped):
            if match.lastgroup == "open":
                opens += 1
            elif match.lastgroup == "close":
                closes += 1
        leading = len(stripped) - len(stripped.lstrip(")"))
        lines.append("\t" * (depth + max(level - leading, 0)) + stripped)
        level += opens - closes
    return "\n".join(lines)


def property_atoms(prop: Node) -> list[Atom]:
    """Return the tokens of a (property ...) from its name.

    KiCad 9 and later write the bare token "private" before the name of a
    private field: (property private "Name" "value").
    """
    atoms = prop.atoms()
    if atoms and not atoms[0].quoted and atoms[0].text == "private":
        return atoms[1:]
    return atoms


def property_field(prop: Node) -> tuple[str, str] | None:
    """Return (name, value) of a (property ...), or None when it has no name."""
    atoms = property_atoms(prop)
    if not atoms:
        return None
    return atoms[0].value, atoms[1].value if len(atoms) > 1 else ""
