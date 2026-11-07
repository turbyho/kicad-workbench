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

import asyncio
import json
import os
from collections.abc import Callable
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
from .kicad_text import kicad_text_bytes, write_kicad_text
from .sch_hierarchy import (
    PRO_SUFFIX,
    in_hierarchy,
    in_project,
    project_root_schematics,
    top_level_sheets,
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


def _open_error(opened: Path, lock_path: Path, document: str, editor: str) -> str:
    return (
        f"❌ KiCad has the {document} open: {opened} "
        f"(lock file {lock_path}, owner {_lock_owner(lock_path)}). "
        "This tool writes the file directly. KiCad does not read such a change, "
        "and the next save in KiCad removes it. Thus the tool did not write the file.\n\n"
        f"Tell the user to save the {document} and close the {editor} window. "
        "Then do this operation again. "
        f"After the write, tell the user to open the {document} again to see the change.\n\n"
        "If KiCad does not run, the lock file stays from a crash of KiCad. "
        f"Then tell the user to open the {document} in KiCad and close it again, "
        "or to delete the lock file."
    )


def check_schematic_not_open(file_path: str | Path) -> str | None:
    """Do a check if KiCad has a schematic open, before a tool writes it.

    KiCad has the schematic open when the file has a lock file, or when a
    root schematic that holds the file in the hierarchy of its project has a
    lock file. KiCad 10 can lock the first top-level sheet of the project.

    Args:
        file_path: Path to the .kicad_sch file that the tool writes.

    Returns:
        None if KiCad does not have the schematic open. Else, an error
        message for the LLM that tells the user to close the schematic editor.
    """
    path = Path(os.path.abspath(file_path))
    lock = _existing_lock(path)
    if lock:
        return _open_error(path, lock, "schematic", "schematic editor")
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
                    return _open_error(opened, lock, "schematic", "schematic editor")
            elif in_hierarchy(path, opened):
                return _open_error(opened, lock, "schematic", "schematic editor")
    return None


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
    return _open_error(path, lock, document, editor) if lock else None


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


FileStamp = tuple[int, int] | None

# The tools write in threads (the save flushes the file to the disk). The lock of
# each file (file_save.file_locks) keeps the check of the stamp and the write
# together: of two tools that read the same stamp, the second gets the refusal,
# and no change is lost. The writes of different files do not wait for each other.


def file_stamp(path: Path) -> FileStamp:
    """Return the modification time and the size of a file, or None if no file.

    A tool gets the stamp before it reads the file, and gives it to
    write_schematic() or write_board(). When the stamp changed before the
    write, an other program (for example KiCad, before it closed) saved the
    file in the meantime, and the write would remove that save.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _changed_error(path: Path) -> str:
    return (
        f"❌ The file {path} changed after this tool read it. "
        "Thus the tool did not write the file, to keep that change. "
        "Do this operation again."
    )


def _guarded_write(
    path: Path, content: str, stamp: FileStamp, open_error: str | None
) -> str | None:
    if open_error:
        return open_error
    with file_locks([path]):
        if file_stamp(path) != stamp:
            return _changed_error(path)
        # KiCad reads and writes its files as UTF-8 on each platform. The locale
        # encoding (on Windows the code page of the region, for example cp1252, cp936
        # or cp1256) cannot encode all characters, and KiCad reads its bytes
        # incorrectly.
        write_kicad_text(path, content)
    return None


async def write_in_thread(
    write: Callable[[Path, str, FileStamp], str | None], path: Path, content: str, stamp: FileStamp
) -> tuple[str | None, str]:
    """Run write_schematic() or write_board() off the event loop: the save flushes
    the file to the disk, which can take long.

    Returns:
        (the error of the write or None, a warning). The warning is "" or a
        paragraph for the answer of the tool: the file has the new content, but a
        step after the save failed (AfterSaveError). The tool then gives its
        result with the warning, not an error: the change is in the file, and an
        other call would make it two times.
    """
    try:
        return await asyncio.to_thread(write, path, content, stamp), ""
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
        stamp: The file_stamp() of the file before the tool read it.

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
        stamps: The file_stamp() of each file before the tool read it.

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
            error = _schematic_error(path, texts[path])
            if not error and file_stamp(path) != stamps[path]:
                error = _changed_error(path)
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
        stamp: The file_stamp() of the file before the tool read it, or
            before the tool made the new content.

    Returns:
        None after the write. Else, the error message for the LLM.
    """
    return _guarded_write(path, content, stamp, check_board_not_open(path))
