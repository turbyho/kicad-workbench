"""The file name of a sheet, as the loader of KiCad 8, 9 and 10 makes it.

``SCH_IO_KICAD_SEXPR::loadHierarchy`` puts the sheet file name (after the
expansion of the text variables) into a ``wxFileName``. When ``IsAbsolute``
is false, it calls ``MakeAbsolute`` with the directory of the parent file.
Then it compares the text of ``GetFullPath`` with the file names of the
ancestors and of the screens that it loaded before, and it opens the file
with that text. The comparison is a text comparison: KiCad does not follow a
symbolic link, and it compares the case of the letters.

The rules of wxWidgets 3.2 (src/common/filename.cpp):

1. The name splits into a volume (Windows only), the directories and the last
   component. A path separator at the start makes the name rooted. Empty
   directory components (two separators) are removed. Windows accepts "\\"
   and "/" and writes "\\".
2. A rooted name with a volume (Windows), or a rooted name (other systems),
   is absolute: the text stays as it is, also with "." and "..".
3. Else ``MakeAbsolute`` adds the directories of the parent in front, and
   removes "." and "<dir>/..". A Windows name with no volume gets the volume
   of the parent, and a rooted name keeps its own directories.
4. ``IsAbsolute`` with the native format does not do the check of "~" (the
   check is only for the format wxPATH_UNIX). Thus ``MakeAbsolute`` replaces a
   first directory "~" or "~<user>" (not on Windows) with the home directory
   of the user (``wxGetUserHome``) in place of the directory of the parent.
   For a user that is not known, the home directory is empty: KiCad then
   opens the rest relative to its working directory.

Measured with kicad-cli 8.0.9, 9.0.9, 10.0.6 and 10.0.7 on Linux
(spike/kicad-versions/sheet_file_names_check.py). The rules for Windows come
from the source of wxWidgets only.
"""

import os
from dataclasses import dataclass

from ..errors import KicadFileError

# The server uses the rules of the system that it runs on.
WINDOWS = os.name == "nt"


class SheetFileError(KicadFileError):
    """KiCad opens a sheet file relative to its working directory, which the server
    does not know."""


@dataclass(frozen=True)
class KicadFileName:
    """A file name as ``wxFileName`` keeps it."""

    windows: bool
    volume: str  # Windows: a drive letter, or "\\\\server" of a UNC path
    rooted: bool  # a path separator at the start (after the volume)
    dirs: tuple[str, ...]
    last: str  # the last component (the file name), as it is

    @property
    def text(self) -> str:
        """``wxFileName::GetFullPath``: the text that the loader compares and opens."""
        separator = "\\" if self.windows else "/"
        volume = ""
        if self.windows and self.volume:
            volume = self.volume + (":" if len(self.volume) == 1 else "")
        return (
            volume
            + (separator if self.rooted else "")
            + "".join(part + separator for part in self.dirs)
            + self.last
        )

    @property
    def directory(self) -> "KicadFileName":
        """The directory of the file: the parent of its sheets (``GetPath``)."""
        return KicadFileName(self.windows, self.volume, self.rooted, self.dirs, "")


def _separators(windows: bool) -> str:
    return "\\/" if windows else "/"


def _split_volume(text: str) -> tuple[str, str]:
    """``wxFileName::SplitVolume`` of Windows: (volume, rest)."""
    seps = _separators(True)
    guid = "\\\\?\\Volume{"
    if len(text) >= 49 and text.startswith(guid) and text[48] == "\\":
        return text[:48], text[48:]
    if len(text) >= 3 and text[0] in seps and text[1] in seps and text[2] not in seps:
        end = next((i for i in range(3, len(text)) if text[i] in seps), len(text))
        return "\\\\" + text[2:end], text[end:]
    colon = text.find(":")
    if colon > 0:
        return text[:colon], text[colon + 1 :]
    return "", text


def parse_file_name(text: str, windows: bool) -> KicadFileName:
    """Split a text as ``wxFileName( text )`` does."""
    volume, rest = _split_volume(text) if windows else ("", text)
    seps = _separators(windows)
    cut = max(rest.rfind(sep) for sep in seps)
    directory, last = (rest[:cut] or rest[:1], rest[cut + 1 :]) if cut >= 0 else ("", rest)
    rooted = bool(directory) and directory[0] in seps
    parts = directory
    for sep in seps[1:]:
        parts = parts.replace(sep, seps[0])
    dirs = tuple(part for part in parts.split(seps[0]) if part)
    return KicadFileName(windows, volume, rooted, dirs, last)


def _without_dots(dirs: tuple[str, ...], rooted: bool) -> tuple[str, ...]:
    """``wxPATH_NORM_DOTS``: remove "." and "<dir>/.."; an extra ".." of a rooted name
    goes, of a relative name it stays."""
    result: list[str] = []
    for part in dirs:
        if part == ".":
            continue
        if part == "..":
            if not result:
                if rooted:
                    continue
            elif result[-1] != "..":
                result.pop()
                continue
        result.append(part)
    return tuple(result)


def user_home(user: str) -> str:
    """``wxGetUserHome`` of Unix: the home directory of the user, or "" for a user that
    is not known. No user: $HOME, else the user of $USER or $LOGNAME, else the user of
    the process."""
    import pwd  # Unix only: Windows has no "~" rule

    if not user:
        home = os.environ.get("HOME")
        if home is not None:
            return home
        entry = None
        name = os.environ.get("USER") or os.environ.get("LOGNAME")
        if name is not None:
            try:
                entry = pwd.getpwnam(name)
            except KeyError:
                entry = None
        if entry is None:
            try:
                entry = pwd.getpwuid(os.getuid())
            except KeyError:
                return ""
        return entry.pw_dir
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return ""


def uses_home(name: str, windows: bool) -> bool:
    """Return True if the loader replaces the first directory of a sheet file name with
    a home directory (rule 4 of the module)."""
    own = parse_file_name(name, windows)
    return not windows and not own.rooted and bool(own.dirs) and own.dirs[0].startswith("~")


def sheet_file_name(name: str, parent: KicadFileName) -> KicadFileName:
    """Return the file name that the loader gives a sheet file name (see the module).

    Args:
        name: The sheet file name after the expansion of the text variables.
        parent: The file name of the parent screen.

    Raises:
        SheetFileError: The name starts with "~<user>" of a user that is not known, or
            with "~" and the home directory is not an absolute path: KiCad opens the
            file relative to its working directory.
    """
    windows = parent.windows
    own = parse_file_name(name, windows)
    if own.rooted and (own.volume or not windows):
        return own  # IsAbsolute: no change
    base = parent.directory
    dirs = own.dirs
    volume = own.volume or base.volume
    if own.rooted:
        # A rooted Windows name with no volume gets the volume of the parent only.
        return KicadFileName(windows, volume, True, _without_dots(dirs, True), own.last)
    if not windows and dirs and dirs[0].startswith("~"):
        home = user_home(dirs[0][1:])
        if not home.startswith("/"):
            raise SheetFileError(
                f"The sheet file name {name!r} starts with {dirs[0]!r}: KiCad replaces it "
                f"with the home directory of the user, but the home directory is "
                f"{home!r}. KiCad then opens the file relative to its working directory, "
                "which the server does not know."
            )
        base = parse_file_name(home.rstrip("/") + "/", False)
        dirs = dirs[1:]
    all_dirs = _without_dots((*base.dirs, *dirs), True)
    return KicadFileName(windows, volume, True, all_dirs, own.last)


def root_file_name(path: str, windows: bool) -> KicadFileName:
    """Return the file name of a top-level sheet: KiCad makes it relative to the
    project and then absolute again, thus "." and ".." go (``MakeRelativeTo``).

    Args:
        path: The absolute path of the file.
    """
    return parse_file_name(os.path.normpath(path), windows)
