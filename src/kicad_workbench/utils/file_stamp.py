"""The stamp of a file: the SHA-256 of its bytes.

A tool that reads a file for a later write keeps the stamp of the bytes that it
read. The write functions (kicad_lock) compare it with the stamp of the file
before the write, and refuse the write when an other program changed the file.
"""

import hashlib
from pathlib import Path

from .kicad_text import text_for_write

# The stamp of a file: the SHA-256 of its bytes, or None when there is no file.
# The time and the size of a file cannot show each change: two writes with the
# same size in one tick of the clock of the file system give the same time.
FileStamp = str | None


def bytes_stamp(data: bytes) -> str:
    """Return the stamp of the bytes of a file (FileStamp)."""
    return hashlib.sha256(data).hexdigest()


def read_stamped(path: Path) -> tuple[bytes, str]:
    """Return the bytes of a file and their stamp, from one read.

    A tool or a parser that reads a file for a later write uses this function.
    Thus a change between two reads cannot go unseen.

    Raises:
        OSError: The file cannot be read.
    """
    data = path.read_bytes()
    return data, bytes_stamp(data)


def file_stamp(path: Path) -> FileStamp:
    """Return the stamp of the current bytes of a file, or None if no file.

    The write functions compare it with the stamp of the bytes that the tool
    read. When the two are different, an other program (for example KiCad,
    before it closed) saved the file in the meantime, and the write would
    remove that save.

    Raises:
        OSError: The file cannot be read.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return bytes_stamp(data)


def read_for_write_stamped(path: Path) -> tuple[str, str]:
    """Return the text of a file that a tool changes (kicad_text.read_for_write),
    and the stamp of the same bytes.

    A tool gives the stamp to write_schematic() (read_stamped).

    Raises:
        OSError: The file cannot be read.
        KiCadEncodingError: The file is not valid UTF-8.
    """
    data, stamp = read_stamped(path)
    return text_for_write(data, path), stamp
