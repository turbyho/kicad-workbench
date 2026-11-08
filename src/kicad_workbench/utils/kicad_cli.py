"""kicad-cli invocation helpers.

Centralizes two concerns that every kicad-cli call site needs:

1. Locating the executable (find_kicad_cli): the path of KICAD_CLI, then PATH,
   then the standard install folder of KiCad (kicad_version.standard_kicad_cli).
   On Windows, the installer of KiCad does not add its bin folder to PATH: the
   server then uses the newest version folder that has kicad-cli.
2. Running it off the asyncio event loop. All MCP tools are ``async def``;
   calling ``subprocess.run`` inline blocks the whole server for the duration
   of an ERC/DRC/export (up to minutes), so callers must use the async
   ``run_kicad_cli`` wrapper instead.

The sync ``run_kicad_cli_sync`` variant exists for tests and other
non-async contexts.
"""

import asyncio
import codecs
import contextvars
import functools
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.kicad_versions import (
    RELEASES,
    is_release_candidate,
    is_verified_nightly,
    kicad_release,
    unverified_reason,
    verified_triple,
    version_numbers,
    version_triple,
)
from .kicad_project import (
    PRO_SUFFIX,
    is_declared,
    top_level_sheets,
)
from .kicad_text import read_kicad_json
from .kicad_version import standard_kicad_cli
from .library_tables import kicad_settings_dir
from .sch_hierarchy import (
    SheetPath,
    netlist_root,
    root_sheet_paths,
    schematic_project_file,
    sheet_paths,
)

_cached_exe: str | None = None
# The version of kicad-cli in one tool call (utils/file_versions.py sets it): the
# check of the file versions and the tool use one version, and kicad-cli runs one time.
_call_version: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kicad_cli_call_version", default=None
)


# The step for the user when KiCad is not installed (also kicad_python.py).
INSTALL_KICAD = "install KiCad 8 or later (https://www.kicad.org/download/)"
# The answer of a tool that needs KiCad, when kicad-cli was not found.
NO_KICAD = (
    "KiCad is not installed: kicad-cli was not found. The tool works with the KiCad files "
    "as the installed KiCad does, thus it needs KiCad 8 or later. Tell the user: "
    f"{INSTALL_KICAD}, or give the path of kicad-cli in the environment variable KICAD_CLI."
)
# Why the last run of `kicad-cli version` gave no version (no_kicad_message).
_version_problem: str | None = None


def kicad_cli_override_problem() -> str | None:
    """Return why the path of the environment variable KICAD_CLI is not a kicad-cli, or
    None. A path that is not a file is an error of the user: find_kicad_cli does not
    use an other kicad-cli then (an other KiCad would give other rules)."""
    return override_problem(
        "KICAD_CLI",
        "the path of kicad-cli of KiCad 8 or later",
        "to use the kicad-cli of PATH",
    )


def override_problem(variable: str, value: str, without: str) -> str | None:
    """Return why the path of an environment variable (KICAD_CLI, KICAD_PYTHON) is not
    a file, or None. The finder does not use a different program then.

    Args:
        variable: The name of the variable.
        value: What the path must be, for the message.
        without: What the server does with no variable, for the message.
    """
    override = os.environ.get(variable)
    if override and not Path(override).is_file():
        return (
            f"The environment variable {variable} gives {override}, but this path is not "
            f"a file. Tell the user: correct {variable} ({value}), or remove it {without}."
        )
    return None


class KicadNotFoundError(FileNotFoundError):
    """kicad-cli was not found: KiCad is not installed (NO_KICAD), or the path of
    KICAD_CLI is not a file (kicad_cli_override_problem)."""

    def __init__(self) -> None:
        super().__init__(kicad_cli_override_problem() or NO_KICAD)


def no_kicad_message() -> str:
    """Return the answer of a tool when kicad_cli_version_sync gave no version: NO_KICAD
    when kicad-cli was not found (or the problem of KICAD_CLI), else the reason of the
    failure of kicad-cli (KiCad is installed: an install does not help). The function
    does not run kicad-cli again."""
    if _version_problem is None:
        return kicad_cli_override_problem() or NO_KICAD
    return (
        f"kicad-cli did not give its version: {_version_problem}. The tool needs the "
        'version of the installed KiCad. Tell the user: run "kicad-cli version" to check '
        "the installation of KiCad, or give the path of a kicad-cli that works in the "
        "environment variable KICAD_CLI."
    )


def installed_kicad_problem(kicad: str | None) -> str | None:
    """Return why a tool cannot work with the installed KiCad, or None. Each check of
    the installed KiCad version uses this function (CLAUDE.md rule 4).

    kicad is the version of kicad_cli_version_sync. These give a message:

    - None: no_kicad_message.
    - A version with no major number.
    - A KiCad before KiCad 8, the minimum.
    - A KiCad with no rules in the server: a release that is not in RELEASES (for
      example 11.0 or 10.1), and a nightly build other than the nightly build after
      the newest release (10.99, branch master).
    - A release candidate (for example 10.0.1-rc1): a test build, not a release.

    A development build of a release branch and a patch release after the newest
    verified one get the rules of the newest verified patch release, with the warning
    of installed_kicad_warning.
    """
    if not kicad:
        return no_kicad_message()
    numbers = version_numbers(kicad)
    if numbers is None:
        return f"The version {kicad!r} of kicad-cli has no major number."
    if numbers[0] < 8:
        return (
            f"KiCad {kicad} is older than KiCad 8, the minimum of this server. "
            f"Tell the user: {INSTALL_KICAD}."
        )
    known = ", ".join(release.name for release in RELEASES)
    if kicad_release(kicad) is None and not is_verified_nightly(kicad):
        build = "a nightly build" if numbers[1] == 99 else "a version"
        return (
            f"KiCad {kicad} is {build} that the server has no rules for. Tell the user: "
            f"use a KiCad release that the server knows ({known}), or look for a newer "
            "release of kicad-workbench."
        )
    if is_release_candidate(kicad):
        return (
            f"KiCad {kicad} is a release candidate (a test build): the server has the "
            f"rules of the KiCad releases only. Tell the user: use a KiCad release that "
            f"the server knows ({known})."
        )
    return None


def installed_kicad_warning(kicad: str | None) -> str | None:
    """Return the warning for a KiCad that the server did not compare with its rules,
    or None (kicad_versions.unverified_reason). The tools then use the rules of the
    newest verified patch release of the same release."""
    reason = unverified_reason(kicad) if kicad else None
    if reason is None:
        return None
    used = ".".join(map(str, verified_triple(kicad)))
    return (
        f"⚠️ KiCad {kicad} is {reason}. The server uses the rules of KiCad {used}, which "
        f"it compared with real KiCad: a result can be different from KiCad {kicad}."
    )


class KicadCliError(WorkbenchError):
    """kicad-cli cannot run, did not stop in time, or did not write its output. The
    message tells why."""


def find_kicad_cli() -> str | None:
    """Locate the kicad-cli executable.

    The order of the search:
    1. The environment variable KICAD_CLI. When it has a path that is not a file,
       the function gives None (kicad_cli_override_problem): no other kicad-cli.
    2. The kicad-cli of PATH (shutil.which).
    3. The kicad-cli of the standard install folder (standard_kicad_cli).

    Returns the path of kicad-cli, or None when it is not found.
    """
    global _cached_exe
    if _cached_exe is not None:
        if Path(_cached_exe).is_file():
            return _cached_exe
        _cached_exe = None  # KiCad was removed or moved while the server runs

    override = os.environ.get("KICAD_CLI")
    if override:
        if not Path(override).is_file():
            return None
        _cached_exe = override
        return _cached_exe

    found = shutil.which("kicad-cli")
    if found:
        _cached_exe = found
        return _cached_exe

    standard = standard_kicad_cli()
    if standard is not None:
        _cached_exe = str(standard)
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

    Raises KicadNotFoundError (a FileNotFoundError) when kicad-cli cannot be
    located, so callers can distinguish "KiCad not installed" from a failed run.
    """
    exe = find_kicad_cli()
    if exe is None:
        raise KicadNotFoundError()
    if sys.platform != "win32" and child_ctype() is None and not all(a.isascii() for a in args):
        raise KicadCliError(
            "kicad-cli cannot read a file path that is not ASCII on this system. "
            "This server writes file paths as UTF-8 (the UTF-8 mode of Python), but "
            "the system has no UTF-8 locale for kicad-cli. Install a UTF-8 locale "
            "(for example C.UTF-8), or start the server with PYTHONUTF8=0."
        )
    env = kicad_cli_env(english)
    if not english:
        return _run(exe, args, timeout, env)
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
        return _run(exe, args, timeout, env)


def _run(
    exe: str, args: list[str], timeout: float, env: dict[str, str] | None
) -> subprocess.CompletedProcess:
    """Run kicad-cli. A kicad-cli that the system cannot find now (KiCad was removed
    after find_kicad_cli) is KicadNotFoundError, when no other kicad-cli is found."""
    try:
        return subprocess.run([exe] + args, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        if Path(exe).is_file():
            raise
        other = find_kicad_cli()
        if other is None:
            raise KicadNotFoundError() from None
        return subprocess.run([other] + args, capture_output=True, timeout=timeout, env=env)


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


def set_call_version(version: str) -> contextvars.Token:
    """Keep the version of kicad-cli for the rest of a tool call (reset_call_version
    ends it)."""
    return _call_version.set(version)


def reset_call_version(token: contextvars.Token) -> None:
    """End the version of set_call_version."""
    _call_version.reset(token)


# The line of the full version in `kicad-cli version --format about`
# (GetVersionInfoData of common/build_version.cpp, KiCad 8, 9 and 10, not
# translated): "Version: <KICAD_VERSION_FULL>, release build".
# KICAD_VERSION_EXTRA of a package is free text: it can have spaces.
_ABOUT_VERSION = re.compile(r"^Version: (.+?), (?:release|debug) build\s*$", re.MULTILINE)


def kicad_cli_version_sync() -> str | None:
    """Return the full version of kicad-cli, or None if unknown: for example "10.0.6",
    "10.0.1-rc1" (a release candidate), "10.0.7-15-gabc1234" (a development build) or
    "10.99.0-1234-gabc1234" (a nightly build). `kicad-cli version` gives only the
    numbers (GetMajorMinorPatchVersion), thus the function reads the line "Version:" of
    `kicad-cli version --format about` (KICAD_VERSION_FULL). No cache between tool
    calls: KiCad can change while the server runs (one run takes about 0.1 s). In a
    tool call, the version of set_call_version. Each version that a tool call uses goes
    to note_kicad_version (the warning of installed_kicad_warning)."""
    known = _call_version.get()
    version = known if known is not None else _run_kicad_cli_version()
    if version is not None:
        note_kicad_version(version)
    return version


# The versions of KiCad that a tool call used (kicad_build_notes.KicadBuildNotes sets
# a list for each call).
_call_versions: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "kicad_cli_call_versions", default=None
)


def collect_kicad_versions() -> tuple[list[str], contextvars.Token]:
    """Start the collection of the KiCad versions of a tool call (note_kicad_version).
    reset_collection ends it."""
    versions: list[str] = []
    return versions, _call_versions.set(versions)


def reset_collection(token: contextvars.Token) -> None:
    """End the collection of collect_kicad_versions."""
    _call_versions.reset(token)


def note_kicad_version(version: str) -> None:
    """Add a KiCad version to the collection of the tool call, if there is one."""
    versions = _call_versions.get()
    if versions is not None and version not in versions:
        versions.append(version)


def _run_kicad_cli_version() -> str | None:
    """Run `kicad-cli version --format about` and return the full version, or None
    (_version_problem gives the reason)."""
    global _version_problem
    try:
        result = run_kicad_cli_sync(["version", "--format", "about"], timeout=30)
    except KicadNotFoundError:
        _version_problem = None
        return None
    except subprocess.TimeoutExpired:
        _version_problem = f"{find_kicad_cli()} did not stop in 30 s"
        return None
    except (OSError, KicadCliError) as e:
        _version_problem = f"{find_kicad_cli()}: {e}"
        return None
    output = decode_output(result.stdout)
    match = _ABOUT_VERSION.search(output)
    if result.returncode != 0 or match is None or version_triple(match.group(1)) is None:
        messages = (output + "\n" + decode_output(result.stderr)).strip()
        if result.returncode != 0:
            reason = f"gave the exit code {result.returncode}"
        else:
            reason = 'gave no line "Version: <major>.<minor>.<patch>..." in its about text'
        _version_problem = f"{find_kicad_cli()} {reason}" + (f": {messages}" if messages else "")
        return None
    _version_problem = None
    return match.group(1)


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
