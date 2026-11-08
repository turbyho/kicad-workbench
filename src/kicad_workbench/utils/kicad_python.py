"""Helpers for running scripts under a Python that can import ``pcbnew``.

pcbnew only ships with KiCad's own Python. The MCP server may run inside that
interpreter (the recommended install) or under a plain Python without pcbnew,
so tools that need KiCad to write files run a short script in a subprocess
using whichever interpreter has pcbnew.

Scripts must be self-contained (pass them with ``-c`` and their inputs via
argv): KiCad's bundled Python on Windows ignores PYTHONPATH because its
sitecustomize.py resets sys.path, so a script cannot import this package.
"""

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ..errors import WorkbenchError
from .kicad_cli import INSTALL_KICAD, find_kicad_cli, fs_encoding
from .kicad_version import find_kicad_install


class KicadPythonError(WorkbenchError):
    """The Python of KiCad (pcbnew) did not do the work or gave no result. The
    message tells why."""


_cached_python: str | None = None
def no_kicad_python_message() -> str:
    """Return the answer of a tool that needs pcbnew, when no Python of KiCad was found:
    the step to install KiCad, or only the step KICAD_PYTHON when KiCad is installed
    (kicad-cli was found)."""
    exe = find_kicad_cli()
    if exe is None:
        return (
            "The Python of KiCad (with the module pcbnew) was not found: KiCad is not "
            f"installed. The tool needs KiCad 8 or later. Tell the user: {INSTALL_KICAD}, "
            "or give the Python of KiCad in the environment variable KICAD_PYTHON."
        )
    return (
        f"The Python of KiCad (with the module pcbnew) was not found, but KiCad is "
        f"installed (kicad-cli {exe}). Tell the user: give the Python of KiCad (a Python "
        "that can import pcbnew) in the environment variable KICAD_PYTHON."
    )


def find_kicad_python() -> str | None:
    """Locate a Python interpreter that can import pcbnew.

    Resolution order:
    1. ``KICAD_PYTHON`` environment variable (explicit user override).
    2. The current interpreter, when pcbnew is importable in this process.
    3. KiCad's bundled interpreter next to the detected install.
    4. Linux: the system python3, which distro KiCad packages install
       pcbnew for (only when a KiCad install was detected).

    Returns the interpreter path, or None when none can be found.
    """
    global _cached_python
    if _cached_python is not None:
        return _cached_python

    override = os.environ.get("KICAD_PYTHON")
    if override and Path(override).is_file():
        _cached_python = override
        return _cached_python

    if importlib.util.find_spec("pcbnew") is not None:
        _cached_python = sys.executable
        return _cached_python

    install = find_kicad_install()
    if install:
        install_path, _version = install
        # Windows: install_path is <...>/KiCad/<version>.
        # macOS: install_path is .../Contents/SharedSupport.
        contents = install_path.parent
        candidates = [
            install_path / "bin" / "python.exe",
            contents / "Frameworks" / "python3",
            contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
        ]
        if sys.platform.startswith("linux"):
            candidates.append(Path("/usr/bin/python3"))
        for candidate in candidates:
            if candidate.is_file():
                _cached_python = str(candidate)
                return _cached_python

    return None


def run_kicad_python_sync(
    args: list[str],
    timeout: float = 120.0,
    input: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run the pcbnew-capable Python synchronously (blocking).

    ``input`` is the stdin of the child (for data that is too large for an
    argument). With no input, the child gets no stdin.

    Raises FileNotFoundError when no such interpreter can be located.
    """
    exe = find_kicad_python()
    if exe is None:
        raise FileNotFoundError(no_kicad_python_message())
    # stdin is the MCP stdio transport; the child must not inherit it.
    stdin = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
    return subprocess.run(
        [exe] + args,
        capture_output=True,
        timeout=timeout,
        env=_child_env(),
        **stdin,
    )


def _child_env() -> dict[str, str]:
    """Return the environment of the child Python.

    The child writes its stdout and stderr as UTF-8 (see decode_python_output()).
    On Linux and macOS it must also decode its arguments with the encoding
    that this process uses for them (fs_encoding()). When that encoding is
    UTF-8, the UTF-8 mode of Python does the two. Else the child keeps the
    locale encoding of this process for its arguments, and PYTHONIOENCODING
    sets only stdout and stderr. The UTF-8 mode is then off explicitly,
    because it is the default from Python 3.15 (PEP 686). On Windows the
    arguments are Unicode. PYTHONIOENCODING is set in the two cases, because
    a value from the environment of this process overrides the UTF-8 mode.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1" if fs_encoding() == "utf-8" else "0"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def decode_python_output(data: bytes | None) -> str:
    """Decode the stdout or the stderr of the child Python (UTF-8)."""
    return (data or b"").decode("utf-8", errors="replace")


def result_line(stdout: bytes | None, prefix: str) -> str | None:
    """Return the text after the prefix of the first stdout line with the prefix, or None.

    pcbnew or a site hook can write more lines, thus a script writes its result
    on a line with a prefix.
    """
    for line in decode_python_output(stdout).splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


async def run_kicad_python(
    args: list[str],
    timeout: float = 120.0,
    input: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run the pcbnew-capable Python off the event loop (see run_kicad_python_sync)."""
    return await asyncio.to_thread(run_kicad_python_sync, args, timeout, input)


# Makes a new board. NewBoard() saves the board and its project file at once.
# SaveBoard(path, board, aSkipSettings=True) saves the board again, with no
# project file. The script prints the KiCad version.
# argv: board path, title, outline width and height in mm (0: no outline).
_VERSION_PREFIX = "KICAD_VERSION="
_NEW_BOARD_SCRIPT = """\
import sys
import pcbnew

path, title = sys.argv[1:3]
width, height = float(sys.argv[3]), float(sys.argv[4])
board = pcbnew.NewBoard(path)
board.GetTitleBlock().SetTitle(title)
if width > 0 and height > 0:
    corners = [(0, 0), (width, 0), (width, height), (0, height)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
        line = pcbnew.PCB_SHAPE(board)
        line.SetShape(pcbnew.SHAPE_T_SEGMENT)
        line.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        line.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        line.SetLayer(pcbnew.Edge_Cuts)
        line.SetWidth(pcbnew.FromMM(0.15))
        board.Add(line)
if not pcbnew.SaveBoard(path, board, True):
    sys.exit("pcbnew.SaveBoard failed")
print("KICAD_VERSION=" + pcbnew.GetMajorMinorPatchVersion())
"""

_KICAD_VERSION = re.compile(r"\d+\.\d+\.\d+")


def _new_board_sync(path: Path, title: str, outline: tuple[float, float] | None) -> str:
    """Run the script, and return the KiCad version of pcbnew that it prints
    (for example "10.0.6").

    Call it in the thread that owns the folder of the board: a cancel of the
    tool does not stop pcbnew.

    Raises:
        FileNotFoundError: No Python with pcbnew is found.
        KicadPythonError: pcbnew cannot make the board, or gives no KiCad version.
        subprocess.TimeoutExpired: pcbnew did not stop in time.
    """
    width, height = outline or (0.0, 0.0)
    result = run_kicad_python_sync(
        [
            "-c",
            _NEW_BOARD_SCRIPT,
            str(path),
            title,
            repr(float(width)),
            repr(float(height)),
        ]
    )
    if result.returncode != 0:
        raise KicadPythonError(
            "pcbnew could not create the board:\n" + decode_python_output(result.stderr).strip()
        )
    line = result_line(result.stdout, _VERSION_PREFIX)
    version = line.strip() if line is not None else ""
    if not _KICAD_VERSION.fullmatch(version):
        raise KicadPythonError(f"pcbnew did not give the KiCad version: {version!r}")
    return version


def _board_text_sync(file_name: str, title: str, outline: tuple[float, float]) -> tuple[str, str]:
    # The thread owns the temporary directory: a cancel of the tool does not stop
    # pcbnew, and the directory goes only after pcbnew stops.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / file_name
        version = _new_board_sync(path, title, outline)
        return path.read_text(encoding="utf-8"), version


async def board_text(file_name: str, title: str, outline: tuple[float, float]) -> tuple[str, str]:
    """Make a board with pcbnew of KiCad, and return its text and the KiCad version.

    The board has a title and a rectangle (width, height) in mm on Edge.Cuts, in
    the file format of that KiCad. pcbnew makes it in a temporary directory,
    because NewBoard() also saves a project file. No file of the user changes.

    Raises:
        FileNotFoundError: No Python with pcbnew is found.
        KicadPythonError: pcbnew cannot make the board, or gives no KiCad version.
        subprocess.TimeoutExpired: pcbnew did not stop in time.
    """
    return await asyncio.to_thread(_board_text_sync, file_name, title, outline)
