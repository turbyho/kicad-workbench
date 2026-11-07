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

import hashlib
import json
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.kicad_fields import FieldRules, rules_for_file, sheet_fields
from ..parsers.kicad_versions import KicadFileError, file_version, version_triple
from ..parsers.sexpr import Node, SExprError, parse
from .kicad_text import KiCadEncodingError, decode_as_kicad

SCH_SUFFIX = ".kicad_sch"
PRO_SUFFIX = ".kicad_pro"
PRO_LOCK_SUFFIX = PRO_SUFFIX + ".lck"
NIL_UUID = "00000000-0000-0000-0000-000000000000"

_TEXT_VAR = re.compile(r"\$\{([^}]+)\}")
# The UUID and the sheets of each schematic content, by the digest of the
# content. One tool call reads the same hierarchy more than one time, and a
# large schematic takes a long time to parse.
_SHEETS_CACHE: dict[bytes, tuple[str | None, list["_Sheet"]]] = {}
_SHEETS_CACHE_SIZE = 1024


class AmbiguousRootError(WorkbenchError):
    """A sub-sheet is part of more than one root schematic."""

    def __init__(self, sheet: Path, roots: list[Path]):
        self.sheet = sheet
        self.roots = roots
        names = ", ".join(str(root) for root in roots)
        super().__init__(f"{sheet} is a sub-sheet of more than one root schematic: {names}")


def _hierarchy_rules(root: Node) -> list[FieldRules]:
    """Return the field rules to find the sheets of a schematic of any version.

    The server reads and changes only files of known versions
    (kicad_fields.rules_for_file), but the hierarchy holds all files that KiCad
    loads. The installed KiCad reads an older file with its own parser, and a
    newer KiCad reads a newer file. The KiCad that loads the file is not
    known: the kicad-cli of PATH is not always the KiCad of the user. Thus
    such a file gets the rules of KiCad 8 and 9 and of KiCad 10: its sheets
    are the sheets of the two rule sets. For the lock check this gives more
    files, never fewer.
    """
    try:
        return [rules_for_file(root)]
    except KicadFileError:
        version = file_version(root) or 0
        return [FieldRules(9, version), FieldRules(10, version)]


@dataclass(frozen=True)
class _Sheet:
    """A sheet symbol of a schematic."""

    uuid: str | None
    name: str
    file: str
    problem: str = ""  # KiCad cannot read the fields of the sheet.


def _read_sheets(path: Path) -> tuple[str | None, list[_Sheet]]:
    """Return the UUID of a schematic file and its sheet symbols, with a cache.

    The file is read as KiCad reads it (kicad_text.decode_as_kicad): a string
    with a byte that is not valid UTF-8 is an empty string. Only a (sheet ...)
    at the top level is a sheet: a symbol field with the name of a sheet field
    is not.

    Raises:
        OSError: The file cannot be read.
        KiCadEncodingError, SExprError: KiCad cannot load the file (a byte
            that is not valid UTF-8 outside a string, unbalanced parentheses).
    """
    data = path.read_bytes()
    key = hashlib.blake2b(data, digest_size=16).digest()
    cached = _SHEETS_CACHE.get(key)
    if cached is None:
        root = parse(decode_as_kicad(data, path))
        uuid = root.first("uuid")
        sheets = []
        all_rules = _hierarchy_rules(root)
        for sheet in root.children("sheet"):
            sheet_uuid = sheet.first("uuid")
            found = []
            errors = []
            for rules in all_rules:
                try:
                    fields = sheet_fields(sheet, rules)
                except KicadFileError as e:
                    errors.append(str(e))
                    continue
                found.append(
                    _Sheet(
                        (sheet_uuid.value(0) or None) if sheet_uuid else None,
                        fields["Sheetname"],
                        fields["Sheetfile"],
                    )
                )
            if not found:
                # Only this sheet is not known: the other sheets of the file stay.
                found.append(_Sheet(None, "", "", errors[0]))
            sheets.extend(dict.fromkeys(found))
        cached = ((uuid.value(0) or None) if uuid else None), sheets
        if len(_SHEETS_CACHE) >= _SHEETS_CACHE_SIZE:
            _SHEETS_CACHE.clear()
        _SHEETS_CACHE[key] = cached
    return cached


def text_variables(root: Path) -> dict[str, str]:
    """Return the text variables that KiCad uses to expand a sheet file name.

    KiCad expands a sheet file name with the variables of the project only
    (PROJECT::TextVarResolver): PROJECTNAME and the text variables of the
    .kicad_pro file. It does not use environment variables. The variables
    for date, time and git hash are not applicable to a file name.
    """
    variables: dict[str, str] = {}
    try:
        project = json.loads(root.with_suffix(PRO_SUFFIX).read_text(encoding="utf-8"))
        # KiCad keeps the text variables as strings. Other values are not
        # applicable to a file name.
        variables.update(
            (name, value)
            for name, value in (project.get("text_variables") or {}).items()
            if isinstance(value, str)
        )
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    variables["PROJECTNAME"] = root.stem
    return variables


def expand_text_variables(name: str, variables: dict[str, str]) -> str:
    """Return a sheet file name with the text variables of text_variables expanded.
    A variable that is not known stays as it is."""
    return _TEXT_VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), name)


def _top_level_entries(project_file: Path) -> list:
    """Return "schematic.top_level_sheets" of a project file, or []."""
    try:
        project = json.loads(project_file.read_text(encoding="utf-8"))
        entries = project["schematic"]["top_level_sheets"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return entries if isinstance(entries, list) else []


@dataclass(frozen=True)
class TopLevelSheet:
    """A top-level sheet of a project."""

    file: Path
    uuid: str | None  # The UUID of the sheet path. None: the UUID of the file.
    name: str


def top_level_sheets(project_file: Path) -> list[TopLevelSheet]:
    """Return the top-level sheets of a project.

    KiCad 10 keeps them in "schematic.top_level_sheets" of the .kicad_pro
    file (PROJECT_FILE). When the list is not there (KiCad 9 and earlier, or
    no project file), the project has one top-level sheet: the schematic
    with the name of the project, with the UUID of the file. KiCad uses the
    schematic of the project name for an entry whose file does not exist (a
    project from a template).
    """
    directory = Path(os.path.abspath(project_file)).parent
    default = directory / (project_file.stem + SCH_SUFFIX)
    sheets = []
    for entry in _top_level_entries(project_file):
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            continue
        file = directory / entry["filename"]
        name = entry.get("name") if isinstance(entry.get("name"), str) else file.stem
        if not file.exists() and default.exists():
            file, name = default, project_file.stem
        uuid = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
        if uuid == NIL_UUID:
            uuid = None  # KiCad gives the default sheet this UUID (PROJECT_FILE::LoadFromFile).
        sheets.append(TopLevelSheet(Path(os.path.normpath(file)), uuid, name))
    return sheets or [TopLevelSheet(default, None, project_file.stem)]


@dataclass(frozen=True)
class SheetPath:
    """One place of a schematic file in the sheet hierarchy of a project."""

    file: Path
    # The top-level sheet, then each sheet symbol. None: KiCad gives a file or
    # a sheet on the path a new UUID at each load.
    uuids: tuple[str, ...] | None
    names: tuple[str, ...]  # The name of each sheet below the top level.

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


def _walk(
    tops: list[TopLevelSheet], variables: dict[str, str], unique: bool = False
) -> tuple[list[SheetPath], list[str]]:
    """Return each place of each file in the sheet trees of the top-level sheets.

    The function resolves each sheet file name as KiCad does: it expands the
    text variables, and it makes a relative name absolute with the directory
    of the parent sheet. KiCad does not load a sheet that holds one of its
    parents. KiCad shows a missing sheet as empty, and it loads the other
    sheets when it cannot load one sheet: such a file gives no place, a file
    that cannot be loaded gives a problem.

    Args:
        unique: Give each file one place: do not read the sheets of a file
            again when the hierarchy uses it more than one time.

    Returns:
        (places in the order of the tree levels, problems).
    """
    places: list[SheetPath] = []
    problems: list[str] = []
    expanded: set[Path] = set()
    pending = deque(
        (top.file, (top.uuid,) if top.uuid else (), () if len(tops) == 1 else (top.name,), ())
        for top in tops
    )
    while pending:
        file, uuids, names, parents = pending.popleft()
        try:
            real = file.resolve()
            if unique and real in expanded:
                continue
            expanded.add(real)
            file_uuid, sheets = _read_sheets(file)
        except (FileNotFoundError, RuntimeError):
            # RuntimeError: a loop of symbolic links (Python 3.12 and earlier).
            continue
        except (OSError, KiCadEncodingError, SExprError) as e:
            problems.append(f"{file} cannot be read: {e}")
            continue
        if uuids == ():
            uuids = (file_uuid,) if file_uuid else None
            if uuids is None:
                problems.append(f"{file} has no UUID.")
        # KiCad has one place for a file with one UUID path. The two rule sets
        # of a file of an other version can give one sheet two names.
        if uuids is not None and any(p.uuids == uuids and p.file == file for p in places):
            continue
        places.append(SheetPath(file, uuids, names))
        for sheet in sheets:
            if sheet.problem:
                problems.append(f"{file}: {sheet.problem}")
                continue
            if not sheet.file:
                continue  # KiCad reads an invalid name as empty: no file.
            child = Path(expand_text_variables(sheet.file, variables))
            if not child.is_absolute():
                child = file.parent / child
            child = Path(os.path.normpath(child))
            try:
                if child.resolve() in (*parents, real):
                    continue
            except (OSError, RuntimeError):
                continue  # KiCad shows such a sheet as missing.
            child_uuids = None
            if uuids is not None:
                if sheet.uuid is None:
                    problems.append(f"The sheet {sheet.name!r} of {file} has no UUID.")
                else:
                    child_uuids = (*uuids, sheet.uuid)
            pending.append((child, child_uuids, (*names, sheet.name), (*parents, real)))
    return places, problems


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
        if project.is_file() and tops[0].file.resolve() != named.resolve():
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
    if not named.is_file() or all(top.file.resolve() != named.resolve() for top in tops):
        raise InputError(
            f"The project {project} has {len(tops)} top-level sheets, but {named.name} is "
            f"not one of them. kicad-cli writes the {output} of all top-level sheets only for "
            "the schematic with the name of the project."
        )
    return named, len(tops)


def root_sheet_paths(root: Path) -> tuple[list[SheetPath], list[str]]:
    """Return each place of each file in the hierarchy of a root schematic, as
    kicad-cli loads it: the schematic is the one top-level sheet
    (EESCHEMA_HELPERS::LoadSchematic of KiCad 8, 9 and 10.0.0 to 10.0.6; KiCad
    10.0.7 and later also load the other top-level sheets of its project).

    A root schematic with no UUID gets NIL_UUID as the first UUID: KiCad gives it
    a new UUID at each load, and netlist_path does not use the first UUID. Thus
    the paths below it have UUIDs. Use these paths for the netlist only, not for
    the instances of the symbols (path).

    Returns:
        (paths, problems), see _walk.
    """
    root = Path(os.path.abspath(root))
    try:
        file_uuid, _sheets = _read_sheets(root)
    except (OSError, RuntimeError, KiCadEncodingError, SExprError):
        file_uuid = None  # _walk gives the problem
    uuid = None if file_uuid else NIL_UUID
    return _walk([TopLevelSheet(root, uuid, root.stem)], text_variables(root))


def sheet_files(root: Path) -> list[Path]:
    """Return the root schematic and all sub-sheet files of its hierarchy.

    Each file is in the list one time, also when the hierarchy uses it more
    than one time. A missing file, and a file that KiCad cannot load, is not
    in the list. A file of an old version is in the list, with the files
    below it (_hierarchy_rules): a parser of the file then gives the error. A file with a byte that is not valid UTF-8 in a string is in
    the list, as in KiCad. See _walk for the rules.
    """
    root = Path(os.path.abspath(root))
    tops = [TopLevelSheet(root, None, root.stem)]
    places, _problems = _walk(tops, text_variables(root), unique=True)
    return [place.file for place in places]


def sheet_paths(project_file: Path) -> tuple[list[SheetPath], list[str]]:
    """Return each place of each schematic file in the sheet hierarchy of a project.

    A file that the hierarchy uses more than one time has more than one path.
    The path of a top-level sheet is the UUID of its schematic file, or the
    UUID of its entry in top_level_sheets (KiCad 10). With one top-level
    sheet, the names start below it, as KiCad shows them. A file or a sheet
    with no UUID gives paths with no UUIDs below it, and a problem.

    Returns:
        (paths, problems).
    """
    project_file = Path(os.path.abspath(project_file))
    return _walk(
        top_level_sheets(project_file), text_variables(project_file.with_suffix(SCH_SUFFIX))
    )


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
    the project.
    """
    project_file = Path(os.path.abspath(project_file))
    variables = text_variables(project_file.with_suffix(SCH_SUFFIX))
    places, _problems = _walk(top_level_sheets(project_file), variables, unique=True)
    return [place.file for place in places]


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
