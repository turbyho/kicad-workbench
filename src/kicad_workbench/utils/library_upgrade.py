"""The upgrade of a KiCad library to the version of the installed KiCad, through
kicad-cli, with a check of the result.

KiCad does the conversion (rule 2 of CLAUDE.md): ``kicad-cli sym upgrade`` and
``kicad-cli fp upgrade`` (EESCHEMA_JOBS_HANDLER::JobSymUpgrade,
PCBNEW_JOBS_HANDLER::JobFootprintUpgrade, tag 10.0.6) load the library and save it in
the version of the installed KiCad. The functions write into a temporary folder; the
caller writes the files into the library after the check.

The check (user decision, 2026-11-16): KiCad saves the converted library again
(``upgrade --force``), and the bytes must be equal. Then KiCad reads from the new files
all that it read from the old files and saves. The names of the symbols or the
footprints must be equal too. The check cannot find an error of the reader of KiCad for
the old version: the symbol editor and the footprint editor do the same conversion at
their next save.
"""

import os
import subprocess
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.sexpr import SExprError, parse
from .kicad_cli import run_kicad_cli_sync
from .kicad_text import decode_as_kicad

# The time for one kicad-cli run: a large library takes some seconds.
_TIMEOUT = 600.0
# FILEEXT::KiCadFootprintFileExtension: FP_CACHE::Load reads "*.kicad_mod".
FOOTPRINT_FILE = ".kicad_mod"


class UpgradeError(WorkbenchError):
    """kicad-cli could not convert the library, or the check of the result failed."""


def footprint_files(folder: Path) -> list[Path]:
    """The files that FP_CACHE::Load reads: wxDir::GetFirst with the mask "*.kicad_mod"
    (hidden files too; wxMatchWild: the case of the extension matters, except on
    Windows)."""
    files = []
    for entry in folder.iterdir():
        name = entry.name if os.name != "nt" else entry.name.lower()
        if entry.is_file() and name.endswith(FOOTPRINT_FILE):
            files.append(entry)
    return sorted(files)


def symbol_names(data: bytes, path: Path) -> list[str]:
    """The names of the symbols of a .kicad_sym file (the "symbol" items of the root).

    Raises:
        UpgradeError: The file cannot be read.
    """
    try:
        root = parse(decode_as_kicad(data, path))
    except (SExprError, WorkbenchError) as e:
        raise UpgradeError(f"The symbol library {path} cannot be read: {e}") from e
    return sorted(node.value(0, "") for node in root.children("symbol"))


def _upgrade(kind: str, source: Path, output: Path, force: bool) -> None:
    """Run ``kicad-cli sym upgrade`` or ``kicad-cli fp upgrade`` into output (a path that
    must not exist).

    Raises:
        UpgradeError: kicad-cli gave an error, or it wrote no output.
    """
    args = ["sym" if kind == "symbol" else "fp", "upgrade"]
    if force:
        args.append("--force")
    args += ["-o", str(output), str(source)]
    try:
        result = run_kicad_cli_sync(args, timeout=_TIMEOUT, english=True)
    except subprocess.TimeoutExpired as e:
        raise UpgradeError(f"kicad-cli did not end in {_TIMEOUT:.0f} s for {source}.") from e
    if result.returncode != 0 or not output.exists():
        text = " ".join(((result.stdout or "") + " " + (result.stderr or "")).split())
        raise UpgradeError(
            f"kicad-cli {' '.join(args[:2])} of {source} failed (exit code "
            f"{result.returncode}): {text or 'no message'}"
        )


def convert(kind: str, library: Path, work: Path, names: set[str]) -> dict[Path, bytes]:
    """Convert a library with kicad-cli in the folder work, and check the result.

    Args:
        kind: "symbol" (a .kicad_sym file) or "footprint" (a .pretty folder).
        library: The library.
        work: An empty temporary folder that the caller owns.
        names: The names that the caller read: the symbols of the file, or the
            footprint files of the folder (footprint_files). The converted library must
            have the same names.

    Returns:
        For each file of the library, its new bytes.

    Raises:
        UpgradeError: kicad-cli failed, or the check found a difference.
    """
    first, second = work / "upgrade", work / "check"
    first.mkdir()
    second.mkdir()
    out, again = first / library.name, second / library.name
    _upgrade(kind, library, out, force=False)
    _upgrade(kind, out, again, force=True)
    if kind == "symbol":
        new_data = out.read_bytes()
        if set(symbol_names(new_data, out)) != names:
            raise UpgradeError(
                f"The converted library {library} has other symbols than the library: "
                "the tool wrote no file."
            )
        if again.read_bytes() != new_data:
            raise UpgradeError(
                f"KiCad saved the converted library {library} again with other bytes: "
                "the conversion is not stable. The tool wrote no file."
            )
        return {library: new_data}
    new = {file.name: file.read_bytes() for file in footprint_files(out)}
    if set(new) != names:
        missing = sorted(names - set(new))
        extra = sorted(set(new) - names)
        raise UpgradeError(
            f"The converted library {library} has other footprints than the library "
            f"(missing: {missing or 'none'}, new: {extra or 'none'}): the tool wrote no file."
        )
    changed = sorted(
        name
        for name, data in new.items()
        if not (again / name).is_file() or (again / name).read_bytes() != data
    )
    if changed:
        raise UpgradeError(
            f"KiCad saved the converted footprints of {library} again with other bytes: "
            f"{', '.join(changed[:5])}. The conversion is not stable. The tool wrote no file."
        )
    return {library / name: data for name, data in new.items()}


def git_note(library: Path) -> str | None:
    """A note when git cannot give the old files of a library back: a file that is not in
    git (also an ignored file), or a file with changes that are not committed. None when
    git has each file with no change.

    The pathspec is the library (a file or a folder), with no symbolic links in the
    path: git refuses a path through a link out of the repository.
    """
    real = Path(os.path.realpath(library))
    folder = real.parent
    try:
        status = subprocess.run(
            ["git", "-C", str(folder), "status", "--porcelain", "--ignored",
             "--untracked-files=all", "--", real.name],
            capture_output=True,
            text=True,
            check=False,
        )  # fmt: skip
    except OSError as e:
        return f"git cannot run ({e}): no way back to the old files"
    if status.returncode != 0:
        return "it is not in a git repository: git cannot give the old files back"
    codes = {line[:2] for line in status.stdout.splitlines() if line}
    if codes & {"??", "!!"}:
        return "not all its files are in git: git cannot give the old files back"
    if codes:
        return "it has changes that are not committed: git cannot give these changes back"
    return None
