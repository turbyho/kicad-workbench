"""KiCad installation detection and version utilities."""

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


def get_kicad_share_dir() -> Path | None:
    """Get the data directory of the KiCad installation ("share/kicad").

    It holds the stock libraries ("symbols", "footprints", "3dmodels") and
    "template".
    """
    kicad = find_kicad_install()
    if kicad:
        install_path, version_marker = kicad
        # find_kicad_install() returns different path shapes per platform:
        # Windows returns the top-level "<...>/KiCad/<version>" dir, with
        # the data nested under "share/kicad". Linux/macOS instead already
        # return the "share/kicad" (or SharedSupport) directory itself.
        if version_marker in ("linux", "macos"):
            return install_path
        return install_path / "share" / "kicad"
    return None

