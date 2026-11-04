"""The bus name test of KiCad 10.0.6 (SCH_CONNECTION::IsBusLabel).

A label text is a bus name when it is a bus vector ("D[0..7]") or a bus group
("BUS{A B}") after UnescapeString (``common/string_utils.cpp:313``,
``common/project/net_settings.cpp:1186`` and ``:1394``). The functions follow
the code of KiCad step by step. A Python str index is one Unicode code point,
as a wxString index on Linux.
"""

_ESCAPES = {
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
_DIGITS = "0123456789"
# wxString::ToLong gives LONG_MAX for a number that is too large (64-bit Linux).
_LONG_MAX = 2**63 - 1


def unescape_string(text: str) -> str:
    """UnescapeString: decode the brace escapes ("{slash}") of a KiCad text."""
    if len(text) <= 2:
        return text
    out = []
    ch = "\0"
    index = 0
    while index < len(text):
        prev = ch
        ch = text[index]
        if ch == "{":
            token = []
            depth = 1
            terminated = False
            index += 1
            while index < len(text):
                ch = text[index]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                if depth <= 0:
                    terminated = True
                    break
                token.append(ch)
                index += 1
            inner = "".join(token)
            if not terminated:
                out.append("{" + unescape_string(inner))
            elif prev in ("$", "~", "^", "_"):
                out.append("{" + unescape_string(inner) + "}")
            elif inner in _ESCAPES:
                out.append(_ESCAPES[inner])
            else:
                out.append("{" + unescape_string(inner) + "}")
        else:
            out.append(ch)
        index += 1
    return "".join(out)


def _format_char(c: str) -> bool:
    """isSuperSubOverbar."""
    return c in ("_", "^", "~")


def _escaped(text: str, pos: int) -> bool:
    """isEscaped: an odd number of backslashes before the position."""
    count = 0
    while pos > 0 and text[pos - 1] == "\\":
        count += 1
        pos -= 1
    return count % 2 == 1


def _to_long(digits: str) -> int:
    """wxString::ToLong of ASCII digits: 0 for no digit, LONG_MAX on overflow."""
    return min(int(digits), _LONG_MAX) if digits else 0


def parse_bus_vector(text: str) -> bool:
    """NET_SETTINGS::ParseBusVector: PREFIX[n..m]SUFFIX, with n != m."""
    n = len(text)
    i = 0
    prefix = ""
    begin = end = brace = 0
    in_quotes = False
    while i < n:
        c = text[i]
        if c == '"' and not _escaped(text, i):
            in_quotes = not in_quotes
            i += 1
            continue
        if in_quotes:
            if c == "\\" and i + 1 < n:
                i += 1
                prefix += text[i]
            else:
                prefix += c
            i += 1
            continue
        if c == "{":
            if i > 0 and _format_char(text[i - 1]):
                brace += 1
                prefix += "{"
                i += 1
                continue
            return False
        if c == "}":
            brace -= 1
            prefix += "}"
            i += 1
            continue
        if c == "\\" and i + 1 < n and text[i + 1] == " ":
            prefix += " "
            i += 2
            continue
        if c in (" ", "]"):
            return False
        if c == "[":
            break
        prefix += c
        i += 1
    i += 1
    if i >= n:
        return False
    digits = ""
    while i < n:
        if text[i] == "." and i + 1 < n and text[i + 1] == ".":
            begin = _to_long(digits)
            i += 2
            break
        if text[i] not in _DIGITS:
            return False
        digits += text[i]
        i += 1
    digits = ""
    if i >= n:
        return False
    while i < n:
        if text[i] == "]":
            end = _to_long(digits)
            i += 1
            break
        if text[i] not in _DIGITS:
            return False
        digits += text[i]
        i += 1
    while i < n:
        c = text[i]
        if c == "}":
            brace -= 1
        elif c not in ("+", "-", "P", "N"):
            return False
        i += 1
    return brace == 0 and begin != end


def parse_bus_group(text: str) -> bool:
    """NET_SETTINGS::ParseBusGroup: NAME{MEMBERS}, the rest is not read."""
    n = len(text)
    i = 0
    brace = 0
    in_quotes = False
    while i < n:
        c = text[i]
        if c == '"' and not _escaped(text, i):
            in_quotes = not in_quotes
            i += 1
            continue
        if in_quotes:
            if c == "\\" and i + 1 < n:
                i += 1
            i += 1
            continue
        if c == "{":
            if i > 0 and _format_char(text[i - 1]):
                brace += 1
                i += 1
                continue
            break
        if c == "}":
            brace -= 1
            i += 1
            continue
        if c == "\\" and i + 1 < n and text[i + 1] == " ":
            i += 2
            continue
        if c in (" ", "[", "]"):
            return False
        i += 1
    if brace != 0:
        return False
    i += 1
    if i >= n:
        return False
    in_quotes = False
    while i < n:
        c = text[i]
        if c == '"' and not _escaped(text, i):
            in_quotes = not in_quotes
            i += 1
            continue
        if in_quotes:
            if c == "\\" and i + 1 < n:
                i += 1
            i += 1
            continue
        if c == "{":
            if i > 0 and _format_char(text[i - 1]):
                brace += 1
                i += 1
                continue
            return False
        if c == "}":
            if brace:
                brace -= 1
                i += 1
                continue
            return True
        if c == "\\" and i + 1 < n and text[i + 1] == " ":
            i += 2
            continue
        i += 1
    return False


def is_bus_label(text: str) -> bool:
    """SCH_CONNECTION::IsBusLabel of the stored label text (no text variables resolved)."""
    unescaped = unescape_string(text)
    return parse_bus_vector(unescaped) or parse_bus_group(unescaped)
