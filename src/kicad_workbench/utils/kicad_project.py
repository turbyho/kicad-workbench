"""The files and the settings of a KiCad project that the load of its schematics uses.

A KiCad project has a .kicad_pro file. KiCad 9 and earlier have one top-level
sheet: the schematic with the name of the project. KiCad 10 can have more
(schematic.top_level_sheets of the .kicad_pro file). KiCad expands the text
variables of the project in a sheet file name.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

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
