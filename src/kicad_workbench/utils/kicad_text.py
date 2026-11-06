"""Read and write the text of KiCad files.

KiCad writes its files as UTF-8 on each platform. Its parser (``DSNLEXER``)
reads each string token with ``FromUTF8()``: a string with a byte that is not
valid UTF-8 becomes an empty string, and KiCad loads the rest of the file. A
byte that is not valid UTF-8 in a token that is not a string stops the load.

A read tool sees the file as KiCad does (``read_as_kicad``). A write tool
cannot keep such a byte in its text, thus it refuses the file
(``read_for_write``).
"""

import json
import os
import re
from pathlib import Path

from ..errors import InputError
from .file_save import save_file

# One S-expression token, as DSNLEXER reads it: a parenthesis, a quoted string
# with backslash escapes, or a bare token. A string starts only at the start of
# a token. A bare token ends only at a space or a parenthesis
# (DSNLEXER::isSep()), thus it can hold a '"'.
TOKEN = re.compile(r'[()]|"(?:[^"\\]|\\.)*"|[^\s()"][^\s()]*')
# The surrogateescape decoder gives these characters for invalid bytes.
_INVALID_BYTE = re.compile("[\udc80-\udcff]")


class KiCadEncodingError(InputError):
    """A KiCad file is not valid UTF-8."""


def _universal_newlines(text: str) -> str:
    # The same translation as read_text(): KiCad writes CRLF on Windows.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _byte_offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8", errors="surrogateescape"))


def decode_as_kicad(data: bytes, path: Path) -> str:
    """Return the text of the bytes of a KiCad file as KiCad reads it.

    A string with a byte that is not valid UTF-8 becomes an empty string.

    Raises:
        KiCadEncodingError: A byte that is not valid UTF-8 is in a token that
            is not a string. KiCad cannot load such a file.
    """
    text = data.decode("utf-8", errors="surrogateescape")
    if not _INVALID_BYTE.search(text):
        return _universal_newlines(text)
    parts = []
    end = 0
    for match in TOKEN.finditer(text):
        token = match.group()
        invalid = _INVALID_BYTE.search(token)
        if invalid is None:
            continue
        if not token.startswith('"'):
            offset = _byte_offset(text, match.start() + invalid.start())
            raise KiCadEncodingError(
                f"The file {path} is not valid UTF-8: byte 0x{data[offset]:02x} at "
                f"offset {offset} is outside a string, thus KiCad cannot load the file."
            )
        parts.append(text[end : match.start()])
        parts.append('""')
        end = match.end()
    parts.append(text[end:])
    return _universal_newlines("".join(parts))


def read_as_kicad(path: Path) -> str:
    """Return the text of a KiCad file as KiCad reads it (see decode_as_kicad).

    Raises:
        OSError: The file cannot be read.
        KiCadEncodingError: KiCad cannot load the file.
    """
    return decode_as_kicad(path.read_bytes(), path)


def read_for_write(path: Path) -> str:
    """Return the text of a KiCad file that a tool changes and writes back.

    Raises:
        OSError: The file cannot be read.
        KiCadEncodingError: The file is not valid UTF-8.
    """
    data = path.read_bytes()
    try:
        return _universal_newlines(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        location = f"byte 0x{data[error.start]:02x} at offset {error.start}"
    raise KiCadEncodingError(
        f"The file {path} is not valid UTF-8 ({location}). The tool did not change the "
        "file. KiCad reads a string with such a byte as an empty string. To repair the "
        "file, correct the byte, or open the file in KiCad and save it: KiCad then "
        "writes that string as an empty string."
    )


def write_kicad_text(path: Path, content: str) -> None:
    """Write the text of a KiCad file as UTF-8, with the line ends of KiCad.

    KiCad writes its files in text mode ("wt"), thus with CRLF on Windows. The
    function encodes the text before it opens the file: text that is not valid
    (a lone surrogate) gives an error, and the file stays as it was. The save is
    the save of KiCad 10.0.7 (file_save.save_file): after an error, the file has
    its old content, not a part of the new content.

    Raises:
        InputError: The text holds a lone surrogate (from an argument of a tool).
        OSError: The file cannot be written. The file did not change.
        AfterSaveError: The file has the new content, but its folder is not
            flushed to the disk.
    """
    save_file(path, kicad_text_bytes(path, content))


def kicad_text_bytes(path: Path, content: str) -> bytes:
    """The bytes of the text of a KiCad file, as write_kicad_text writes them: UTF-8,
    with the line ends of KiCad.

    Raises:
        InputError: The text holds a lone surrogate (from an argument of a tool).
    """
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as e:
        raise InputError(
            f"The text for {path.name} is not valid text ({e}). The tool did not change the file."
        ) from e
    if os.linesep != "\n":
        data = data.replace(b"\n", os.linesep.encode("ascii"))
    return data


# A JSON string, a comment to the end of the line, or a block comment.
_JSON_PART = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)


def _not_json(name: str) -> None:
    raise InputError(f"{name} is not a JSON value")


def read_kicad_json(path: Path) -> object:
    """Read a JSON file of KiCad, for example a .kicad_pro file, as KiCad reads it.

    KiCad parses the file with nlohmann::json and ignore_comments
    (JSON_SETTINGS::LoadFromFile): a UTF-8 byte order mark at the start and
    the comments "//" and "/* */" are allowed. NaN and Infinity are not JSON.

    Raises:
        OSError: The file cannot be read.
        InputError: The file is not valid UTF-8, or it is not JSON.
    """
    data = path.read_bytes()
    try:
        text = data.decode("utf-8").removeprefix("\ufeff")
        text = _JSON_PART.sub(lambda m: m.group() if m.group().startswith('"') else " ", text)
        return json.loads(text, parse_constant=_not_json)
    except ValueError as e:  # UnicodeDecodeError, JSONDecodeError, _not_json
        raise InputError(f"{path} is not valid JSON: {e}") from e
