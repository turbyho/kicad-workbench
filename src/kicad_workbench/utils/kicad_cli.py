"""kicad-cli invocation helpers.

Centralizes two concerns that every kicad-cli call site needs:

1. Locating the executable. On Windows, KiCad's installer does NOT add its
   bin directory to PATH, so a bare "kicad-cli" only works if the user
   patched PATH by hand. We fall back to the detected install location
   (``find_kicad_install``), where kicad-cli sits next to the versioned
   ``share/kicad`` directory.
2. Running it off the asyncio event loop. All MCP tools are ``async def``;
   calling ``subprocess.run`` inline blocks the whole server for the duration
   of an ERC/DRC/export (up to minutes), so callers must use the async
   ``run_kicad_cli`` wrapper instead.

The sync ``run_kicad_cli_sync`` variant exists for tests and other
non-async contexts.
"""

import asyncio
import codecs
import functools
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.kicad_versions import version_numbers, version_triple
from .kicad_project import (
    PRO_SUFFIX,
    is_declared,
    top_level_sheets,
)
from .kicad_text import read_kicad_json
from .kicad_version import find_kicad_install
from .library_tables import kicad_settings_dir
from .sch_hierarchy import (
    SheetPath,
    netlist_root,
    root_sheet_paths,
    schematic_project_file,
    sheet_paths,
)

_cached_exe: str | None = None


class KicadCliError(WorkbenchError):
    """kicad-cli cannot run, did not stop in time, or did not write its output. The
    message tells why."""


def find_kicad_cli() -> str | None:
    """Locate the kicad-cli executable.

    Resolution order:
    1. ``KICAD_CLI`` environment variable (explicit user override).
    2. ``shutil.which("kicad-cli")`` (PATH lookup — default on Linux/macOS
       and on Windows installs whose bin dir was added to PATH).
    3. Known KiCad install locations via ``find_kicad_install()``.

    Returns the executable path, or None when KiCad cannot be found.
    """
    global _cached_exe
    if _cached_exe is not None:
        return _cached_exe

    override = os.environ.get("KICAD_CLI")
    if override and Path(override).is_file():
        _cached_exe = override
        return _cached_exe

    found = shutil.which("kicad-cli")
    if found:
        _cached_exe = found
        return _cached_exe

    install = find_kicad_install()
    if install:
        install_path, _version = install
        exe_name = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
        # Windows: <install>/bin/kicad-cli.exe
        # macOS: install_path is .../Contents/SharedSupport, the binary is
        #        in the sibling MacOS directory.
        # Linux: distro packages put kicad-cli on PATH, which step 2 found.
        candidates = [
            install_path / "bin" / exe_name,
            install_path.parent / "MacOS" / "kicad-cli",
        ]
        for candidate in candidates:
            if candidate.is_file():
                _cached_exe = str(candidate)
                return _cached_exe

    return None


def _codec(name: str) -> str | None:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def fs_encoding() -> str:
    """Return the encoding that this process uses for file names and arguments.

    It is the locale encoding, or UTF-8 in the UTF-8 mode of Python (the
    default from Python 3.15, PEP 686) and on macOS.
    """
    return _codec(sys.getfilesystemencoding()) or "utf-8"


_LOCALE_PROBE = "import locale, sys; locale.setlocale(locale.LC_CTYPE, sys.argv[1])"


@functools.cache
def _locale_exists(name: str) -> bool:
    # A probe in a child process: setlocale() in this process is not safe
    # while other threads run. A probe that cannot run counts as a locale
    # that does not exist.
    if not sys.executable:
        return False
    try:
        probe = subprocess.run(
            [sys.executable, "-I", "-c", _LOCALE_PROBE, name], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@functools.cache
def child_ctype() -> str | None:
    """Return the LC_CTYPE that kicad-cli gets on Linux and macOS, or None.

    kicad-cli decodes its arguments (the file paths) and encodes its output
    with the character set of LC_CTYPE. This process encodes the paths with
    fs_encoding(), thus the character set must be the same. The LC_CTYPE of
    this process has it, except in the UTF-8 mode of Python with a locale
    that is not UTF-8 (for example zh_CN.GBK), and on macOS in the C locale.
    Then the function takes a UTF-8 locale that exists. If no UTF-8 locale
    exists, it gives None (see run_kicad_cli_sync()).
    """
    current = locale.setlocale(locale.LC_CTYPE)
    encoding = fs_encoding()
    if encoding != "utf-8" or _codec(locale.nl_langinfo(locale.CODESET)) == encoding:
        return current
    language = current.split(".")[0].split("@")[0]
    for name in (f"{language}.UTF-8", "C.UTF-8", "C.utf8", "en_US.UTF-8", "UTF-8"):
        if _locale_exists(name):
            return name
    return None


def kicad_cli_env(english: bool = False) -> dict[str, str] | None:
    """Return the environment for kicad-cli, or None for the environment of
    this process.

    With ``english``, the function removes all LC_ variables of the user and
    sets LANG to "C", which each system has. This keeps a decimal comma of the
    user (LC_NUMERIC) out of the report. The language of kicad-cli comes from
    its settings (see english_settings(), which run_kicad_cli_sync() uses):
    KiCad takes LANG only when the language of its settings is "Default".

    LC_CTYPE is child_ctype(). A locale that does not exist (for example
    "C.UTF-8" on macOS or on an old Linux) would put kicad-cli in the "C"
    locale, and it would not load a file whose path is not ASCII.

    On Windows kicad-cli uses the UTF-8 code page from its manifest
    (activeCodePage, KiCad 6 and later) and Unicode arguments, thus its
    environment does not change.
    """
    if sys.platform == "win32":
        return None
    current = locale.setlocale(locale.LC_CTYPE)
    # Without a locale of the correct character set, ASCII text stays
    # correct (run_kicad_cli_sync() refuses other text).
    ctype = child_ctype() or current
    env = dict(os.environ)
    if english:
        env = {name: value for name, value in env.items() if not name.startswith("LC_")}
        env.update(LANG="C", LANGUAGE="", LC_CTYPE=ctype)
    elif ctype != current:
        if "LC_ALL" in env:
            # LC_ALL overrides LC_CTYPE. Its value stays the locale of the
            # other categories.
            lc_all = env.pop("LC_ALL")
            env = {name: value for name, value in env.items() if not name.startswith("LC_")}
            env["LANG"] = lc_all
        env["LC_CTYPE"] = ctype
    else:
        return None
    return env


def decode_output(data: bytes | None) -> str:
    """Decode the stdout or the stderr of kicad-cli.

    kicad-cli writes with the character set of its LC_CTYPE, which is
    fs_encoding() (see kicad_cli_env()). On Windows it writes UTF-8.
    """
    if not data:
        return ""
    encoding = "utf-8" if sys.platform == "win32" else fs_encoding()
    return data.decode(encoding, errors="replace")


# The labels of the list of languages of KiCad (LanguagesList of common/pgm_base.cpp,
# KiCad 8.0.9, 9.0.9 and 10.0.6). "English" is not translated; "Default" matches no
# label of the list when KiCad runs in English, thus it also gives wxLANGUAGE_DEFAULT.
_ENGLISH = "English"
_DEFAULT = "Default"


def _english_language() -> str:
    """Return the language of the settings that gives an English kicad-cli.

    On Linux and other Unix systems: "Default". kicad-cli then takes the language
    of its environment (wxLocale::Init with wxLANGUAGE_DEFAULT), and
    kicad_cli_env(english=True) gives LANG=C and the LC_CTYPE of the file paths. A
    language of the settings makes wxLocale set all categories of the locale, also
    LC_CTYPE: in a locale that is not UTF-8, kicad-cli then cannot load a file
    whose path is not ASCII. On Windows and macOS the environment does not give
    the language: "English". There kicad-cli gets the file paths as Unicode
    (Windows) or as UTF-8 (macOS).
    """
    return _ENGLISH if sys.platform in ("win32", "darwin") else _DEFAULT


def english_settings(directory: Path, version: str) -> None:
    """Make in directory a copy of the KiCad settings of the version, with the
    language of _english_language(), for KICAD_CONFIG_HOME of kicad-cli.

    kicad-cli takes its language from system.language of kicad_common.json
    (PGM_BASE::InitPgm calls SetLanguage, KiCad 8, 9 and 10), thus LANG gives
    English only when that language is "Default". The copy keeps the other
    settings: the path variables and the global library tables. The settings
    of the user do not change: the copy has the files, not symbolic links to
    them (a write of kicad-cli or of this function cannot go through a link
    into the settings of the user), and a link that points to no file is left
    out (KiCad does not find that file either). kicad_common.json is read as
    KiCad reads it (read_kicad_json).

    Raises:
        OSError: The settings cannot be copied.
        InputError: kicad_common.json is not JSON, or its system is not a JSON
            object.
        LibraryTableError: The version has no major and minor number.
    """
    source = kicad_settings_dir(version)
    target = directory / source.name
    if source.is_dir():
        shutil.copytree(source, target, symlinks=False, ignore_dangling_symlinks=True)
    else:
        target.mkdir(parents=True)
    common = target / "kicad_common.json"
    try:
        data = read_kicad_json(common) if common.is_file() else {}
    except InputError as e:  # its message names the copy
        raise InputError(f"{source / common.name} is not valid JSON: {e.__cause__}") from e
    if not isinstance(data, dict) or not isinstance(data.setdefault("system", {}), dict):
        raise InputError(f"{source / common.name}: system is not a JSON object.")
    data["system"]["language"] = _english_language()
    common.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def run_kicad_cli_sync(
    args: list[str],
    timeout: float = 60.0,
    english: bool = False,
) -> subprocess.CompletedProcess:
    """Run kicad-cli synchronously (blocking).

    ``english`` makes kicad-cli write its messages and reports in English, also
    when the user selected an other language in KiCad: kicad-cli gets a copy
    of the settings with the language of _english_language()
    (english_settings()) and the environment of kicad_cli_env(english=True).

    Raises KicadCliError when the settings cannot be copied, or when kicad-cli
    gives no version.

    Raises KicadCliError when an argument is not ASCII and no locale lets
    kicad-cli decode it as this process encodes it (see child_ctype()).

    Raises FileNotFoundError when kicad-cli cannot be located, so callers can
    distinguish "KiCad not installed" from a failed run.
    """
    exe = find_kicad_cli()
    if exe is None:
        raise FileNotFoundError(
            "kicad-cli not found in PATH or known KiCad install locations. "
            "Install KiCad 8 or later, or set the KICAD_CLI environment variable."
        )
    if sys.platform != "win32" and child_ctype() is None and not all(a.isascii() for a in args):
        raise KicadCliError(
            "kicad-cli cannot read a file path that is not ASCII on this system. "
            "This server writes file paths as UTF-8 (the UTF-8 mode of Python), but "
            "the system has no UTF-8 locale for kicad-cli. Install a UTF-8 locale "
            "(for example C.UTF-8), or start the server with PYTHONUTF8=0."
        )
    env = kicad_cli_env(english)
    if not english:
        return subprocess.run([exe] + args, capture_output=True, timeout=timeout, env=env)
    # The thread that runs this function owns the directory: a cancel of the tool
    # call does not stop kicad-cli.
    with tempfile.TemporaryDirectory(
        prefix="kicad-workbench-settings-", ignore_cleanup_errors=True
    ) as settings:
        version = kicad_cli_version_sync()
        if version is None or version_numbers(version) is None:
            raise KicadCliError(f"kicad-cli did not give its version: {version!r}")
        try:
            english_settings(Path(settings), version)
        except (OSError, WorkbenchError) as e:
            raise KicadCliError(f"The settings of KiCad cannot be copied for kicad-cli: {e}") from e
        env = dict(os.environ if env is None else env)
        env["KICAD_CONFIG_HOME"] = settings
        return subprocess.run([exe] + args, capture_output=True, timeout=timeout, env=env)


@dataclass(frozen=True)
class CliExport:
    """The output file and the messages of a kicad-cli export."""

    data: bytes  # The output file, as kicad-cli wrote it.
    messages: list[str]  # The lines of kicad-cli, less the line that names the output file.


def export_with_kicad_cli(
    command: list[str], source: Path, file_name: str, timeout: float
) -> CliExport:
    """Run a kicad-cli export into a new temporary directory, and return its file.

    kicad-cli gets ``[*command, "--output", <file>, <source>]``. The thread
    that runs this function owns the temporary directory: a cancel of the tool
    call does not stop kicad-cli. kicad-cli runs in English (see
    run_kicad_cli_sync()). The function finds the line that names the output
    file by the name of the directory, not by its text.

    Raises:
        KicadCliError: kicad-cli did not stop in time, or did not write the file.
        FileNotFoundError: kicad-cli was not found.
    """
    directory = Path(tempfile.mkdtemp(prefix="kicad-workbench-"))
    try:
        output = directory / file_name
        args = [*command, "--output", str(output), str(source)]
        try:
            result = run_kicad_cli_sync(args, timeout=timeout, english=True)
        except subprocess.TimeoutExpired as e:
            raise KicadCliError(f"kicad-cli did not stop in {timeout:g} s.") from e
        text = decode_output(result.stdout) + "\n" + decode_output(result.stderr)
        messages = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and directory.name not in line
        ]
        if result.returncode != 0 or not output.is_file():
            raise KicadCliError(
                f"kicad-cli stopped with the exit code {result.returncode}: " + " ".join(messages)
            )
        return CliExport(output.read_bytes(), messages)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def run_kicad_cli(
    args: list[str],
    timeout: float = 60.0,
    english: bool = False,
) -> subprocess.CompletedProcess:
    """Run kicad-cli off the event loop (non-blocking for the server).

    Raises FileNotFoundError when kicad-cli cannot be located — same contract
    as the sync variant, so existing ``except FileNotFoundError`` handlers
    keep working.
    """
    return await asyncio.to_thread(run_kicad_cli_sync, args, timeout, english)


def kicad_cli_version_sync() -> str | None:
    """Return the version of kicad-cli (e.g. "10.0.6"), or None if unknown. No
    cache: KiCad can change while the server runs (one run takes about 0.1 s)."""
    try:
        result = run_kicad_cli_sync(["version"], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return decode_output(result.stdout).strip() or None


async def get_kicad_version() -> str | None:
    """Return the installed KiCad version (e.g. "10.0.6"), or None if unknown."""
    return await asyncio.to_thread(kicad_cli_version_sync)


def cli_root(sch_path: Path, output: str, kicad: str | None = None) -> Path | None:
    """Return the schematic that kicad-cli gets for an output of the full project
    (the netlist, the ERC), when it is not sch_path; None when it is sch_path.

    kicad-cli loads the schematic that it gets and the project file of its name
    (EESCHEMA_HELPERS::LoadSchematic): thus the schematic of the project name
    (netlist_root, as for the BOM). A sub-sheet alone does not have the hierarchy
    that gives the names of the nets with no label. The function blocks: it can
    run kicad-cli for its version.

    Args:
        sch_path: A schematic file of the project.
        output: The name of the output in the error messages.
        kicad: The version of kicad-cli, when the caller has it; None: the
            function gets it when it needs it.

    Raises:
        AmbiguousRootError: More than one project holds the sheet.
        InputError: kicad-cli cannot write the output of the full project (more
            than one top-level sheet with kicad-cli 10.0.0 to 10.0.6, or a
            top-level sheet that is not the schematic of the project name).
    """
    project = schematic_project_file(sch_path)
    tops = top_level_sheets(project)
    if len(tops) <= 1:
        kicad = None
    else:
        # The version of kicad-cli only for a project with more than one top-level sheet.
        kicad = kicad or kicad_cli_version_sync()
        if kicad is None:
            raise InputError(
                f"The project {project} has {len(tops)} top-level sheets: the tool needs the "
                "version of kicad-cli, but kicad-cli does not give it (it is not there or it "
                "does not run)."
            )
    root, _tops = netlist_root(project, kicad, output)
    same = Path(root).resolve() == Path(sch_path).resolve()
    return None if same else root


def cli_sheet_paths(
    root: Path, kicad: str | None, project: Path | None = None
) -> tuple[list[SheetPath], list[str]]:
    """Return the sheet paths of the hierarchy that kicad-cli loads for a root
    schematic (cli_root), and the problems.

    kicad-cli 10.0.7 and later load all top-level sheets of the project when the
    project has more than one and the schematic is one of them
    (EESCHEMA_HELPERS::LoadSchematic, findDeclaredTopLevelSheet): the paths of
    sheet_paths, with the name of the top-level sheet in each path. Else kicad-cli
    loads the schematic as the one top-level sheet: the paths of root_sheet_paths.

    Args:
        root: The schematic that kicad-cli gets (cli_root).
        kicad: The version of kicad-cli; None: not known (a project with one
            top-level sheet does not need it).
        project: The project file (schematic_project_file); None: the project
            file of the name of root.
    """
    project = project or root.with_suffix(PRO_SUFFIX)
    numbers = version_triple(kicad) if kicad else None
    # The KIIDs of the parser of that kicad-cli (sheet_list.parsed_kiids).
    major = numbers[0] if numbers is not None else None
    tops = top_level_sheets(project)
    if len(tops) > 1 and is_declared(root, tops) and numbers is not None and numbers >= (10, 0, 7):
        return sheet_paths(project, major)
    return root_sheet_paths(root, major)
