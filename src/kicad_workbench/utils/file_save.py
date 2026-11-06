"""Save a file as KiCad 10.0.7 and later save it: a temporary file next to the
file, then a rename over the file.

KiCad 8, 9 and 10.0.0 to 10.0.6 write a file in place (FILE_OUTPUTFORMATTER). An
error during the write (for example a full disk) then leaves only a part of the
file. KiCad 10.0.7 writes the content into a temporary file in the same folder,
flushes it to the disk, and renames it over the file (SIBLING_TEMP_FILE,
KIPLATFORM::IO::CommitTempFile). After an error, the file has its old content. A
reader gets the old or the new file, never a part.

The facts from the KiCad source (libs/kiplatform, common/richio.cpp):

- The temporary file is "<file>.kicad-save-<process ID>-<counter>" in the folder
  of the file, made only when no file has that name. Its permissions are 0600
  when the file exists, else the default permissions of a new file.
- A symbolic link: KiCad saves the file that the link points to, and the link
  stays (KIPLATFORM::IO::ResolveSymlinkTarget). A link that points to no file
  (realpath fails): KiCad saves at the name of the link, and the rename replaces
  the link with a file.
- Before the rename, KiCad flushes the temporary file to the disk (fsync, on
  macOS F_FULLFSYNC) and copies the permissions of the file to it (POSIX: the
  permission bits, Windows: the DACL). When the volume does not keep
  permissions (FAT, a cloud drive), KiCad saves the file with no copy. (The tag
  10.0.7 stopped the save then: KiCad repaired this in commit 701c54dd86 of the
  branch 10.0 and of master, issue 25691, "That aborted every save".)
- On Windows, KiCad renames with MoveFileEx (MOVEFILE_REPLACE_EXISTING and
  MOVEFILE_WRITE_THROUGH), and tries again for 0.5 s when an other program (an
  antivirus or a cloud sync) locks the file. Then it tries ReplaceFileW, which
  can replace a file that an other program has open with FILE_SHARE_DELETE. It
  removes the attributes READONLY and HIDDEN before the rename, and sets them
  again after it.
- After the rename, KiCad flushes the folder to the disk (POSIX). A folder that
  cannot do this (EINVAL, ENOTSUP) is not an error.
- KiCad does not save a file that it cannot write (EDA_BASE_FRAME::IsWritable,
  KiCad 8, 9 and 10).

The save of KiCad 10.0.7 applies with each installed KiCad. Thus a file in a
folder that is read only gives an error, also with KiCad 8, 9 and 10.0.0 to
10.0.6, which write such a file in place.

One difference: KiCad calls ReplaceFileW with no backup file. Then, after the
error ERROR_UNABLE_TO_MOVE_REPLACEMENT, the file has no content at its name
(the documentation of ReplaceFileW). This module gives ReplaceFileW a backup
file: after an error, the file keeps its old content.
"""

import errno
import itertools
import os
import stat
import sys
import time
from pathlib import Path

if sys.platform != "win32":
    import fcntl

# The number of each temporary file of this process (the counter of
# KIPLATFORM::IO::MakeSiblingTempPath).
_COUNTER = itertools.count()
_NAME_ATTEMPTS = 32
# The locks of an antivirus or a cloud sync: ERROR_ACCESS_DENIED,
# ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION. 10 attempts, 50 ms between them.
_LOCK_ERRORS = (5, 32, 33)
_RENAME_ATTEMPTS = 10
_RENAME_WAIT = 0.05
# The Windows attributes that KiCad removes before the rename and sets again.
# _check_writable refuses a file with READONLY: READONLY is here only for a file
# that an other program made read only after the check.
_KEPT_ATTRIBUTES = stat.FILE_ATTRIBUTE_READONLY | stat.FILE_ATTRIBUTE_HIDDEN


class AfterSaveError(OSError):
    """The file has the new content, but a step after the rename failed: the flush
    of the folder to the disk (after a power loss, the file can have its old
    content), or the remove of the backup file of ReplaceFileW."""


def _target(path: Path) -> Path:
    """The file to save: for a symbolic link, the file that it points to. A rename
    over the link would replace the link with a file. A link that points to no file
    stays the name to save, as in KiCad (realpath fails)."""
    if path.is_symlink():
        try:
            return Path(os.path.realpath(path, strict=True))
        except OSError:
            return path
    return path


def _check_writable(target: Path) -> None:
    """Refuse a file that KiCad does not save (EDA_BASE_FRAME::IsWritable), and a
    folder in which the save cannot make its temporary file."""
    folder = target.parent
    if not folder.is_dir():
        raise FileNotFoundError(
            f"The folder {folder} does not exist. The tool did not write {target.name}."
        )
    if target.exists() and not os.access(target, os.W_OK):
        raise PermissionError(
            f"The file {target} is read only. KiCad also does not save a file that it "
            "cannot write. The tool did not change the file."
        )
    if not os.access(folder, os.W_OK):
        # KiCad 10.0.7 and later also make the temporary file in the folder. KiCad 8,
        # 9 and 10.0.0 to 10.0.6 write in place: they save a file that is writable
        # in such a folder.
        raise PermissionError(
            f"The folder {folder} is read only: the save makes a temporary file in it, as "
            f"KiCad 10.0.7 and later do. The tool did not write {target.name}."
        )


def _sibling(target: Path) -> Path:
    """A name for a temporary file next to the file (KIPLATFORM::IO::MakeSiblingTempPath)."""
    return target.with_name(f"{target.name}.kicad-save-{os.getpid()}-{next(_COUNTER)}")


def _no_free_name(target: Path) -> FileExistsError:
    return FileExistsError(
        f"The tool found no free name for a temporary file next to {target}. "
        "The tool did not change the file."
    )


def _open_temp(target: Path, mode: int) -> tuple[int, Path]:
    """Make a new temporary file next to the file. Return its descriptor and path."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT"):
        flags |= getattr(os, name, 0)
    for _ in range(_NAME_ATTEMPTS):
        temp = _sibling(target)
        try:
            return os.open(temp, flags, mode), temp
        except FileExistsError:
            continue
    raise _no_free_name(target)


def _free_name(target: Path) -> Path:
    """A name next to the file that no file has (the backup file of ReplaceFileW)."""
    for _ in range(_NAME_ATTEMPTS):
        name = _sibling(target)
        if not os.path.lexists(name):
            return name
    raise _no_free_name(target)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _flush(fd: int) -> None:
    """Flush the file to the disk. macOS: F_FULLFSYNC, because fsync puts the data
    only into the cache of the drive. A volume with no F_FULLFSYNC (a network or
    FUSE volume) gets fsync, as KiCad does."""
    full = getattr(fcntl, "F_FULLFSYNC", None) if sys.platform != "win32" else None
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def _remove(temp: Path, error: BaseException) -> None:
    """Remove the temporary file after an error. If the remove also fails, give the
    two errors."""
    try:
        temp.unlink(missing_ok=True)
    except OSError as e:
        raise OSError(
            f"{error} The tool could not remove the temporary file {temp}: {e}"
        ) from error


def _copy_permissions(target: Path, temp: Path) -> None:
    """Give the temporary file the permissions of the file (KiCad: DuplicatePermissions).
    A volume that does not keep permissions (FAT, a cloud drive) refuses the copy:
    then the new file has the permissions of the temporary file, as in KiCad."""
    if sys.platform == "win32":
        _windows.copy_dacl(target, temp)
        return
    try:
        os.chmod(temp, stat.S_IMODE(os.stat(target).st_mode) & 0o777)
    except OSError:
        pass


def _rename(temp: Path, target: Path) -> Path | None:
    """Rename the temporary file over the file. Return the backup file of
    ReplaceFileW (Windows), which the caller removes, or None.

    Raises:
        OSError: The rename failed. The file did not change.
    """
    if sys.platform != "win32":
        os.replace(temp, target)
        return None
    for attempt in range(_RENAME_ATTEMPTS):
        try:
            _windows.move_file(temp, target)
            return None
        except OSError as e:
            error = e
            if e.winerror not in _LOCK_ERRORS:
                break
        if attempt < _RENAME_ATTEMPTS - 1:
            time.sleep(_RENAME_WAIT)
    if not target.exists():
        raise error
    backup = _free_name(target)
    try:
        _windows.replace_file(target, temp, backup)
    except OSError as e:
        raise OSError(f"{error} ReplaceFileW also failed: {e}") from error
    return backup


def _remove_backup(target: Path, backup: Path) -> None:
    try:
        backup.unlink()
    except OSError as e:
        raise AfterSaveError(
            f"The tool wrote {target}, but it could not remove the backup file {backup} "
            f"({e}). The backup file has the old content."
        ) from e


def _flush_folder(target: Path) -> None:
    """Flush the folder of the file to the disk: the rename then stays after a power
    loss. Windows: NTFS writes the rename into its journal, and MoveFileEx with
    MOVEFILE_WRITE_THROUGH returns after that."""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as e:
        # A network or FUSE volume can refuse to open a folder or to flush it.
        if e.errno in (errno.EINVAL, errno.ENOTSUP):
            return
        raise _folder_error(target, e) from e
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno != errno.EINVAL:
            raise _folder_error(target, e) from e
    finally:
        os.close(fd)


def _folder_error(target: Path, error: OSError) -> AfterSaveError:
    return AfterSaveError(
        f"The tool wrote {target}, but it could not flush the folder to the disk ({error}). "
        "After a power loss, the file can have its old content."
    )


class PendingSave:
    """The new content of a file, in a temporary file next to it, flushed to the disk.

    commit() renames the temporary file over the file. discard() removes it.
    A change of more than one file makes a PendingSave for each file first:
    an error of the disk then stops the change before it changes a file.

    Raises:
        PermissionError: KiCad does not save the file (it is read only).
        OSError: The temporary file cannot be written. The file did not change,
            and the temporary file is removed.
    """

    def __init__(self, path: Path, data: bytes):
        self.target = _target(path)
        _check_writable(self.target)
        exists = self.target.exists()
        fd, self.temp = _open_temp(self.target, 0o600 if exists else 0o666)
        try:
            try:
                _write_all(fd, data)
                _flush(fd)
            finally:
                # A network volume or a quota can give the error of the write at
                # the close: the temporary file is then not complete.
                os.close(fd)
        except BaseException as e:
            _remove(self.temp, e)
            raise

    def commit(self) -> None:
        """Rename the temporary file over the file.

        Raises:
            OSError: The rename failed. The file did not change, and the
                temporary file is removed.
            AfterSaveError: The file has the new content, but a step after the
                rename failed.
        """
        attributes = 0
        backup = None
        try:
            if self.target.exists():
                _copy_permissions(self.target, self.temp)
                if sys.platform == "win32":
                    attributes = os.stat(self.target).st_file_attributes & _KEPT_ATTRIBUTES
                    _windows.set_attributes(self.target, remove=attributes)
            backup = _rename(self.temp, self.target)
        except BaseException as e:
            _remove(self.temp, e)
            raise
        finally:
            if attributes:
                _windows.set_attributes(self.target, add=attributes)
        if backup is not None:
            _remove_backup(self.target, backup)
        _flush_folder(self.target)

    def discard(self) -> None:
        """Remove the temporary file. The file does not change."""
        self.temp.unlink(missing_ok=True)


def save_file(path: Path, data: bytes) -> None:
    """Save the bytes into the file as KiCad 10.0.7 saves a file.

    Raises:
        PermissionError: KiCad does not save the file (it is read only).
        OSError: The save failed. The file did not change.
        AfterSaveError: The file has the new content, but a step after the
            rename failed.
    """
    PendingSave(path, data).commit()


if sys.platform == "win32":
    from . import _file_save_windows as _windows
