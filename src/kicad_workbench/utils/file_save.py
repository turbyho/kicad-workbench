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

import contextlib
import errno
import itertools
import os
import stat
import sys
import threading
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
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
            f"The folder {folder} is read only: the tool saves a file with a temporary "
            "file in its folder (KiCad 10.0.7 and later save their files in this way). The "
            f"tool did not write {target.name}."
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


def _flush_folder(folder: Path, written: str) -> None:
    """Flush the folder to the disk after the renames of the files in it (written:
    the files, for the message): the renames then stay after a power loss.
    Windows: NTFS writes the rename into its journal, and MoveFileEx with
    MOVEFILE_WRITE_THROUGH returns after that."""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as e:
        # A network or FUSE volume can refuse to open a folder or to flush it.
        if e.errno in (errno.EINVAL, errno.ENOTSUP):
            return
        raise _folder_error(written, e) from e
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno != errno.EINVAL:
            raise _folder_error(written, e) from e
    finally:
        os.close(fd)


def _folder_error(written: str, error: OSError) -> AfterSaveError:
    return AfterSaveError(
        f"The tool wrote {written}, but it could not flush the folder to the disk ({error}). "
        "After a power loss, the files can have their old content."
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

    def commit(self, flush_folder: bool = True) -> None:
        """Rename the temporary file over the file. flush_folder=False: the caller
        flushes the folder after more renames.

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
        if flush_folder:
            _flush_folder(self.target.parent, str(self.target))

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
    with file_locks([path]):
        PendingSave(path, data).commit()


# A lock for each file: the save of a set of files (save_files) and its restore
# after an error must not run at the same time as an other save of the same file.
# Reentrant: write_schematics has the locks of its files, and save_files takes them
# again. A lock goes from the table when no thread uses it.
@dataclass
class _FileLock:
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0


_LOCKS: dict[str, _FileLock] = {}
_LOCKS_GUARD = threading.Lock()


def file_key(path: Path) -> str:
    """The key of the file that the save writes (_target): the file that a symbolic
    link points to, or the link itself when it points to no file. The folder with no
    links. Windows: with no regard to case (os.path.normcase)."""
    target = _target(path)
    return os.path.normcase(os.path.join(os.path.realpath(target.parent), target.name))


@contextlib.contextmanager
def file_locks(paths: list[Path]) -> Iterator[None]:
    """Hold the lock of each file, taken in one order for all callers: no deadlock."""
    with _LOCKS_GUARD:
        entries = []
        for key in sorted({file_key(path) for path in paths}):
            entry = _LOCKS.setdefault(key, _FileLock())
            entry.users += 1
            entries.append((key, entry))
    try:
        with contextlib.ExitStack() as held:
            for _key, entry in entries:
                held.enter_context(entry.lock)
            yield
    finally:
        with _LOCKS_GUARD:
            for key, entry in entries:
                entry.users -= 1
                if entry.users == 0:
                    del _LOCKS[key]


if sys.platform == "win32":
    from . import _file_save_windows as _windows


class SaveFilesError(OSError):
    """save_files() failed. The message tells which files have their old content:
    all files, or (when also the restore failed) the files that it names."""


def _sentence(error: BaseException) -> str:
    text = str(error).rstrip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def check_one_name_each(paths: list[Path]) -> None:
    """Refuse two names of one file: the same name two times, a link, a hard link,
    or a name with an other case of a file that is there. Two contents for one file
    would lose one content (the second rename removes the first).

    Raises:
        SaveFilesError: Two names of one file, or a name that cannot be checked.
    """
    seen: dict[tuple, Path] = {}
    for path in paths:
        try:
            identity = _identity(path)
        except OSError as e:
            raise SaveFilesError(f"The tool wrote no file. {path}: {_sentence(e)}") from e
        if identity in seen:
            raise SaveFilesError(
                f"The tool wrote no file: {seen[identity]} and {path} are one file (the same "
                "name, a link, or the same name with an other case)."
            )
        seen[identity] = path


def _identity(path: Path) -> tuple:
    """The identity of a file: the device and the file number of a file that is
    there (also a symbolic link, a hard link, or a name with an other case on a file
    system that ignores the case), else its key."""
    try:
        status = os.stat(path)
    except OSError:
        # No file, or a link that the save replaces (a link loop: _target).
        return ("name", file_key(path))
    return ("file", status.st_dev, status.st_ino)


@dataclass
class _Link:
    """A symbolic link that points to no file: the save replaces it with a file."""

    target: str


def _old_state(path: Path) -> bytes | _Link | None:
    """The content of a file before the save: its bytes, a link to no file, or None
    (no file)."""
    if path.is_symlink() and not path.exists():
        return _Link(os.readlink(path))
    return path.read_bytes() if path.exists() else None


def save_files(files: list[tuple[Path, bytes]]) -> list[str]:
    """Save more than one file as one change: all files or no file.

    1. The lock of each file (file_locks), in one order.
    2. A PendingSave for each file: a temporary file with the new content next to
       the file, flushed to the disk. An error of the disk (a full disk, a file
       that is read only) then stops the change before it changes a file.
    3. The renames. When a rename fails, the files that have the new content get
       their old content again: the old bytes, no file, or the old link.
    4. The flush of each folder to the disk, one time.

    Returns:
        The warnings: the files have the new content, but a step after the save
        failed (AfterSaveError).

    Raises:
        SaveFilesError: The save failed. The message tells the state of the files.
    """
    check_one_name_each([path for path, _data in files])
    with file_locks([path for path, _data in files]):
        return _save_locked(files)


def _save_locked(files: list[tuple[Path, bytes]]) -> list[str]:
    pending: list[PendingSave] = []
    # The old state of each file but the last: the restore after a failed rename
    # gives it to the files before that rename.
    old: dict[Path, bytes | _Link | None] = {}
    current = files[0][0] if files else None
    try:
        for index, (current, data) in enumerate(files):
            if index < len(files) - 1:
                old[current] = _old_state(current)
            pending.append(PendingSave(current, data))
    except OSError as e:
        raise SaveFilesError(
            f"The tool wrote no file. {current}: {_sentence(e)}{_discard(pending)}"
        ) from e
    except BaseException:
        _discard(pending)
        raise
    try:
        _check_new_names(pending)
    except SaveFilesError as e:
        raise SaveFilesError(f"{e}{_discard(pending)}") from e
    written: list[Path] = []
    warnings: list[str] = []
    for index, save in enumerate(pending):
        path = files[index][0]
        try:
            save.commit(flush_folder=False)
        except AfterSaveError as e:
            warnings.append(str(e))
        except OSError as e:
            # commit() removed the temporary file of this save, or its error names it.
            raise SaveFilesError(
                f"The tool could not write {path}: {_sentence(e)}"
                f"{_discard(pending[index + 1 :])}{_restore(written, old)}"
            ) from e
        except BaseException:
            _discard(pending[index + 1 :])
            _restore(written, old)
            raise
        written.append(path)
    folders: dict[Path, list[str]] = {}
    for save in pending:
        folders.setdefault(save.target.parent, []).append(str(save.target))
    for folder, names in folders.items():
        try:
            _flush_folder(folder, ", ".join(names))
        except AfterSaveError as e:
            warnings.append(str(e))
    return warnings


def _check_new_names(pending: list[PendingSave]) -> None:
    """Refuse two new names in one folder that the file system reads as one name: a
    name with an other case or an other Unicode normalization (Windows, macOS, a FAT
    volume, a folder of ext4 with casefold). Only the file system knows its rule:
    the function looks for the temporary file of the first name under the spelling
    of the second name.

    Raises:
        SaveFilesError: Two names of one file.
    """
    groups: dict[tuple[Path, str], PendingSave] = {}
    for save in pending:
        key = (save.target.parent, unicodedata.normalize("NFC", save.target.name).casefold())
        first = groups.setdefault(key, save)
        if first is save or first.target.name == save.target.name:
            continue
        suffix = first.temp.name[len(first.target.name) :]
        probe = save.target.with_name(save.target.name + suffix)
        try:
            same = os.path.samefile(probe, first.temp)
        except OSError:
            same = False
        if same:
            raise SaveFilesError(
                f"The tool wrote no file: {first.target} and {save.target} are one file (the "
                "file system of the folder does not compare the case or the Unicode form of "
                "the names)."
            )


def _discard(pending: list[PendingSave]) -> str:
    """Remove the temporary files of the saves that did not run. Return the text for
    the files that stay."""
    kept = []
    for save in pending:
        try:
            save.discard()
        except OSError as e:
            kept.append(f"{save.temp} ({e})")
    if not kept:
        return ""
    return f" The tool could not remove these temporary files: {'; '.join(kept)}."


def _restore(written: list[Path], old: dict[Path, bytes | _Link | None]) -> str:
    """Give the old state to the files that have the new content. Return the text
    for the answer."""
    if not written:
        return " The tool wrote no file."
    restored, removed, failed, notes = [], [], [], []
    folders: dict[Path, list[str]] = {}
    for path in written:
        state = old[path]
        try:
            if state is None:
                path.unlink()
                removed.append(path.name)
                folders.setdefault(path.parent, []).append(str(path))
                continue
            if isinstance(state, _Link):
                # The link under a free name first, then a rename over the file: when
                # the link cannot be made, the file keeps the new content.
                link = _free_name(path)
                os.symlink(state.target, link)
                try:
                    os.replace(link, path)
                except OSError:
                    link.unlink(missing_ok=True)
                    raise
                folders.setdefault(path.parent, []).append(str(path))
            else:
                save_file(path, state)
        except AfterSaveError as e:
            notes.append(str(e))
        except OSError as e:
            failed.append(f"{path} ({e})")
            continue
        restored.append(path.name)
    for folder, names in folders.items():
        try:
            _flush_folder(folder, ", ".join(names))
        except AfterSaveError as e:
            notes.append(str(e))
    done = []
    if restored:
        done.append(f"it gave {', '.join(restored)} the old content again")
    if removed:
        done.append(
            f"it removed the new file{'s' if len(removed) > 1 else ''} {', '.join(removed)}"
        )
    note = f" {' '.join(notes)}" if notes else ""
    if failed:
        text = (
            " The tool could not give these files their old content again: "
            f"{'; '.join(failed)}. These files have the new content, the other files do not"
        )
        return f"{text} ({', and '.join(done)}).{note}" if done else f"{text}.{note}"
    return f" The tool wrote no file ({', and '.join(done)}).{note}"
