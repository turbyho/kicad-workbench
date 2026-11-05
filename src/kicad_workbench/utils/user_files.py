"""The files of the server in the cache directory of the user."""

import os
import sys
import tempfile
from pathlib import Path


def cache_home() -> Path:
    """The cache directory of kicad-workbench, in the cache directory of the user."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "kicad-workbench"


def write_atomic(file: Path, text: str) -> None:
    """Write a UTF-8 text file in one step: a reader gets the old or the new file,
    never a part. A unique temporary file, because a different server process can
    write the same file. After an error (for example a full disk at the write or
    at the flush of the close), the temporary file goes and the old file stays.
    The errors (OSError) go to the caller."""
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=file.parent, suffix=".tmp", delete=False
    )
    try:
        with temp:
            temp.write(text)
        os.replace(temp.name, file)
    except BaseException:
        os.unlink(temp.name)
        raise
