"""The filters of the footprint list of CvPcb, and the footprint IDs of KiCad 10.0.6
(checked in the source):

- FOOTPRINT_FILTER::FilterByFootprintFilters makes an anchored wildcard of each
  footprint filter of the symbol (EDA_PATTERN_MATCH_WILDCARD_ANCHORED, the pattern in
  lower case: wxString::Lower). "?" is one character, "*" is any text, each other
  character is literal (SetPattern puts a backslash before each character of
  ".*+?^${}()|[]/\\"). The pattern must match the full text ("^...$").
- FOOTPRINT_FILTER_IT::FootprintFilterMatch: the text is the footprint name in lower
  case; when the pattern has a ":", the text is "<nickname>:<name>" in lower case. One
  filter that matches is sufficient. With no filters, each footprint matches.
- LIB_ID::Parse: the nickname is the text before the first ":", the name is the text
  after it. LIB_ID::HasIllegalChars refuses ":", tab, CR, LF, "\\", "<", ">" and '"' in
  the name (LIB_ID::isLegalChar).
"""

import re

from ..parsers.annotation import wx_lower

# LIB_ID::isLegalChar of a library item name.
_ILLEGAL_NAME_CHARS = ':\t\n\r\\<>"'


def _wildcard(pattern: str) -> re.Pattern[str]:
    """The regular expression of EDA_PATTERN_MATCH_WILDCARD_ANCHORED::SetPattern."""
    parts = []
    for char in pattern:
        if char == "?":
            parts.append(".")
        elif char == "*":
            parts.append(".*")
        else:
            parts.append(re.escape(char))
    # wxRE_ADVANCED: "." matches each character, also a line end.
    return re.compile("".join(parts), re.DOTALL)


def filter_matches(filters: tuple[str, ...] | list[str], nickname: str, name: str) -> bool:
    """FOOTPRINT_FILTER_IT::FootprintFilterMatch for the footprint nickname:name."""
    if not filters:
        return True
    for each in filters:
        pattern = wx_lower(each)
        text = wx_lower(name)
        if ":" in pattern:
            text = wx_lower(nickname) + ":" + text
        if _wildcard(pattern).fullmatch(text):
            return True
    return False


def parse_lib_id(text: str) -> tuple[str, str]:
    """LIB_ID::Parse with no fix: (the nickname, the item name). The nickname is the text
    before the first ":" (none: empty). An item name with a character that
    LIB_ID::isLegalChar refuses gives an empty item name (Parse returns before
    SetLibItemName). LIB_ID::operator== compares the two parts."""
    nickname, colon, name = text.partition(":")
    if not colon:
        nickname, name = "", text
    if any(char in _ILLEGAL_NAME_CHARS for char in name):
        name = ""
    return nickname, name


def footprint_id_problem(footprint: str) -> str | None:
    """The error of a footprint ID that KiCad cannot use (LIB_ID::Parse), or None."""
    nickname, colon, name = footprint.partition(":")
    if not colon or not nickname:
        return (
            f"The footprint {footprint!r} has no library nickname: give "
            '"<library>:<footprint>", for example "Resistor_SMD:R_0603_1608Metric".'
        )
    if not name:
        return f"The footprint {footprint!r} has no footprint name after the library nickname."
    for char in name:
        if char in _ILLEGAL_NAME_CHARS:
            return (
                f"The footprint name {name!r} has the character {char!r}: KiCad cannot read "
                "such a library ID (LIB_ID::HasIllegalChars)."
            )
    return None
