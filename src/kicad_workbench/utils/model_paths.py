"""Find the file of a 3D model of a footprint, as KiCad finds it.

The rules come from ``FILENAME_RESOLVER::ResolvePath`` and
``FILENAME_RESOLVER::createPathList`` (``common/filename_resolver.cpp``) of
KiCad 8.0.9, 9.0.9 and 10.0. This module is an own implementation of these
rules. KiCad tries these places in this sequence, and uses the first file:

1. KiCad 9 and later replace "\\" with "/" (not on Windows).
2. KiCad expands the path variables (``ExpandEnvVarSubstitutions``).
3. KiCad 9 and later: "kicad-embed://<name>" is a file that is embedded in the
   footprint.
4. The path as it is: an absolute path, or a path relative to the working
   directory of KiCad.
5. A name that starts with "${" or "$(" and is not found gives no file.
6. A path relative to the project directory.
7. A path relative to the footprint library (its full URI).
8. A path relative to ``${KICAD<major>_3DMODEL_DIR}``.
9. "ALIAS:path" or ":ALIAS:path": a path relative to a search path. The search
   paths are the path variables of KiCad, except the URLs, KICAD_PTEMPLATES
   and KICAD*_FOOTPRINT_DIR.

The working directory of KiCad (step 4) is not known to this server. Thus a
relative path that this module finds in a later step gets a note.
"""

import fnmatch
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .library_tables import KicadEnvironment, expand

EMBEDDED_PREFIX = "kicad-embed://"
_UNEXPANDED = re.compile(r"\$\{([^}]*)\}|\$\(([^)]*)\)")


@dataclass(frozen=True)
class ModelFile:
    """The result of the search for the file of a 3D model."""

    state: str  # "found", "embedded", "not found" or "no file name"
    path: Path | None = None  # The file that KiCad uses (state "found").
    note: str = ""


def _file(path: str) -> Path | None:
    """Return the absolute path when it is a file, else None."""
    candidate = Path(path)
    if candidate.is_absolute() and candidate.is_file():
        return Path(os.path.normpath(candidate))
    return None


def _search_paths(major: int, env: KicadEnvironment, project: dict[str, str]) -> dict[str, str]:
    """Return the search paths of FILENAME_RESOLVER: alias name -> directory.

    The aliases are the names of the path variables of KiCad (GetKicadPaths).
    """
    names = [
        name
        for name, value in env.variables.items()
        if name != "KICAD_PTEMPLATES"
        and not fnmatch.fnmatchcase(name, "KICAD*_FOOTPRINT_DIR")
        and "://" not in value
    ]
    if not any(fnmatch.fnmatchcase(name, "KICAD*_3DMODEL_DIR") for name in names):
        names.append(f"KICAD{major}_3DMODEL_DIR")
    paths = {}
    for name in names:
        value = expand(f"${{{name}}}", env, project)
        # KiCad makes a relative value absolute from its working directory,
        # which this server does not know.
        if value and not _UNEXPANDED.search(value) and Path(value).is_absolute():
            paths[name] = value.rstrip("/\\") or value
    return paths


def _split_alias(name: str) -> tuple[str, str] | None:
    """Split "ALIAS:path" or ":ALIAS:path" (FILENAME_RESOLVER::SplitAlias)."""
    start = 1 if name.startswith(":") else 0
    colon = name.find(":", start)
    if colon < 0 or colon == start or colon + 1 >= len(name):
        return None
    return name[start:colon], name[colon + 1 :]


def resolve_model(
    file_name: str,
    major: int,
    env: KicadEnvironment,
    project_file: Path | None,
    project: dict[str, str],
    library: Path | None,
    embedded: set[str],
    board_embedded: set[str] | None = None,
) -> ModelFile:
    """Find the file of a 3D model as KiCad finds it.

    Args:
        file_name: The file name of the model in the footprint.
        major: The major version of the KiCad that reads the footprint.
        env: The path variables of that KiCad (library_tables.kicad_environment).
        project_file: The .kicad_pro file of the project, or None.
        project: The variables of the project (library_tables.project_variables).
        library: The directory of the footprint library, or None.
        embedded: The names of the files that are embedded in the footprint.
        board_embedded: For a footprint of a board, the names of the files
            that are embedded in the board. KiCad looks in the files of the
            footprint, then of the board. None: a footprint of a library.
    """
    if not file_name:
        return ModelFile("no file name")
    name = file_name
    if major >= 9 and sys.platform != "win32":
        name = name.replace("\\", "/")
    name = expand(name, env, project)

    if major >= 9 and name.startswith(EMBEDDED_PREFIX):
        embedded_name = name[len(EMBEDDED_PREFIX) :]
        if embedded_name in embedded or embedded_name in (board_embedded or set()):
            return ModelFile("embedded")
        owners = (
            "The footprint has" if board_embedded is None else "The footprint and the board have"
        )
        return ModelFile("not found", note=f'{owners} no embedded file "{embedded_name}".')

    if found := _file(name):
        return ModelFile("found", found)
    relative = not Path(name).is_absolute()
    note = (
        "KiCad first looks for a relative path in its working directory, which this "
        "server does not know."
        if relative
        else ""
    )

    # KiCad 10 also compares the variable name with the aliases of the search
    # paths. These aliases are the path variables, and KiCad expanded each
    # variable that has a value. Thus this step finds no file.
    if not file_name.startswith(("${", "$(")):
        # KiCad joins the directory and the name as text, also for an absolute
        # name: "/project" and "/abs/x.step" give "/project//abs/x.step".
        candidates = []
        if project_file is not None and not name.startswith(":"):
            directory = project_file.absolute().parent
            candidates.append(expand(f"{directory}{os.sep}{name}", env, project))
        if library is not None and not name.startswith(":"):
            candidates.append(f"{library}{os.sep}{name}")
        if not name.startswith(":"):
            candidates.append(expand(f"${{KICAD{major}_3DMODEL_DIR}}{os.sep}{name}", env, project))
        alias = _split_alias(name)
        if alias is not None:
            directory = _search_paths(major, env, project).get(alias[0])
            if directory is not None:
                candidates.append(expand(f"{directory}{os.sep}{alias[1]}", env, project))
        for candidate in candidates:
            if found := _file(candidate):
                return ModelFile("found", found, note)

    missing = sorted({a or b for a, b in _UNEXPANDED.findall(name)})
    if missing:
        # The variable is the cause. A file whose path holds "${NAME}" in the
        # working directory of KiCad is not a real case, thus no note for it.
        names = ", ".join(missing)
        note = (
            f"The path variable {names} has no value."
            if len(missing) == 1
            else f"The path variables {names} have no value."
        )
        if "KIPRJMOD" in missing and project_file is None:
            note += " Give the project to set KIPRJMOD."
    return ModelFile("not found", note=note)
