"""Guard for KiCad files that a KiCad editor has open.

Some tools write a .kicad_sch or .kicad_pcb file directly. When a KiCad
editor has the file open, KiCad does not read the change, and the next save
in KiCad removes it. Thus each such tool calls a check of this module just
before the write, and refuses the write when KiCad has the file open.

KiCad 8 and later writes the lock file ``~<file name>.lck`` next to the file
that an editor opens, for example ``~board.kicad_sch.lck``. The lock file
holds ``{"username": ..., "hostname": ...}``. The schematic editor locks only
the root schematic of the hierarchy, not the sub-sheets. Thus the check of a
schematic also finds the root schematics that hold it (see sch_hierarchy).

After a crash, KiCad does not remove its lock file. The guard cannot know if
KiCad still runs, thus it also refuses the write for such a lock file. The
error message tells how to remove it.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.kicad_versions import KicadFileError, schematic_text_version
from .file_save import (
    AfterSaveError,
    SaveFilesError,
    check_one_name_each,
    file_locks,
    save_files,
)
from .file_stamp import FileStamp, file_stamp
from .kicad_project import (
    PRO_SUFFIX,
    top_level_sheets,
)
from .kicad_text import kicad_text_bytes, write_kicad_text
from .sch_hierarchy import (
    in_hierarchy,
    in_project,
    project_root_schematics,
)


def lock_file_path(file_path: Path) -> Path:
    """Return the path of the KiCad lock file for a file."""
    return file_path.with_name(f"~{file_path.name}.lck")


def _existing_lock(file_path: Path) -> Path | None:
    """Return the lock file of a file, if it exists.

    KiCad writes the lock file next to the path that it opened. When that
    path is a symbolic link, the lock file is not next to the target. Thus
    the function does a check of the path as given and of the real path.
    """
    absolute = Path(os.path.abspath(file_path))
    for path in (absolute, absolute.resolve()):
        lock = lock_file_path(path)
        if lock.exists():
            return lock
    return None


def _lock_owner(lock_path: Path) -> str:
    """Return 'user@host' from a KiCad lock file, or 'unknown'."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return f"{data['username']}@{data['hostname']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


@dataclass(frozen=True)
class OpenInKicad:
    """A file that a KiCad editor has open: the file and its lock file."""

    file: Path  # the file that KiCad opened (for a schematic: the root that holds the file)
    lock: Path
    owner: str  # "user@host" from the lock file at the check, or "unknown"

    def text(self, document: str) -> str:
        """Return the first sentence of an error message: KiCad has the file open."""
        return (
            f"KiCad has the {document} open: {self.file} "
            f"(lock file {self.lock}, owner {self.owner})."
        )


def _opened(file: Path, lock: Path) -> OpenInKicad:
    return OpenInKicad(file, lock, _lock_owner(lock))


def stale_lock_text(document: str) -> str:
    """Return the text of an error message about a lock file that stays after a crash."""
    return (
        "If KiCad does not run, the lock file stays from a crash of KiCad. "
        f"Then tell the user to open the {document} in KiCad and close it again, "
        "or to delete the lock file."
    )


def _open_error(opened: OpenInKicad, document: str, editor: str) -> str:
    return (
        f"❌ {opened.text(document)} "
        "This tool writes the file directly. KiCad does not read such a change, "
        "and the next save in KiCad removes it. Thus the tool did not write the file.\n\n"
        f"Tell the user to save the {document} and close the {editor} window. "
        "Then do this operation again. "
        f"After the write, tell the user to open the {document} again to see the change.\n\n"
        + stale_lock_text(document)
    )


def schematic_open_in_kicad(file_path: str | Path) -> OpenInKicad | None:
    """Return the schematic that KiCad has open and that holds a file, or None.

    KiCad has the schematic open when the file has a lock file, or when a
    root schematic that holds the file in the hierarchy of its project has a
    lock file. KiCad 10 can lock the first top-level sheet of the project.

    Args:
        file_path: Path to the .kicad_sch file.
    """
    path = Path(os.path.abspath(file_path))
    lock = _existing_lock(path)
    if lock:
        return _opened(path, lock)
    for root in project_root_schematics(path):
        # KiCad locks the schematic that it opens (SCH_EDIT_FRAME::OpenProjectFiles):
        # for the project, the root or the first top-level sheet of a KiCad 10
        # project (RootScreen). An other top-level sheet with a lock is open alone.
        tops = [top.file for top in top_level_sheets(root.with_suffix(PRO_SUFFIX))]
        for opened in dict.fromkeys([root, *tops]):
            lock = _existing_lock(opened)
            if lock is None:
                continue
            if opened in (root, tops[0]):
                if in_project(path, root):
                    return _opened(opened, lock)
            elif in_hierarchy(path, opened):
                return _opened(opened, lock)
    return None


def check_schematic_not_open(file_path: str | Path) -> str | None:
    """Do a check if KiCad has a schematic open, before a tool writes it
    (schematic_open_in_kicad).

    Args:
        file_path: Path to the .kicad_sch file that the tool writes.

    Returns:
        None if KiCad does not have the schematic open. Else, an error
        message for the LLM that tells the user to close the schematic editor.
    """
    opened = schematic_open_in_kicad(file_path)
    return _open_error(opened, "schematic", "schematic editor") if opened else None


def check_board_not_open(file_path: str | Path) -> str | None:
    """Do a check if KiCad has a board open, before a tool writes it.

    Args:
        file_path: Path to the .kicad_pcb file that the tool writes.

    Returns:
        None if KiCad does not have the board open. Else, an error message
        for the LLM that tells the user to close the PCB editor.
    """
    return _check_file_not_open(file_path, "board", "PCB editor")


def _check_file_not_open(file_path: str | Path, document: str, editor: str) -> str | None:
    path = Path(os.path.abspath(file_path))
    lock = _existing_lock(path)
    return _open_error(_opened(path, lock), document, editor) if lock else None


def check_project_not_open(file_path: str | Path) -> str | None:
    """Do a check if KiCad has a project open, before a tool replaces its files.

    The project manager of KiCad locks the project file that it opens
    (SETTINGS_MANAGER::LoadProject), and saves the project settings again.

    Args:
        file_path: Path to the .kicad_pro file of the project.

    Returns:
        None if KiCad does not have the project open. Else, an error message for
        the LLM that tells the user to close the project.
    """
    return _check_file_not_open(file_path, "project", "KiCad project manager")


# The tools write in threads (the save flushes the file to the disk). The lock of
# each file (file_save.file_locks) keeps the check of the stamp and the write
# together: of two tools that read the same stamp, the second gets the refusal,
# and no change is lost. The writes of different files do not wait for each other.


def _changed_error(path: Path) -> str:
    return (
        f"❌ The file {path} changed after this tool read it. "
        "Thus the tool did not write the file, to keep that change. "
        "Do this operation again."
    )


def _change_error(path: Path, stamp: FileStamp) -> str | None:
    """Return the error when the file does not have the stamp now, or None."""
    try:
        current = file_stamp(path)
    except OSError as e:
        return (
            f"❌ The tool cannot read the file {path} again before the write ({e}). "
            "Thus the tool did not write the file."
        )
    return _changed_error(path) if current != stamp else None


def _guarded_write(
    path: Path, content: str, stamp: FileStamp, open_error: str | None
) -> str | None:
    if open_error:
        return open_error
    with file_locks([path]):
        error = _change_error(path, stamp)
        if error:
            return error
        # KiCad reads and writes its files as UTF-8 on each platform. The locale
        # encoding (on Windows the code page of the region, for example cp1252, cp936
        # or cp1256) cannot encode all characters, and KiCad reads its bytes
        # incorrectly.
        write_kicad_text(path, content)
    return None


def write_with_warning(
    write: Callable[[Path, str, FileStamp], str | None], path: Path, content: str, stamp: FileStamp
) -> tuple[str | None, str]:
    """Run write_schematic() or write_board(). The function blocks (the save flushes
    the file to the disk): run it off the event loop.

    Returns:
        (the error of the write or None, a warning). The warning is "" or a
        paragraph for the answer of the tool: the file has the new content, but a
        step after the save failed (AfterSaveError). The tool then gives its
        result with the warning, not an error: the change is in the file, and an
        other call would make it two times.
    """
    try:
        return write(path, content, stamp), ""
    except AfterSaveError as e:
        return None, f"\n\n⚠️ {e}"


def writable_schematic_version(path: Path, content: str) -> int | str:
    """Return the file version of a schematic that a tool can change, or the error.

    A tool adds items in the format of the file version, and it does not change
    the version. Thus a tool changes only the versions that this server knows
    (kicad_versions.check_schematic_version).
    """
    try:
        return schematic_text_version(content)
    except KicadFileError as e:
        return f"❌ {path}: {e} The tool did not change the file."


def write_schematic(path: Path, content: str, stamp: FileStamp) -> str | None:
    """Write a schematic file as UTF-8, if KiCad does not have it open.

    Each tool that writes a schematic must use this function. The checks are
    just before the write, to keep the time between them and the write short.
    The function writes only a schematic of a version that this server knows
    (writable_schematic_version).

    Args:
        path: The .kicad_sch file.
        content: The new content of the file.
        stamp: The stamp of the bytes that the tool read
            (read_for_write_stamped).

    Returns:
        None after the write. Else, the error message for the LLM.
    """
    return _guarded_write(path, content, stamp, _schematic_error(path, content))


def _schematic_error(path: Path, content: str) -> str | None:
    """The checks of a schematic before its write: a version that this server knows,
    and a file that KiCad does not have open. Return the error, or None."""
    version = writable_schematic_version(path, content)
    return version if isinstance(version, str) else check_schematic_not_open(path)


def write_schematics(
    texts: dict[Path, str], stamps: dict[Path, FileStamp]
) -> tuple[list[Path], list[str]] | str:
    """Write more than one schematic file as one change: all files or no file.

    The units of one package can be in different files: when only some files get
    the change, the units have different fields. The function has the lock of each
    file for the full change, and does these steps:

    1. The checks of write_schematic() for each file (the file version, a file
       that KiCad has open, the stamp), and the bytes of each new text.
    2. file_save.save_files: a temporary file with the new content next to each
       file, flushed to the disk (an error of the disk then stops the change
       before it changes a file), then the renames. When a rename fails, the
       files that have the new content get their old bytes again.

    Args:
        texts: The new text of each file.
        stamps: The stamp of the bytes of each file that the tool read.

    Returns:
        (the written files, warnings), or the error message for the LLM. A
        warning tells of a file that has the new content, but a step after its
        save failed (AfterSaveError).
    """
    files = list(texts)
    try:
        check_one_name_each(files)
    except SaveFilesError as e:
        return f"❌ {e} Give each sheet file one name."
    with file_locks(files):
        for path in files:
            error = _schematic_error(path, texts[path]) or _change_error(path, stamps[path])
            if error:
                return f"❌ The tool wrote no file: {error.removeprefix('❌ ')}"
        data = []
        for path in files:
            try:
                data.append((path, kicad_text_bytes(path, texts[path])))
            except WorkbenchError as e:
                return f"❌ The tool wrote no file. {path}: {e}"
        try:
            return files, save_files(data)
        except SaveFilesError as e:
            return f"❌ {e}"


def write_board(path: Path, content: str, stamp: FileStamp) -> str | None:
    """Write a board file as UTF-8, if KiCad does not have it open.

    Each tool that writes a board must use this function. The checks are
    just before the write, to keep the time between them and the write short.

    Args:
        path: The .kicad_pcb file.
        content: The new content of the file.
        stamp: The stamp of the bytes of the file that the tool read, or the
            file_stamp() before the tool made the new content.

    Returns:
        None after the write. Else, the error message for the LLM.
    """
    return _guarded_write(path, content, stamp, check_board_not_open(path))
