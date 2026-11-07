"""KiCad installation detection and version utilities."""

import os
import sys
from pathlib import Path

_cached_install: tuple[Path, str] | None = None


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse version string like '10.0' into tuple for sorting."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


def find_kicad_install() -> tuple[Path, str] | None:
    """Find the installed KiCad directory and version string.

    Scans known install locations, preferring the newest version.

    Returns:
        (install_path, version_string) or None if not found
    """
    global _cached_install
    if _cached_install is not None:
        return _cached_install

    # Windows: C:/Program Files/KiCad/{version}/
    win_base = Path("C:/Program Files/KiCad")
    if win_base.is_dir():
        try:
            version_dirs = sorted(
                [d for d in win_base.iterdir() if d.is_dir()],
                key=lambda d: _parse_version(d.name),
                reverse=True,
            )
            for vd in version_dirs:
                if (vd / "share" / "kicad").is_dir():
                    _cached_install = (vd, vd.name)
                    return _cached_install
        except OSError:
            pass

    # macOS: single app bundle
    mac_base = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")
    if mac_base.is_dir():
        _cached_install = (mac_base, "macos")
        return _cached_install

    # Linux: standard paths
    for linux_base in [
        Path("/usr/share/kicad"),
        Path("/usr/local/share/kicad"),
    ]:
        if linux_base.is_dir():
            _cached_install = (linux_base, "linux")
            return _cached_install

    return None


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
