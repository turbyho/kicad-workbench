"""Sheet hierarchy of KiCad projects.

A KiCad project has one root schematic: the .kicad_sch file with the same
name as the .kicad_pro file, in the same directory. The root schematic holds
sheet symbols, and each sheet symbol refers to a sub-sheet file through its
sheet file field. A sub-sheet can hold more sheet symbols. KiCad 10 can have
more than one top-level sheet (top_level_sheets of the .kicad_pro).

This module finds the root schematic of a sub-sheet. It does not guess from
the directory layout only: it reads the sheet tree of each candidate root,
and it accepts a root only when the tree holds the file. It reads the sheet
fields with the rules of the file version (kicad_fields.sheet_fields).

Limit: the candidate roots are the projects in the directory of the file and
in each parent directory. A sub-sheet outside the directory tree of its
project (for example ``../common/power.kicad_sch``) has no link back to its
project in the file system, thus this module cannot find its root.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.kicad_versions import version_triple
from ..parsers.sheet_list import SheetList, build_sheet_list, kiid_text, screens_by_file
from .kicad_project import (
    PRO_SUFFIX,
    SCH_SUFFIX,
    TopLevelSheet,
    is_declared,
    same_file,
    text_variables,
    top_level_sheets,
)

PRO_LOCK_SUFFIX = PRO_SUFFIX + ".lck"


class AmbiguousRootError(WorkbenchError):
    """A sub-sheet is part of more than one root schematic."""

    def __init__(self, sheet: Path, roots: list[Path]):
        self.sheet = sheet
        self.roots = roots
        names = ", ".join(str(root) for root in roots)
        super().__init__(f"{sheet} is a sub-sheet of more than one root schematic: {names}")


@dataclass(frozen=True)
class SheetPath:
    """One place of a schematic file in the sheet hierarchy of a project."""

    # The file of the screen. None: an empty sheet (KiCad has the sheet path, with no
    # file: see sheet_list.EMPTY_REASONS).
    file: Path | None
    # The top-level sheet, then each sheet symbol (KIID::AsString). None: KiCad gives
    # a file or a sheet on the path a new UUID at each load.
    uuids: tuple[str, ...] | None
    names: tuple[str, ...]  # The name of each sheet below the top level.
    top: int = 0  # the index of its top-level sheet (top_level_sheets)

    @property
    def path(self) -> str:
        """The UUID path, as in the (instances) of a placed symbol: "/<uuid>/<uuid>"."""
        return "/" + "/".join(self.uuids or ())

    @property
    def shown(self) -> str:
        """The sheet names, for example "/Power/Regulator/". The root is "/"."""
        return "/" + "".join(f"{name}/" for name in self.names)

    @property
    def netlist_path(self) -> str | None:
        """The UUID path as the XML netlist of kicad-cli writes it ("tstamps"):
        each UUID but the first, each with "/" after it. The root is "/".
        SCH_SHEET_PATH::PathAsString starts at the second sheet of the path: KiCad
        8 and 9 skip the root sheet, KiCad 10 the top-level sheet (BuildSheetList
        puts no virtual root into a path). None: no UUIDs. Checked with kicad-cli
        8.0.9, 9.0.9 and 10.0.6 (spike/kicad-versions/netlist_sheets_check.py)."""
        if self.uuids is None:
            return None
        return "/" + "".join(f"{uuid}/" for uuid in self.uuids[1:])


def _load(
    tops: list[TopLevelSheet],
    project_dir: Path,
    variables: dict[str, str],
    kicad: int | None = None,
    nil_top: bool = False,
) -> SheetList:
    """Load the hierarchy as KiCad does, but do not stop at a file that the server
    cannot read (a tolerant load of sheet_list.build_sheet_list): more files than
    KiCad loads, never fewer.

    Args:
        kicad: The major version of the KiCad that loads the files; None: not known
            (the KiCad of the version of each file).
    """
    return build_sheet_list(
        tops, project_dir, variables, kicad, tolerant=True, nil_top_uuid=nil_top
    )


def _places(sheet_list: SheetList) -> list[SheetPath]:
    """Return the sheet paths of a load in the order of SCH_SHEET_LIST::BuildSheetList."""
    places = []
    for path in sheet_list.paths:
        texts = tuple(kiid_text(kiid) for kiid in path.kiids)
        uuids = None if None in texts else tuple(text for text in texts if text is not None)
        file = path.screen.file if path.screen is not None else None
        places.append(SheetPath(file, uuids, path.names, path.top))
    return places


def _files(sheet_list: SheetList) -> list[Path]:
    """Return each file of the screens of a load one time (a file with two names, for
    example through a symbolic link, is two screens of KiCad)."""
    by_file = screens_by_file(list(sheet_list.screens.values()))
    return [screens[0].file for screens in by_file.values()]


def file_versions_of_load(
    tops: list[TopLevelSheet], project_dir: Path, variables: dict[str, str], kicad: int | None
) -> list[tuple[Path, int | None]]:
    """Return each file of a tolerant load of the hierarchy one time (see _load and
    sheet_files), with its file version: None when the parser of KiCad cannot read the
    file, or the file has no (version N).

    Args:
        kicad: The major version of the KiCad that loads the files; None: not known.
    """
    by_file = screens_by_file(list(_load(tops, project_dir, variables, kicad).screens.values()))
    return [(screens[0].file, screens[0].version or None) for screens in by_file.values()]


def netlist_root(project: Path, kicad: str | None, output: str = "netlist") -> tuple[Path, int]:
    """Return the schematic that gives kicad-cli the netlist of the full project.

    kicad-cli uses the project file of the name of the schematic
    (EESCHEMA_HELPERS::LoadSchematic). From KiCad 10.0.7 it loads all
    top-level sheets of that project (top_level_sheets) when the schematic is
    one of them. KiCad 10.0.0 to 10.0.6 load only the schematic. The BOM
    export loads the schematic in the same way (JobExportBom).

    kicad: The version of kicad-cli. A project with one top-level sheet does not
        need it (None).
    output: The name of the output in the error messages, for example "BOM".

    Returns:
        (the schematic, the number of top-level sheets of the project).

    Raises:
        InputError: kicad-cli cannot write the output of the full project.
    """
    tops = top_level_sheets(project)
    named = Path(os.path.abspath(project.with_suffix(".kicad_sch")))
    if len(tops) == 1:
        if project.is_file() and not same_file(tops[0].file, named):
            raise InputError(
                f"The top-level sheet of the project {project} is {tops[0].file.name}. "
                "kicad-cli uses the project file of the name of the schematic, thus it does "
                "not use the settings of this project (for example its text variables)."
            )
        return tops[0].file, 1
    numbers = version_triple(kicad) if kicad else None
    if numbers is None or numbers < (10, 0, 7):
        which = f"of KiCad {kicad}" if kicad else "of an unknown version"
        raise InputError(
            f"The project {project} has {len(tops)} top-level sheets. kicad-cli {which} "
            f"writes the {output} of one top-level sheet only. KiCad 10.0.7 and later "
            f"write the {output} of all top-level sheets."
        )
    if not is_declared(named, tops):
        raise InputError(
            f"The project {project} has {len(tops)} top-level sheets, but {named.name} is "
            f"not one of them. kicad-cli writes the {output} of all top-level sheets only for "
            "the schematic with the name of the project."
        )
    return named, len(tops)


def root_sheet_paths(root: Path, kicad: int | None = None) -> tuple[list[SheetPath], list[str]]:
    """Return each sheet path of the hierarchy of a root schematic, as kicad-cli
    loads it: the schematic is the one top-level sheet
    (EESCHEMA_HELPERS::LoadSchematic of KiCad 8, 9 and 10.0.0 to 10.0.6; KiCad
    10.0.7 and later also load the other top-level sheets of its project).

    A root schematic with no UUID gets NIL_UUID as the first UUID: KiCad gives it
    a new UUID at each load, and netlist_path does not use the first UUID. Thus
    the paths below it have UUIDs. Use these paths for the netlist only, not for
    the instances of the symbols (path).

    Args:
        kicad: The major version of the KiCad that loads the files; None: not known.

    Returns:
        (the paths in the order of SCH_SHEET_LIST::BuildSheetList, also the paths
        of the empty sheets, problems).
    """
    root = Path(os.path.abspath(root))
    tops = [TopLevelSheet(root, None, root.stem)]
    sheet_list = _load(tops, root.parent, text_variables(root), kicad, nil_top=True)
    return _places(sheet_list), sheet_list.problems


def sheet_files(root: Path) -> list[Path]:
    """Return the root schematic and all sub-sheet files of its hierarchy.

    Each file is in the list one time, also when the hierarchy uses it more than
    one time or under more than one name. A missing file, and a file that the
    system cannot open, is not in the list. A sub-sheet that the server cannot read
    (a parse error, a version that the server does not know) is in the list (a
    tolerant load, see _load): a parser of the file then gives the error. A
    top-level sheet with a parse error is not in the list, with no sub-sheets:
    KiCad does not load it.
    """
    root = Path(os.path.abspath(root))
    tops = [TopLevelSheet(root, None, root.stem)]
    return _files(_load(tops, root.parent, text_variables(root)))


def sheet_paths(project_file: Path, kicad: int | None = None) -> tuple[list[SheetPath], list[str]]:
    """Return each sheet path of the sheet hierarchy of a project.

    A file that the hierarchy uses more than one time has more than one path.
    The path of a top-level sheet is the UUID of its schematic file, or the
    UUID of its entry in top_level_sheets (KiCad 10). With one top-level
    sheet, the names start below it, as KiCad shows them. A file or a sheet
    with no UUID gives paths with no UUIDs below it, and a problem.

    Args:
        kicad: The major version of the KiCad that loads the files; None: not known.

    Returns:
        (the paths in the order of SCH_SHEET_LIST::BuildSheetList, also the paths
        of the empty sheets, problems).
    """
    project_file = Path(os.path.abspath(project_file))
    sheet_list = _load(
        top_level_sheets(project_file),
        project_file.parent,
        text_variables(project_file.with_suffix(SCH_SUFFIX)),
        kicad,
    )
    return _places(sheet_list), sheet_list.problems


def project_root_schematics(file_path: Path) -> list[Path]:
    """Return the root schematics of the projects that can hold a file.

    These are the projects in the directory of the file and in each parent
    directory. A project is a .kicad_pro file or its lock file: KiCad writes
    the lock file of the project also when no .kicad_pro file is there, for
    example when the schematic editor opens a .kicad_sch file alone. The root
    schematic does not always exist. When a symbolic link gives two paths to
    one root schematic, the list holds only the first path.
    """
    absolute = Path(os.path.abspath(file_path))
    directories: list[Path] = []
    for directory in [*absolute.parents, *absolute.resolve().parents]:
        if directory not in directories:
            directories.append(directory)

    roots: list[Path] = []
    real_roots: set[Path] = set()
    for directory in directories:
        names = {project.stem for project in directory.glob("*" + PRO_SUFFIX)}
        names.update(
            lock.name[1 : -len(PRO_LOCK_SUFFIX)] for lock in directory.glob("~*" + PRO_LOCK_SUFFIX)
        )
        for name in sorted(names):
            root = directory / (name + SCH_SUFFIX)
            real = root.resolve()
            if real not in real_roots:
                real_roots.add(real)
                roots.append(root)
    return roots


def is_project_root(file_path: Path) -> bool:
    """Return True if a schematic is the root schematic of its own project.

    That is, a .kicad_pro file with the same name is next to the schematic.
    A lock file of a project alone does not count: KiCad writes it also when
    the schematic editor opens a sub-sheet alone.
    """
    absolute = Path(os.path.abspath(file_path))
    return any(path.with_suffix(PRO_SUFFIX).is_file() for path in (absolute, absolute.resolve()))


def in_hierarchy(file_path: Path, root: Path) -> bool:
    """Return True if a file is the root schematic or a sub-sheet of it."""
    target = Path(file_path).resolve()
    return any(sheet.resolve() == target for sheet in sheet_files(root))


def project_files(project_file: Path) -> list[Path]:
    """Return each schematic file of a project one time: the trees of its top-level sheets.

    A project of KiCad 9 and earlier has one top-level sheet, the schematic of
    the project name. The sheet file names are expanded with the variables of
    the project. See sheet_files for the files in the list.
    """
    project_file = Path(os.path.abspath(project_file))
    variables = text_variables(project_file.with_suffix(SCH_SUFFIX))
    return _files(_load(top_level_sheets(project_file), project_file.parent, variables))


def in_project(file_path: Path, root: Path) -> bool:
    """Return True if a file is in the hierarchy of the project of a root schematic.

    The hierarchy of a KiCad 10 project also holds the trees of its other
    top-level sheets (top_level_sheets).
    """
    target = Path(file_path).resolve()
    return in_hierarchy(file_path, root) or any(
        file.resolve() == target for file in project_files(root.with_suffix(PRO_SUFFIX))
    )


def roots_containing(file_path: Path) -> list[Path]:
    """Return the existing root schematics whose hierarchy holds a file."""
    return [
        root
        for root in project_root_schematics(file_path)
        if root.is_file() and in_hierarchy(file_path, root)
    ]


def _other_roots(file_path: Path) -> list[Path]:
    """Return the root schematics whose hierarchy holds a file, but not the file itself.

    A lock file of a project can make the file a candidate root of itself.
    """
    target = Path(file_path).resolve()
    return [root for root in roots_containing(file_path) if root.resolve() != target]


def find_root_schematic(file_path: Path) -> Path | None:
    """Return the root schematic of a sub-sheet, or None.

    The tools that give a schematic to kicad-cli do not use this function: they
    use kicad_cli.cli_root, the schematic of the project name that kicad-cli loads
    with the full project (also with the top-level sheets of KiCad 10).

    Returns:
        None if the file is the root schematic of its own project, or if no
        root schematic holds it. Else, the one root schematic that holds it.

    Raises:
        AmbiguousRootError: More than one root schematic holds the file.
    """
    if is_project_root(file_path):
        return None
    roots = _other_roots(file_path)
    if len(roots) > 1:
        raise AmbiguousRootError(Path(file_path), roots)
    return roots[0] if roots else None


def schematic_project_file(schematic: Path) -> Path:
    """Return the project file that KiCad uses for a schematic.

    KiCad edits a sub-sheet in the project of its root schematic. A KiCad 10
    project can also hold the schematic as an other top-level sheet, or below
    one (top_level_sheets). A schematic with a project file of its name is the
    root of that project. The project file of a root schematic has the name
    of the schematic. When that file does not exist, the schematic editor uses
    its path for a project with no settings (``SCH_EDIT_FRAME::OpenProjectFiles``),
    thus the project library table is next to the schematic.

    Raises:
        AmbiguousRootError: More than one project holds the file.
    """
    absolute = Path(os.path.abspath(schematic))
    target = absolute.resolve()
    if is_project_root(absolute):
        # The project file can be next to the target of a symbolic link.
        for path in (absolute, target):
            if path.with_suffix(PRO_SUFFIX).is_file():
                return path.with_suffix(PRO_SUFFIX)
    projects = [root.with_suffix(PRO_SUFFIX) for root in _other_roots(absolute)]
    for candidate in project_root_schematics(absolute):
        project = candidate.with_suffix(PRO_SUFFIX)
        # A project of KiCad 10 whose top-level sheets are not only the root.
        tops = [top.file for top in top_level_sheets(project)]
        if project in projects or not project.is_file() or tops == [candidate]:
            continue
        if any(file.resolve() == target for file in project_files(project)):
            projects.append(project)
    if len(projects) > 1:
        raise AmbiguousRootError(
            absolute, [project.with_suffix(SCH_SUFFIX) for project in projects]
        )
    if projects:
        return Path(os.path.abspath(projects[0]))
    return absolute.with_suffix(PRO_SUFFIX)
