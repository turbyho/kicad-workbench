"""Tests of file_save: the save of KiCad 10.0.7 (a temporary file, then a rename).

The old write (Path.write_bytes) wrote the file in place: an error during the
write left only a part of the file.
"""

import errno
import os
import stat
import sys

import pytest

from kicad_workbench.errors import InputError
from kicad_workbench.utils import file_save
from kicad_workbench.utils.file_save import AfterSaveError, PendingSave, save_file
from kicad_workbench.utils.kicad_text import write_kicad_text

OLD = b"(kicad_sch (version 20250114))\n"
NEW = b'(kicad_sch (version 20250114) (title_block (title "new")))\n'
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


@pytest.fixture
def sch(tmp_path):
    path = tmp_path / "a.kicad_sch"
    path.write_bytes(OLD)
    return path


def _files(folder):
    return sorted(p.name for p in folder.iterdir())


def test_save_writes_the_content_and_leaves_no_temporary_file(sch):
    save_file(sch, NEW)
    assert sch.read_bytes() == NEW
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_a_new_file(tmp_path):
    path = tmp_path / "new.kicad_sch"
    save_file(path, NEW)
    assert path.read_bytes() == NEW
    assert _files(tmp_path) == ["new.kicad_sch"]


@posix
def test_a_new_file_gets_the_default_permissions(tmp_path):
    umask = os.umask(0o022)
    try:
        save_file(tmp_path / "new.kicad_sch", NEW)
    finally:
        os.umask(umask)
    assert stat.S_IMODE((tmp_path / "new.kicad_sch").stat().st_mode) == 0o644


@posix
def test_the_file_keeps_its_permissions(sch):
    sch.chmod(0o640)
    save_file(sch, NEW)
    assert stat.S_IMODE(sch.stat().st_mode) == 0o640


@posix
def test_a_symbolic_link_stays_a_link(tmp_path, sch):
    link = tmp_path / "link.kicad_sch"
    link.symlink_to(sch.name)
    save_file(link, NEW)
    assert link.is_symlink()
    assert sch.read_bytes() == NEW
    assert _files(tmp_path) == ["a.kicad_sch", "link.kicad_sch"]


@posix
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes each file")
def test_a_read_only_file_is_refused(sch):
    # KiCad does not save a file that it cannot write (EDA_BASE_FRAME::IsWritable).
    # A rename needs only a folder that is writable: with no check, the save
    # would replace the file.
    sch.chmod(0o444)
    with pytest.raises(PermissionError, match="is read only"):
        save_file(sch, NEW)
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_an_error_of_the_write_keeps_the_old_file(monkeypatch, sch):
    real_write = os.write

    def full_disk(fd, data):
        real_write(fd, bytes(data[:5]))  # a part of the content, then a full disk
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(file_save.os, "write", full_disk)
    with pytest.raises(OSError, match="No space left"):
        save_file(sch, NEW)
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_an_error_of_the_flush_keeps_the_old_file(monkeypatch, sch):
    def no_flush(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(file_save, "_flush", no_flush)
    with pytest.raises(OSError, match="Input/output error"):
        save_file(sch, NEW)
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_an_error_of_the_close_keeps_the_old_file(monkeypatch, sch):
    # A network volume or a quota can give the error of the write at the close.
    real_close = os.close

    def close(fd):
        real_close(fd)
        raise OSError(errno.EDQUOT, "Disk quota exceeded")

    monkeypatch.setattr(file_save.os, "close", close)
    with pytest.raises(OSError, match="quota"):
        save_file(sch, NEW)
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_an_error_of_the_rename_keeps_the_old_file(monkeypatch, sch):
    def rename(temp, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(file_save, "_rename", rename)
    with pytest.raises(OSError, match="cross-device"):
        save_file(sch, NEW)
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_a_temporary_file_that_cannot_be_removed_is_in_the_error(monkeypatch, sch):
    def rename(temp, target):
        raise OSError(errno.EIO, "rename failed")

    def unlink(self, missing_ok=False):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(file_save, "_rename", rename)
    monkeypatch.setattr(file_save.Path, "unlink", unlink)
    with pytest.raises(OSError, match="could not remove the temporary file .*Read-only") as e:
        save_file(sch, NEW)
    assert "rename failed" in str(e.value)


@posix
def test_an_error_of_the_folder_flush_is_an_error(monkeypatch, sch):
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(file_save.os, "fsync", fsync)
    with pytest.raises(AfterSaveError, match="wrote .*a.kicad_sch.*power loss"):
        save_file(sch, NEW)
    assert sch.read_bytes() == NEW
    assert _files(sch.parent) == ["a.kicad_sch"]


@posix
@pytest.mark.parametrize("code", [errno.EINVAL, errno.ENOTSUP])
def test_a_folder_that_cannot_be_flushed_is_not_an_error(monkeypatch, sch, code):
    # A network or FUSE volume (KIPLATFORM::IO::FlushDirectory).
    real_open = os.open

    def open_(path, flags, *args):
        if flags & getattr(os, "O_DIRECTORY", 0):
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, *args)

    monkeypatch.setattr(file_save.os, "open", open_)
    save_file(sch, NEW)
    assert sch.read_bytes() == NEW


def test_a_used_name_gets_the_next_name(monkeypatch, sch):
    counter = iter([0, 0, 1])
    monkeypatch.setattr(file_save, "_COUNTER", counter)
    (sch.parent / f"a.kicad_sch.kicad-save-{os.getpid()}-0").write_bytes(b"other")
    save_file(sch, NEW)
    assert sch.read_bytes() == NEW
    assert (sch.parent / f"a.kicad_sch.kicad-save-{os.getpid()}-0").read_bytes() == b"other"


def test_discard_keeps_the_old_file(sch):
    pending = PendingSave(sch, NEW)
    assert pending.temp.read_bytes() == NEW
    pending.discard()
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


def test_write_kicad_text_refuses_a_lone_surrogate_before_a_file_is_made(sch):
    with pytest.raises(InputError, match="not valid text"):
        write_kicad_text(sch, '(kicad_sch (title "\udcff"))\n')
    assert sch.read_bytes() == OLD
    assert _files(sch.parent) == ["a.kicad_sch"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_windows_a_hidden_file_stays_hidden(sch):
    from kicad_workbench.utils import _file_save_windows as windows

    windows.set_attributes(sch, add=stat.FILE_ATTRIBUTE_HIDDEN)
    save_file(sch, NEW)
    assert sch.read_bytes() == NEW
    assert os.stat(sch).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
    assert _files(sch.parent) == ["a.kicad_sch"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rename")
def test_windows_a_lock_of_an_other_program_gets_more_attempts(monkeypatch, sch):
    from kicad_workbench.utils import _file_save_windows as windows

    real_move = windows.move_file
    calls = []

    def move_file(source, target):
        calls.append(source)
        if len(calls) < 3:
            # ERROR_SHARING_VIOLATION: an antivirus has the file open.
            raise OSError(None, "The process cannot access the file", None, 32)
        real_move(source, target)

    monkeypatch.setattr(windows, "move_file", move_file)
    save_file(sch, NEW)
    assert len(calls) == 3
    assert sch.read_bytes() == NEW


@posix
def test_a_link_to_no_file_becomes_a_file(tmp_path):
    # KiCad: realpath fails, thus the save goes to the name of the link, and the
    # rename replaces the link (KIPLATFORM::IO::ResolveSymlinkTarget).
    link = tmp_path / "link.kicad_sch"
    link.symlink_to(tmp_path / "missing" / "a.kicad_sch")
    save_file(link, NEW)
    assert not link.is_symlink()
    assert link.read_bytes() == NEW


@posix
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes each file")
def test_a_read_only_folder_is_refused_with_its_name(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    sch = folder / "a.kicad_sch"
    sch.write_bytes(OLD)
    folder.chmod(0o555)
    try:
        with pytest.raises(PermissionError, match="folder .*project is read only"):
            save_file(sch, NEW)
    finally:
        folder.chmod(0o755)
    assert sch.read_bytes() == OLD
    assert _files(folder) == ["a.kicad_sch"]


def test_a_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError, match="folder .*missing does not exist"):
        save_file(tmp_path / "missing" / "a.kicad_sch", NEW)


class _Windows:
    """The calls of _file_save_windows, for the tests of the rename on each system."""

    def __init__(self, move_errors, replace_error=None):
        self.move_errors = list(move_errors)
        self.replace_error = replace_error
        self.moves = 0
        self.replaced = []

    def move_file(self, source, target):
        self.moves += 1
        if self.move_errors:
            error = OSError(errno.EACCES, "move failed")
            error.winerror = self.move_errors.pop(0)  # as ctypes.WinError
            raise error
        os.replace(source, target)

    def replace_file(self, target, source, backup):
        if self.replace_error:
            raise OSError(errno.EACCES, self.replace_error)
        self.replaced.append(backup)
        os.replace(target, backup)
        os.replace(source, target)


@pytest.fixture
def windows(monkeypatch):
    def use(*args, **kwargs):
        calls = _Windows(*args, **kwargs)
        monkeypatch.setattr(file_save.sys, "platform", "win32")
        monkeypatch.setattr(file_save, "_windows", calls, raising=False)
        monkeypatch.setattr(file_save.time, "sleep", lambda seconds: None)
        return calls

    return use


def test_windows_rename_tries_again_after_a_lock(windows, sch):
    calls = windows([32, 33])
    file_save._rename(PendingSave(sch, NEW).temp, sch)
    assert calls.moves == 3 and calls.replaced == []
    assert sch.read_bytes() == NEW


def test_windows_replace_file_after_the_last_lock(windows, sch):
    # MoveFileEx fails 10 times: then ReplaceFileW (KiCad), with a backup file.
    calls = windows([32] * 10)
    pending = PendingSave(sch, NEW)
    backup = file_save._rename(pending.temp, sch)
    assert calls.moves == 10 and calls.replaced == [backup]
    assert sch.read_bytes() == NEW
    assert backup.read_bytes() == OLD  # commit() removes it


def test_windows_replace_file_after_an_other_error(windows, sch):
    calls = windows([2])  # ERROR_FILE_NOT_FOUND: no lock, thus no more attempts
    file_save._rename(PendingSave(sch, NEW).temp, sch)
    assert calls.moves == 1 and len(calls.replaced) == 1


def test_windows_two_errors_keep_the_old_file(windows, sch):
    windows([2], replace_error="replace failed")
    pending = PendingSave(sch, NEW)
    with pytest.raises(OSError, match="move failed.*ReplaceFileW also failed: .*replace failed"):
        file_save._rename(pending.temp, sch)
    assert sch.read_bytes() == OLD


def test_windows_no_replace_file_for_a_new_file(windows, tmp_path):
    calls = windows([2])
    path = tmp_path / "new.kicad_sch"
    with pytest.raises(OSError, match="move failed"):
        file_save._rename(PendingSave(path, NEW).temp, path)
    assert calls.replaced == []


def test_a_backup_file_that_stays_is_an_error_after_the_save(monkeypatch, sch):
    def rename(temp, target):
        backup = target.with_name("backup")
        os.replace(target, backup)
        os.replace(temp, target)
        return backup

    def unlink(self, missing_ok=False):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(file_save, "_rename", rename)
    pending = PendingSave(sch, NEW)
    monkeypatch.setattr(file_save.Path, "unlink", unlink)
    with pytest.raises(AfterSaveError, match="wrote .*backup file .*old content"):
        pending.commit()
    monkeypatch.undo()
    assert sch.read_bytes() == NEW
    assert (sch.parent / "backup").read_bytes() == OLD


@posix
def test_save_files_refuses_two_names_of_one_file(tmp_path, sch):
    link = tmp_path / "link.kicad_sch"
    link.symlink_to(sch.name)
    with pytest.raises(file_save.SaveFilesError, match="are one file"):
        file_save.save_files([(sch, NEW), (link, NEW + b" ")])
    assert sch.read_bytes() == OLD
    assert _files(tmp_path) == ["a.kicad_sch", "link.kicad_sch"]


def test_save_files_removes_a_new_file_after_a_failed_rename(monkeypatch, tmp_path, sch):
    new = tmp_path / "b.kicad_sch"
    real, calls = file_save._rename, []

    def rename(temp, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError(errno.EIO, "Input/output error")
        return real(temp, target)

    monkeypatch.setattr(file_save, "_rename", rename)
    with pytest.raises(
        file_save.SaveFilesError, match=r"wrote no file \(it removed the new file b.kicad_sch\)"
    ):
        file_save.save_files([(new, NEW), (sch, NEW)])
    assert _files(tmp_path) == ["a.kicad_sch"]
    assert sch.read_bytes() == OLD


@pytest.mark.parametrize("same_object", [True, False])
def test_save_files_refuses_one_name_two_times(tmp_path, sch, same_object):
    other = sch if same_object else tmp_path / "a.kicad_sch"
    with pytest.raises(file_save.SaveFilesError, match="are one file"):
        file_save.save_files([(sch, NEW), (other, NEW + b" ")])
    assert sch.read_bytes() == OLD


@posix
def test_save_files_replaces_a_link_loop_as_save_file(tmp_path, sch):
    # realpath fails for a link to itself: the save goes to the name of the link,
    # as KiCad saves it (KIPLATFORM::IO::ResolveSymlinkTarget), also in a set.
    loop = tmp_path / "loop.kicad_sch"
    loop.symlink_to(loop.name)
    file_save.save_files([(sch, NEW), (loop, NEW)])
    assert not loop.is_symlink() and loop.read_bytes() == NEW


def test_the_table_of_the_locks_stays_empty(tmp_path, sch):
    file_save.save_files([(sch, NEW), (tmp_path / "b.kicad_sch", NEW)])
    file_save.save_file(sch, OLD)
    assert file_save._LOCKS == {}


@posix
def test_save_files_gives_a_link_to_no_file_back(monkeypatch, tmp_path, sch):
    # The save replaces a link to no file with a file (as KiCad). After a failed
    # rename of the next file, the link comes back.
    link = tmp_path / "link.kicad_sch"
    link.symlink_to(tmp_path / "missing.kicad_sch")
    real, calls = file_save._rename, []

    def rename(temp, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError(errno.EIO, "Input/output error")
        return real(temp, target)

    monkeypatch.setattr(file_save, "_rename", rename)
    with pytest.raises(file_save.SaveFilesError, match=r"gave link.kicad_sch the old content"):
        file_save.save_files([(link, NEW), (sch, NEW)])
    assert link.is_symlink() and os.readlink(link) == str(tmp_path / "missing.kicad_sch")
    assert sch.read_bytes() == OLD


@posix
def test_save_files_flushes_each_folder_one_time(monkeypatch, tmp_path, sch):
    flushed = []
    monkeypatch.setattr(file_save, "_flush_folder", lambda folder, written: flushed.append(folder))
    file_save.save_files([(sch, NEW), (tmp_path / "b.kicad_sch", NEW)])
    assert flushed == [tmp_path]


def test_save_files_reads_no_old_content_of_one_file(monkeypatch, sch):
    # Only a file before a failed rename gets its old content again: the last file
    # never. A file that the tool can write but not read is then no error.
    monkeypatch.setattr(file_save, "_old_state", lambda path: pytest.fail("read"))
    assert file_save.save_files([(sch, NEW)]) == []
    assert sch.read_bytes() == NEW


def test_save_files_gives_a_defect_as_it_is(monkeypatch, tmp_path, sch):
    # A defect of the code is no SaveFilesError (an expected error with no
    # traceback): it goes up after the restore.
    real, calls = file_save._rename, []

    def rename(temp, target):
        calls.append(target)
        if len(calls) == 2:
            raise TypeError("a defect")
        return real(temp, target)

    monkeypatch.setattr(file_save, "_rename", rename)
    with pytest.raises(TypeError, match="a defect"):
        file_save.save_files([(tmp_path / "b.kicad_sch", NEW), (sch, NEW)])
    assert _files(tmp_path) == ["a.kicad_sch"]


def test_a_failed_restore_tells_also_the_restored_files(monkeypatch, tmp_path, sch):
    real_rename, calls = file_save._rename, []

    def rename(temp, target):
        calls.append(target)
        if len(calls) == 3:
            raise OSError(errno.EIO, "Input/output error")
        return real_rename(temp, target)

    def save_file(path, data):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(file_save, "_rename", rename)
    monkeypatch.setattr(file_save, "save_file", save_file)
    new = tmp_path / "b.kicad_sch"
    with pytest.raises(file_save.SaveFilesError) as error:
        file_save.save_files([(sch, NEW), (new, NEW), (tmp_path / "c.kicad_sch", NEW)])
    text = str(error.value)
    assert "could not give these files their old content again: " in text
    assert text.endswith(
        "These files have the new content, the other files do not (it removed the new file "
        "b.kicad_sch)."
    ), text


def test_two_new_names_that_the_folder_reads_as_one(monkeypatch, tmp_path):
    # A folder that does not compare the case (Windows, macOS, FAT): the temporary
    # file of X.png is there also under the spelling x.png.
    monkeypatch.setattr(file_save.os.path, "samefile", lambda a, b: True)
    with pytest.raises(file_save.SaveFilesError, match="X.png and .*x.png are one file"):
        file_save.save_files([(tmp_path / "X.png", NEW), (tmp_path / "x.png", NEW)])
    assert _files(tmp_path) == []


@pytest.mark.skipif(sys.platform != "linux", reason="a file system that compares the case")
def test_two_new_names_that_the_folder_reads_as_two(tmp_path):
    file_save.save_files([(tmp_path / "X.png", NEW), (tmp_path / "x.png", OLD)])
    assert (tmp_path / "X.png").read_bytes() == NEW and (tmp_path / "x.png").read_bytes() == OLD
