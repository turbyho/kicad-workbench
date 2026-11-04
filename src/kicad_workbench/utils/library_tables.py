"""Library tables of KiCad: the symbol, footprint and design block libraries.

The rules come from KiCad 10 (``common/paths.cpp``, ``common/env_vars.cpp``,
``common/settings/common_settings.cpp``, ``common/libraries/``,
``KIwxExpandEnvVars`` in ``common/common.cpp``). This module is an own
implementation of these rules.

- The global tables are in the configuration directory of the KiCad version,
  for example ``~/.config/kicad/10.0/sym-lib-table``. The project tables are
  next to the ``.kicad_pro`` file.
- KiCad reads the global table, then the project table. When more than one
  library has the same name, KiCad uses the last one (``active_rows``): a
  library of the project table replaces a library of the global table.
- A row of the type "Table" holds a nested table. The default global table of
  KiCad 10 has only such a row, to the table of the KiCad installation.
- A URI can hold path variables: ``${KICAD10_SYMBOL_DIR}``, ``${KIPRJMOD}``
  and others.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..parsers.kicad_versions import version_numbers
from ..parsers.sexpr import SExprError, parse
from .kicad_text import KiCadEncodingError, read_as_kicad
from .kicad_version import get_kicad_share_dir

# The file name of each table kind, and the head of its S-expression.
TABLE_FILES = {
    "symbol": "sym-lib-table",
    "footprint": "fp-lib-table",
    "design_block": "design-block-lib-table",
}
_TABLE_HEADS = {
    "symbol": "sym_lib_table",
    "footprint": "fp_lib_table",
    "design_block": "design_block_lib_table",
}
# The type of a row that holds a nested table.
NESTED_TABLE = "Table"

# The path variables of KiCad, with the stock directory of each in the
# installation ("<NAME>" gets the major version, for example KICAD10_SYMBOL_DIR).
_STOCK_DIRS = {
    "SYMBOL_DIR": "symbols",
    "FOOTPRINT_DIR": "footprints",
    "DESIGN_BLOCK_DIR": "blocks",
    "3DMODEL_DIR": "3dmodels",
    "TEMPLATE_DIR": "template",
}
# A variable of an other version of KiCad gives the value of this version.
# KiCad has these aliases only (KIwxExpandEnvVars), not for the design blocks
# and the templates.
_ALIASES = (
    (re.compile(r"KICAD\d+_3DMODEL_DIR|.*KISYS3DMOD.*"), "3DMODEL_DIR"),
    (re.compile(r"KICAD\d+_SYMBOL_DIR|KICAD_SYMBOL_DIR"), "SYMBOL_DIR"),
    (re.compile(r"KICAD\d+_FOOTPRINT_DIR"), "FOOTPRINT_DIR"),
    (re.compile(r"KICAD\d+_3RD_PARTY"), "3RD_PARTY"),
)
_VARIABLE_CHARS = re.compile(r"[A-Za-z0-9_:]*")


class LibraryTableError(ValueError):
    """The KiCad version or a library table cannot be read."""


@dataclass(frozen=True)
class KicadEnvironment:
    """The directories and the path variables of one KiCad version."""

    major: int
    minor: int
    config_dir: Path
    variables: dict[str, str]

    @property
    def version_dir(self) -> str:
        """The name of the version directory, for example "10.0"."""
        return f"{self.major}.{self.minor}"


@dataclass
class LibraryRow:
    """One library of a library table."""

    name: str
    type: str
    uri: str
    options: str
    description: str
    disabled: bool
    hidden: bool
    scope: str  # "project" or "global"
    table: Path  # The table file that holds the row.
    path: Path | None = None  # The URI with the variables expanded.
    problems: list[str] = field(default_factory=list)


def _parse_version(version: str) -> tuple[int, int]:
    numbers = version_numbers(version)
    if numbers is None:
        raise LibraryTableError(f"The KiCad version {version!r} has no major and minor number.")
    return numbers


def _documents_base() -> Path:
    """The documents directory of the user, as KiCad finds it."""
    if sys.platform == "win32":
        # The known folder "Documents" can be moved (for example to OneDrive).
        import ctypes
        from ctypes import wintypes
        from uuid import UUID

        folder_id = UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
        guid = (ctypes.c_byte * 16).from_buffer_copy(folder_id.bytes_le)
        path_ptr = wintypes.LPWSTR()
        if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(path_ptr)) != 0:
            raise LibraryTableError("Windows does not give the Documents folder.")
        try:
            return Path(path_ptr.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
    if sys.platform == "darwin":
        return Path.home() / "Documents"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _config_base() -> Path:
    """The configuration directory of the user, as KiCad finds it."""
    if sys.platform == "win32":
        if not os.environ.get("APPDATA"):
            raise LibraryTableError("The environment has no APPDATA, thus KiCad has no settings.")
        return Path(os.environ["APPDATA"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _stock_dir(version_dir: str) -> Path | None:
    """The "share/kicad" directory of the KiCad installation of the version.

    On Windows each version has its own directory, "KiCad/<major.minor>". On
    Linux and macOS KiCad has its data directory built in, thus the first
    installation that find_kicad_install() finds is used. With more than one
    installed version, the environment or kicad_common.json must set the
    variables (for example KICAD10_SYMBOL_DIR), as in KiCad.
    """
    if sys.platform == "win32":
        path = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "KiCad" / version_dir
        stock = path / "share" / "kicad"
        return stock if stock.is_dir() else None
    return get_kicad_share_dir()


def kicad_environment(version: str) -> KicadEnvironment:
    """Return the directories and path variables of a KiCad version.

    Args:
        version: The KiCad version, for example "10.0.6" (kicad-cli version).

    The value of a variable comes from the environment of this process, else
    from ``environment.vars`` of ``kicad_common.json``, else from the default
    of the installation, in this order (COMMON_SETTINGS of KiCad).
    """
    major, minor = _parse_version(version)
    version_dir = f"{major}.{minor}"
    config_home = os.environ.get("KICAD_CONFIG_HOME")
    config_dir = (Path(config_home) if config_home else _config_base() / "kicad") / version_dir

    folder = "KiCad" if sys.platform in ("win32", "darwin") else "kicad"
    documents_home = os.environ.get("KICAD_DOCUMENTS_HOME")
    documents = (
        (Path(documents_home) if documents_home else _documents_base()) / folder / version_dir
    )

    defaults: dict[str, str] = {
        f"KICAD{major}_3RD_PARTY": str(documents / "3rdparty"),
        "KICAD_USER_TEMPLATE_DIR": str(documents / "template"),
    }
    stock = _stock_dir(version_dir)
    if stock is not None:
        for name, subdir in _STOCK_DIRS.items():
            defaults[f"KICAD{major}_{name}"] = str(stock / subdir)

    settings: dict[str, str] = {}
    common = config_dir / "kicad_common.json"
    if common.is_file():
        values = _json_object(common, "environment", "vars")
        settings = {str(k).strip(): str(v).strip() for k, v in values.items() if v is not None}

    # An environment variable with a value replaces the setting (an empty
    # value does not, InitializeEnvironment of KiCad).
    variables = {**defaults, **settings}
    for name in list(variables):
        if os.environ.get(name):
            variables[name] = os.environ[name]
    return KicadEnvironment(major, minor, config_dir, variables)


def project_variables(project_file: Path) -> dict[str, str]:
    """Return the variables of a project: KIPRJMOD, PROJECTNAME and its text variables.

    Raises:
        LibraryTableError: The project file cannot be read.
    """
    project_file = project_file.absolute()
    variables = {"KIPRJMOD": str(project_file.parent), "PROJECTNAME": project_file.stem}
    if project_file.is_file():
        texts = _json_object(project_file, "text_variables")
        variables.update({k: v for k, v in texts.items() if isinstance(v, str)})
    return variables


def _json_object(path: Path, *keys: str) -> dict:
    """Return the JSON object at the keys of a JSON file, or {} if it has none.

    Raises:
        LibraryTableError: The file cannot be read, or a value on the way is
            not a JSON object.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LibraryTableError(f"{path} cannot be read: {e}") from e
    for key in ("",) + keys:
        if key:
            value = value.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            name = ".".join(keys[: keys.index(key) + 1]) if key else "the file"
            raise LibraryTableError(f"{path}: {name} is not a JSON object.")
    return value


def expand(text: str, env: KicadEnvironment, project: dict[str, str] | None = None) -> str:
    """Expand the path variables in a library URI as KiCad does.

    The forms are $NAME, $(NAME), ${NAME} and, on Windows, %NAME%. "\\$"
    (and "\\%" on Windows) is a "$" or "%" with no variable. An unknown
    variable stays as it is. When the result holds brackets, it is expanded
    again, one time for each different text (KIwxExpandEnvVars of KiCad).
    """
    project = project or {}
    seen: set[str] = set()
    while text not in seen:
        seen.add(text)
        text = _expand_once(text, env, project)
        first = min((text.find(c) for c in "{(%" if c in text), default=-1)
        last = max((text.rfind(c) for c in "})%" if c in text), default=-1)
        if first < 0 or last < 0 or first == last:
            break
    return text


def _lookup(name: str, env: KicadEnvironment, project: dict[str, str]) -> str | None:
    # KiCad sets its own variables in its environment, thus they come before
    # the other environment variables. A defined empty variable gives "".
    if name in project:
        return project[name]
    if name in env.variables:
        return env.variables[name]
    if name in os.environ:
        return os.environ[name]
    for pattern, base in _ALIASES:
        if pattern.fullmatch(name):
            return env.variables.get(f"KICAD{env.major}_{base}")
    return None


def _expand_once(text: str, env: KicadEnvironment, project: dict[str, str]) -> str:
    windows = sys.platform == "win32"
    result = []
    i = 0
    while i < len(text):
        char = text[i]
        if (
            char == "\\"
            and i + 1 < len(text)
            and (text[i + 1] == "$" or (windows and text[i + 1] == "%"))
        ):
            result.append(text[i + 1])
            i += 2
            continue
        if char not in "$%" or (char == "%" and not windows):
            result.append(char)
            i += 1
            continue
        if char == "%":
            close, start = "%", i + 1
        elif text[i + 1 : i + 2] in ("(", "{"):
            close, start = ")" if text[i + 1] == "(" else "}", i + 2
        else:
            close, start = "", i + 1
        name = _VARIABLE_CHARS.match(text, start).group()
        end = start + len(name)
        # KiCad also expands a variable with no closing bracket.
        if close and text[end : end + 1] == close:
            end += 1
        value = _lookup(name, env, project) if name else None
        result.append(text[i:end] if value is None else value)
        i = end
    return "".join(result)


def read_table(path: Path, kind: str) -> list[dict]:
    """Read the rows of one library table file.

    Raises:
        LibraryTableError: The file cannot be read, or it is not a table of
            the kind.
    """
    try:
        # KiCad 10 reads a file of 0 or 1 byte as a table with no rows (LIBRARY_TABLE).
        if path.stat().st_size <= 1:
            return []
        root = parse(read_as_kicad(path))
    except (OSError, KiCadEncodingError, SExprError) as e:
        raise LibraryTableError(f"The library table {path} cannot be read: {e}") from e
    if root.head != _TABLE_HEADS[kind]:
        raise LibraryTableError(
            f"The library table {path} is a {root.head!r} table, not {_TABLE_HEADS[kind]!r}."
        )
    rows = []
    for lib in root.children("lib"):
        values = {}
        for key in ("name", "type", "uri", "options", "descr"):
            item = lib.first(key)
            values[key] = item.value(0, "") if item else ""
        values["disabled"] = lib.first("disabled") is not None
        values["hidden"] = lib.first("hidden") is not None
        rows.append(values)
    return rows


def library_rows(
    kind: str, version: str, project_file: Path | None = None
) -> tuple[list[LibraryRow], list[str]]:
    """Return the libraries of the global table and of the project table.

    Args:
        kind: "symbol", "footprint" or "design_block".
        version: The KiCad version, for example "10.0.6".
        project_file: The .kicad_pro file, or None for the global table only.

    Returns:
        (rows, problems). The rows are in the sequence in which KiCad reads
        them: the global table, then the project table. The rows of a nested
        table replace its "Table" row. A disabled nested table gives no rows,
        and a hidden one makes its rows hidden. ``problems`` names the tables
        that cannot be read.

    Raises:
        LibraryTableError: The version or kicad_common.json cannot be read.
    """
    env = kicad_environment(version)
    project = project_variables(project_file) if project_file else {}
    rows: list[LibraryRow] = []
    problems: list[str] = []
    tables = [("global", env.config_dir / TABLE_FILES[kind])]
    if project_file:
        tables.append(("project", project_file.parent / TABLE_FILES[kind]))
    for scope, table in tables:
        if not table.is_file():
            if scope == "global":
                problems.append(f"The global library table {table} does not exist.")
            continue
        _add_rows(kind, scope, table, False, env, project, rows, problems, set())
    return rows, problems


def _add_rows(kind, scope, table, hidden, env, project, rows, problems, seen) -> None:
    try:
        real = table.resolve()
    except OSError:
        real = table
    if real in seen:
        # KiCad loads each table one time (LIBRARY_MANAGER).
        problems.append(f"A reference to the library table {table} already exists.")
        return
    seen.add(real)
    try:
        values = read_table(table, kind)
    except LibraryTableError as e:
        problems.append(str(e))
        return
    for value in values:
        expanded = expand(value["uri"], env, project)
        if value["type"] == NESTED_TABLE:
            if value["disabled"]:
                continue
            # A nested table can be relative to the table that holds it.
            child = Path(expanded)
            if not child.is_absolute():
                child = table.parent / child
            _add_rows(
                kind, scope, child, hidden or value["hidden"], env, project, rows, problems, seen
            )
            continue
        row = LibraryRow(
            name=value["name"],
            type=value["type"],
            uri=value["uri"],
            options=value["options"],
            description=value["descr"],
            disabled=value["disabled"],
            hidden=hidden or value["hidden"],
            scope=scope,
            table=table,
        )
        if re.search(r"\$[({]", expanded):
            row.problems.append(f"The URI has a variable with no value: {expanded}")
        elif Path(expanded).is_absolute():
            row.path = Path(expanded)
        else:
            # KiCad makes such a path absolute from its working directory.
            row.problems.append(f"The URI is a relative path: {expanded}")
        rows.append(row)


def active_rows(rows: list[LibraryRow]) -> list[LibraryRow]:
    """Return the row that KiCad uses for each library name.

    A later row replaces an earlier row with the same name, also when the later
    row is disabled or hidden. The row stays at the place of the first row with
    its name (``LIBRARY_MANAGER::Rows`` of KiCad 10).
    """
    # A dict keeps the place of the first key when a later value replaces it.
    active: dict[str, LibraryRow] = {}
    for row in rows:
        active[row.name] = row
    return list(active.values())


def find_library(
    kind: str,
    name: str,
    version: str,
    project_file: Path | None = None,
    rows: tuple[list[LibraryRow], list[str]] | None = None,
) -> tuple[LibraryRow | None, list[str]]:
    """Return the row that KiCad uses for the library name, and the table problems.

    The row can be disabled: then KiCad does not load the library. KiCad uses
    no row of a table that it cannot read (``LIBRARY_TABLE`` of KiCad 10 is
    then not OK), thus the row is the same as in KiCad. KiCad shows the
    problems to the user.

    Args:
        rows: The result of library_rows for the same kind, version and
            project, to find more than one library with one read of the tables.

    Returns:
        (row or None, problems of library_rows).

    Raises:
        LibraryTableError: The version or kicad_common.json cannot be read.
    """
    rows, problems = rows if rows is not None else library_rows(kind, version, project_file)
    for row in active_rows(rows):
        if row.name == name:
            return row, problems
    return None, problems
