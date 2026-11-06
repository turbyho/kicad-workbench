"""The Windows calls of file_save: the DACL, the rename and the attributes of a file.

Python has no interface for these calls (os.replace uses MoveFileExW with no
MOVEFILE_WRITE_THROUGH, os.chmod changes only FILE_ATTRIBUTE_READONLY).
"""

import ctypes
from ctypes import wintypes
from pathlib import Path

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_GetFileSecurityW = _advapi32.GetFileSecurityW
_GetFileSecurityW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_GetFileSecurityW.restype = wintypes.BOOL

_SetFileSecurityW = _advapi32.SetFileSecurityW
_SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
_SetFileSecurityW.restype = wintypes.BOOL

_MoveFileExW = _kernel32.MoveFileExW
_MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_MoveFileExW.restype = wintypes.BOOL

_ReplaceFileW = _kernel32.ReplaceFileW
_ReplaceFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_ReplaceFileW.restype = wintypes.BOOL

_GetFileAttributesW = _kernel32.GetFileAttributesW
_GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_GetFileAttributesW.restype = wintypes.DWORD

_SetFileAttributesW = _kernel32.SetFileAttributesW
_SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_SetFileAttributesW.restype = wintypes.BOOL

_DACL_SECURITY_INFORMATION = 0x4
_ERROR_INSUFFICIENT_BUFFER = 122
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8
_ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 = 1177
_REPLACEFILE_WRITE_THROUGH = 0x1
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_FILE_ATTRIBUTE_NORMAL = 0x80


def copy_dacl(source: Path, target: Path) -> None:
    """Copy the DACL of the file to the temporary file (KiCad: DuplicatePermissions).
    Only the DACL: a copy of the owner needs a privilege of the administrator. A
    volume with no ACL (FAT, a cloud drive) refuses the copy: the new file then
    has the DACL of the folder, as in KiCad."""
    size = wintypes.DWORD(0)
    _GetFileSecurityW(str(source), _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(size))
    if size.value == 0 or ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
        return
    descriptor = ctypes.create_string_buffer(size.value)
    if _GetFileSecurityW(
        str(source), _DACL_SECURITY_INFORMATION, descriptor, size, ctypes.byref(size)
    ):
        _SetFileSecurityW(str(target), _DACL_SECURITY_INFORMATION, descriptor)


def move_file(source: Path, target: Path) -> None:
    """Rename the file over the target, and return after the disk has the rename.

    Raises:
        OSError: The rename failed (the winerror attribute has the Windows error).
    """
    flags = _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH
    if not _MoveFileExW(str(source), str(target), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def replace_file(target: Path, source: Path, backup: Path) -> None:
    """Replace the file with ReplaceFileW. The old file goes to the backup file. The
    new file keeps the DACL, the attributes and the creation time of the old file.

    After the errors of ReplaceFileW (its documentation), the file keeps its old
    content: ERROR_UNABLE_TO_MOVE_REPLACEMENT, ERROR_UNABLE_TO_REMOVE_REPLACED and
    the other errors keep the names of the files. After
    ERROR_UNABLE_TO_MOVE_REPLACEMENT_2, the old file has the name of the backup
    file: the function renames it back.

    Raises:
        OSError: ReplaceFileW failed. The file has its old content, or (when the
            rename back also failed) the message gives the backup file.
    """
    # REPLACEFILE_WRITE_THROUGH, as KiCad gives it (AtomicRename).
    flags = _REPLACEFILE_WRITE_THROUGH
    if _ReplaceFileW(str(target), str(source), str(backup), flags, None, None):
        return
    code = ctypes.get_last_error()
    error = ctypes.WinError(code)
    if code != _ERROR_UNABLE_TO_MOVE_REPLACEMENT_2:
        raise error
    if not _MoveFileExW(str(backup), str(target), _MOVEFILE_WRITE_THROUGH):
        raise OSError(
            f"{error} The old content of {target} is in {backup}: the tool could not "
            f"rename it back ({ctypes.WinError(ctypes.get_last_error())})."
        ) from error
    raise error


def set_attributes(path: Path, add: int = 0, remove: int = 0) -> None:
    """Add or remove file attributes. A failure is not an error: KiCad also saves
    the file when it cannot change the attributes."""
    current = _GetFileAttributesW(str(path))
    if current == _INVALID_FILE_ATTRIBUTES:
        return
    changed = (current | add) & ~remove
    if changed != current:
        # No attribute: FILE_ATTRIBUTE_NORMAL, the value of a file with no attribute.
        _SetFileAttributesW(str(path), changed or _FILE_ATTRIBUTE_NORMAL)
