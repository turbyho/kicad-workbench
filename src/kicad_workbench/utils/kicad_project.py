"""The files and the settings of a KiCad project that the load of its schematics uses.

A KiCad project has a .kicad_pro file. KiCad 9 and earlier have one top-level
sheet: the schematic with the name of the project. KiCad 10 can have more
(schematic.top_level_sheets of the .kicad_pro file). KiCad expands the text
variables of the project in a sheet file name.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError
from ..parsers.sheet_file_names import (
    WINDOWS,
    KicadFileName,
    parse_file_name,
    sheet_file_name,
)
from .kicad_text import read_kicad_json

SCH_SUFFIX = ".kicad_sch"
PRO_SUFFIX = ".kicad_pro"
NIL_UUID = "00000000-0000-0000-0000-000000000000"

_TEXT_VAR = re.compile(r"\$\{([^}]+)\}")


def text_variables(root: Path) -> dict[str, str]:
    """Return the text variables that KiCad uses to expand a sheet file name.

    KiCad expands a sheet file name with the variables of the project only
    (PROJECT::TextVarResolver): PROJECTNAME and the text variables of the
    .kicad_pro file. It does not use environment variables. The variables
    for date, time and git hash are not applicable to a file name.
    """
    variables: dict[str, str] = {}
    project, _loaded = _project_settings(root.with_suffix(PRO_SUFFIX))
    texts = project.get("text_variables")
    if isinstance(texts, dict):
        # KiCad keeps the text variables as strings. Other values are not
        # applicable to a file name.
        variables.update((name, value) for name, value in texts.items() if isinstance(value, str))
    variables["PROJECTNAME"] = root.stem
    return variables


def expand_text_variables(name: str, variables: dict[str, str]) -> str:
    """Return a sheet file name with the text variables of text_variables expanded.
    A variable that is not known stays as it is."""
    return _TEXT_VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), name)


def _project_settings(project_file: Path) -> tuple[dict, bool]:
    """Return the JSON of a project file and the result of JSON_SETTINGS::LoadFromFile.

    nlohmann::json reads the file with a byte order mark and comments
    (kicad_text.read_kicad_json). The result is False for a file with a JSON error,
    and for a file with no meta.version that Get<int> converts: a number or a
    boolean (measured with kicad-cli 10.0.7: a text and null fail). A file that is
    not there gives {} and True.
    """
    try:
        project = read_kicad_json(project_file)
    except FileNotFoundError:
        return {}, True
    except (OSError, InputError):
        return {}, False
    if not isinstance(project, dict):
        return {}, False
    meta = project.get("meta")
    version = meta.get("version") if isinstance(meta, dict) else None
    return project, isinstance(version, (int, float))  # bool is an int


def _entry_fields(entry: object) -> dict[str, str] | None:
    """from_json of TOP_LEVEL_SHEET_INFO: the texts of uuid, name and filename that the
    entry has. An entry that is not an object has no fields (wxCHECK). None: a value
    that is not a string, a type error of nlohmann::json."""
    if not isinstance(entry, dict):
        return {}
    fields = {key: entry[key] for key in ("uuid", "name", "filename") if key in entry}
    if not all(isinstance(value, str) for value in fields.values()):
        return None
    return fields


@dataclass(frozen=True)
class TopLevelSheet:
    """A top-level sheet of a project."""

    # The file that KiCad checks (FileExists) and gives LoadSchematicFile. The load
    # expands the text variables of its name (top_level_file).
    file: Path
    # The UUID text of the entry. None: the UUID of the file (no entry, or the nil
    # UUID). A text that is not a UUID (also "" for an entry with no uuid: KIID())
    # gives a random KIID (sheet_list.kiid).
    uuid: str | None
    name: str


def top_level_file(file: Path, project_dir: Path, variables: dict[str, str]) -> KicadFileName:
    """Return the file name that the loader gives a top-level sheet.

    SCH_IO_KICAD_SEXPR::LoadSchematicFile gives the sheet its file name relative to
    the project, in the notation of Unix (SCH_SHEET::SetFileName), and loadHierarchy
    expands the text variables and makes it absolute with the project directory.

    Raises:
        SheetFileError: KiCad opens the file relative to its working directory.
    """
    relative = os.path.relpath(os.path.normpath(file), project_dir).replace("\\", "/")
    project = parse_file_name(os.path.join(os.path.abspath(project_dir), ""), WINDOWS)
    return sheet_file_name(expand_text_variables(relative, variables), project)


def same_file(a: Path, b: Path) -> bool:
    """wxFileName::SameAs: the normalized paths are equal, or the files have the same
    device and inode (stat: also through a symbolic link, and a hard link)."""
    if os.path.normpath(os.path.abspath(a)) == os.path.normpath(os.path.abspath(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False  # wxFileName::SameAs: not an error if a file is not there


def is_declared(root: Path, tops: list[TopLevelSheet]) -> bool:
    """findDeclaredTopLevelSheet of EESCHEMA_HELPERS::LoadSchematic: True if a schematic
    that is there is the file of a top-level sheet."""
    return root.is_file() and any(same_file(top.file, root) for top in tops)


def _entry_file(directory: Path, filename: str, windows: bool) -> Path:
    """wxFileName( projectPath, filename ): the directory and the volume of filename
    go, only its last component stays (wxFileName::Assign with a path and a name)."""
    return directory / parse_file_name(filename, windows).last


def top_level_sheets(project_file: Path) -> list[TopLevelSheet]:
    """Return the top-level sheets of a project, as KiCad 10 reads them.

    KiCad 10 keeps them in "schematic.top_level_sheets" of the .kicad_pro file
    (PROJECT_FILE). The rules of KiCad 10.0.7 (PARAM_LIST, from_json of
    TOP_LEVEL_SHEET_INFO, PROJECT_FILE::LoadFromFile, SCH_EDIT_FRAME::OpenProjectFiles,
    EESCHEMA_HELPERS::LoadSchematic):

    - An entry with a "uuid", "name" or "filename" that is not a string is a type
      error: KiCad ignores the full list. An entry that is not an object has no
      fields. An entry with no uuid gets a random UUID (KIID()), with no name an
      empty name.
    - The file of an entry is the last component of its filename in the directory
      of the project (wxFileName with a path and a name).
    - When the project file loads with no error (a number meta.version): an entry
      whose file is not there gets the schematic of the project name, when that one
      is there (a project from a template); an empty list gets the schematic of the
      project name with the UUID of the file.

    With no list (KiCad 9 and earlier, no project file), the project has one
    top-level sheet: the schematic of the project name, with the UUID of the file.
    KiCad does not load an entry whose file is not there (the caller checks it).
    """
    directory = Path(os.path.abspath(project_file)).parent
    default = directory / (project_file.stem + SCH_SUFFIX)
    project, loaded = _project_settings(project_file)
    schematic = project.get("schematic")
    entries = schematic.get("top_level_sheets") if isinstance(schematic, dict) else None
    sheets: list[TopLevelSheet] = []
    if isinstance(entries, list):
        for entry in entries:
            fields = _entry_fields(entry)
            if fields is None:
                sheets = []  # PARAM_LIST::Load throws: the list stays empty
                break
            sheets.append(
                TopLevelSheet(
                    _entry_file(directory, fields.get("filename", ""), WINDOWS),
                    fields.get("uuid", ""),
                    fields.get("name", ""),
                )
            )
    if loaded:
        sheets = [
            TopLevelSheet(default, sheet.uuid, project_file.stem)
            if not sheet.file.is_file() and default.is_file()
            else sheet
            for sheet in sheets
        ]
    return sheets or [TopLevelSheet(default, None, project_file.stem)]
