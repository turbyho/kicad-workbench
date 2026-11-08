"""KiCad installation detection and version utilities."""

import os
import sys
from pathlib import Path

# The application of macOS. The folder of the KiCad installations of Windows is
# "KiCad" in the Program Files folder of the system (windows_kicad_folder).
MACOS_KICAD = Path("/Applications/KiCad/KiCad.app")


def windows_kicad_folder() -> Path:
    """Return the folder of the KiCad installations of Windows: "KiCad" in the folder of
    the environment variable ProgramFiles (the default folder of the installer of KiCad),
    else in "C:/Program Files"."""
    return Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "KiCad"


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse version string like '10.0' into tuple for sorting."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


def standard_kicad_cli(platform: str | None = None) -> Path | None:
    """Return the kicad-cli of the standard install folder of KiCad, or None.

    - Windows: "bin/kicad-cli.exe" of the newest version folder in
      windows_kicad_folder that has it. A version folder with no kicad-cli (a
      removed or a partial installation) does not count.
    - macOS: "Contents/MacOS/kicad-cli" of "/Applications/KiCad/KiCad.app".
    - Linux: none. The package of the distribution puts kicad-cli into PATH.

    Args:
        platform: The value of sys.platform (default: the system).
    """
    platform = platform or sys.platform
    if platform == "win32":
        try:
            folders = sorted(
                (d for d in windows_kicad_folder().iterdir() if d.is_dir()),
                key=lambda d: _parse_version(d.name),
                reverse=True,
            )
        except OSError:
            return None
        for folder in folders:
            cli = folder / "bin" / "kicad-cli.exe"
            if cli.is_file():
                return cli
        return None
    if platform == "darwin":
        cli = MACOS_KICAD / "Contents" / "MacOS" / "kicad-cli"
        return cli if cli.is_file() else None
    return None


def kicad_python_of(cli: Path, platform: str | None = None) -> list[Path]:
    """Return the places of the Python of KiCad in the installation of a kicad-cli,
    in the order of the search.

    - Windows: "python.exe" in the folder of kicad-cli ("<install>/bin").
    - macOS: the Python of the frameworks of the application bundle.
    - Linux: "python3" next to kicad-cli, for a kicad-cli "<prefix>/bin/kicad-cli"
      (symbolic links resolved): the KiCad package of the distribution installs
      pcbnew for the Python of its prefix ("/usr/bin/python3" for
      "/usr/bin/kicad-cli"). For a different program (for example a start script
      "kicad-cli-nightly") the Python is not known: the user gives it in
      KICAD_PYTHON.

    Args:
        platform: The value of sys.platform (default: the system).
    """
    platform = platform or sys.platform
    if platform == "win32":
        return [Path(os.path.abspath(cli)).parent / "python.exe"]
    if platform == "darwin":
        frameworks = _osx_bundle_root(cli) / "Contents" / "Frameworks"
        return [
            frameworks / "python3",
            frameworks / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
        ]
    real = cli.resolve()
    if real.name != "kicad-cli" or real.parent.name != "bin":
        return []
    return [real.parent / "python3"]


def _osx_bundle_root(program: Path) -> Path:
    """The root of the application bundle of a program (getOSXBundleRoot of
    KiCad 10, common/paths.cpp): the folder of the program with symbolic links
    resolved, up from ".../Contents/MacOS", and up from the bundle of an
    auxiliary program (".../main.app/Contents/Applications/standalone.app")."""
    folder = program.resolve().parent
    if len(folder.parts) >= 3 and folder.name == "MacOS":
        folder = folder.parent.parent
    parts = folder.parts
    if len(parts) >= 5 and parts[-2] == "Applications" and parts[-4].lower().endswith("app"):
        folder = folder.parent.parent.parent
    return folder


def stock_library_dir(cli: str | None) -> Path | None:
    """Return the stock library folder of the KiCad installation of a kicad-cli,
    or None when the folder is not known.

    The folder holds "symbols", "footprints", "blocks", "3dmodels" and
    "template": the defaults of KICAD<major>_SYMBOL_DIR and the other path
    variables of the libraries. The rules are those of
    PATHS::GetStockEDALibraryPath of KiCad 10 (common/paths.cpp):

    - Windows: GetStockDataPath, that is KICAD_STOCK_DATA_HOME when it has a
      value, else "share/kicad" in the folder above the folder of the program
      (getWindowsKiCadRoot).
    - macOS: "Contents/SharedSupport" of the application bundle of the program
      (GetOSXKicadMachineDataDir).
    - Linux: KICAD_LIBRARY_DATA, a path that the build of KiCad sets. kicad-cli
      does not give it. By default the build installs the program as
      "<prefix>/bin/kicad-cli" and the data in "<prefix>/share/kicad" (the
      default CMAKE_INSTALL_BINDIR and CMAKE_INSTALL_DATADIR of CMakeLists.txt).
      The function gives that folder for a program with these names. A
      different program (for example the start script "kicad-cli-nightly" of
      a second installation) has a folder that is not known.

    The folder must exist. On Linux it must also hold the folder "template",
    which each installation of KiCad has (the project template of KiCad): a
    folder "share/kicad" of an other program does not count.
    """
    if cli is None:
        return None
    program = Path(cli)
    if sys.platform == "win32":
        override = os.environ.get("KICAD_STOCK_DATA_HOME")
        folder = Path(override) if override else _windows_share_dir(program)
    elif sys.platform == "darwin":
        folder = _osx_bundle_root(program) / "Contents" / "SharedSupport"
    else:
        real = program.resolve()
        if real.name != "kicad-cli" or real.parent.name != "bin":
            return None
        folder = real.parent.parent / "share" / "kicad"
        if not (folder / "template").is_dir():
            return None
    return folder if folder.is_dir() else None


def _windows_share_dir(program: Path) -> Path:
    """The folder "share/kicad" in the folder above the folder of a program: the
    root of the installation (getWindowsKiCadRoot of KiCad 10), then "share/kicad"."""
    return Path(os.path.abspath(program)).parent.parent / "share" / "kicad"


def system_data_dir(cli: str | None) -> Path | None:
    """Return the data folder of the KiCad installation of a kicad-cli in the
    system folders that KiCad searches (SystemDirsAppend of KiCad 10,
    common/systemdirsappend.cpp), or None when it is not known.

    Linux: GetStockEDALibraryPath; macOS: GetOSXKicadMachineDataDir. These are
    the folder of stock_library_dir. Windows: the folder above the folder of
    the program, with "share/kicad" (SystemDirsAppend adds it to each base),
    also when KICAD_STOCK_DATA_HOME has a value. The folder must exist.
    """
    if cli is None:
        return None
    if sys.platform == "win32":
        folder = _windows_share_dir(Path(cli))
        return folder if folder.is_dir() else None
    return stock_library_dir(cli)
