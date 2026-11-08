"""New KiCad projects, as the project manager of the installed KiCad makes them.

The rules come from the KiCad source of each release (tags 8.0.0 to 10.0.7):

- KICAD_MANAGER_CONTROL::NewProject: KiCad 8 and 9 call
  KICAD_MANAGER_FRAME::CreateNewProject. KiCad 10 makes the project from the
  template "default" (ensureDefaultProjectTemplate, EnsureDefaultProjectTemplate
  from 10.0.5), then calls CreateNewProject.
- KICAD_MANAGER_CONTROL::NewFromTemplate: PROJECT_TEMPLATE::GetFileList finds
  the files of the template (FILE_TRAVERSER), CreateProject copies them with new
  names, then CreateNewProject adds the files that are missing.
- KICAD_MANAGER_FRAME::CreateNewProject: a missing project file is a copy of
  the first "kicad.kicad_pro" of the search path of KiCad (else "{\\n}\\n"), and
  a missing schematic and board get a stub.

KiCad does not change the UUIDs, the title block or the project file of a
template. A release with no known rules gives an error.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError
from ..parsers.kicad_versions import RELEASES, Release, kicad_release, verified_triple
from .file_save import AfterSaveError, save_file
from .kicad_cli import installed_kicad_problem, no_kicad_message
from .kicad_text import write_kicad_text
from .library_tables import KicadEnvironment, expand, kicad_documents_dir

PROJECT_EXT = ".kicad_pro"
LEGACY_PROJECT_EXT = ".pro"
META_DIR = "meta"
DEFAULT_TEMPLATE = "default"
# Windows: FILE_ATTRIBUTE_HIDDEN and FILE_ATTRIBUTE_READONLY.
_HIDDEN = 0x2
_READONLY = 0x1


class NewProjectError(InputError):
    """KiCad of this version makes no project here, or the server has no rules for it."""


@dataclass(frozen=True)
class Rules:
    """The rules of one installed KiCad version (major, minor, patch)."""

    version: tuple[int, int, int]
    release: Release

    @property
    def gitignore(self) -> bool:
        """KiCad 10 reads the .gitignore files of a template (FILE_TRAVERSER)."""
        return self.version >= (10, 0, 0)

    @property
    def lock_by_extension(self) -> bool:
        """KiCad 8.0.0 to 8.0.7 skip a file whose extension holds "lck". Later
        releases skip a file whose name starts with "~" (FILEEXT::LockFilePrefix)."""
        return self.version < (8, 0, 8)

    @property
    def skip_autosave(self) -> bool:
        """KiCad 8 and 9 skip a file whose name starts with "_autosave-"."""
        return self.version < (10, 0, 0)

    @property
    def rename_folders(self) -> bool:
        """KiCad 10 also renames the folders of a template with its name."""
        return self.version >= (10, 0, 0)

    @property
    def make_writable(self) -> bool:
        """KiCad 10.0.7 makes each copied file writable (KIPLATFORM::IO::MakeWriteable)."""
        return self.version >= (10, 0, 7)

    @property
    def new_stub(self) -> bool:
        """The schematic stub with a UUID and sheet_instances (from KiCad 9.0.2)."""
        return self.version >= (9, 0, 2)


def rules(kicad_version: str | None) -> Rules:
    """The rules of the installed KiCad version (kicad-cli version).

    Raises:
        NewProjectError: The version is not known, a release candidate or a nightly
            build (installed_kicad_problem).
    """
    if not kicad_version:
        raise NewProjectError(no_kicad_message())
    if problem := installed_kicad_problem(kicad_version):
        raise NewProjectError(problem)
    # A patch release after the newest verified one gets the rules of that one
    # (verified_triple; the tool call gets the warning of installed_kicad_warning).
    triple = verified_triple(kicad_version)
    release = kicad_release(kicad_version)
    if triple is None or release is None or triple < (8, 0, 0):
        newest = RELEASES[-1].verified_name
        raise NewProjectError(
            f"The server has no rules for a new project of KiCad {kicad_version}: it has the "
            f"rules of the KiCad releases 8.0.0 to {newest}, from the KiCad source. Create "
            "the project in KiCad."
        )
    return Rules(triple, release)


def schematic_stub(rules: Rules) -> str:
    """The schematic that CreateNewProject writes when the project has none."""
    release = rules.release
    if not rules.new_stub:
        return (
            f'(kicad_sch (version {release.schematic}) (generator "eeschema") '
            f'(generator_version "{release.name}")\n'
            '  (paper "A4")\n  (lib_symbols)\n'
            "  (symbol_instances)\n)\n"
        )
    return (
        "(kicad_sch\n"
        f"\t(version {release.schematic})\n"
        '\t(generator "eeschema")\n'
        f'\t(generator_version "{release.name}")\n'
        f"\t(uuid {uuid.uuid4()})\n"
        '\t(paper "A4")\n'
        "\t(lib_symbols)\n"
        "\t(sheet_instances\n"
        '\t\t(path "/"\n'
        '\t\t\t(page "1")\n'
        "\t\t)\n"
        "\t)\n"
        "\t(embedded_fonts no)\n"
        ")"
    )


def board_stub(rules: Rules) -> str:
    """The board that CreateNewProject writes when the project has none."""
    release = rules.release
    return (
        f'(kicad_pcb (version {release.board}) (generator "pcbnew") '
        f'(generator_version "{release.name}")\n)'
    )


def default_project_file(rules: Rules) -> str:
    """The project file of the template "default" that KiCad 10 makes."""
    if rules.version >= (10, 0, 7):
        return '{\n  "meta": {\n    "version": 1\n  }\n}\n'
    return "{}"


# The empty project file of CreateNewProject when it finds no "kicad.kicad_pro".
EMPTY_PROJECT_FILE = "{\n}\n"


def _versioned(rules: Rules, env: KicadEnvironment, base: str) -> str | None:
    """ENV_VAR::GetVersionedEnvVarValue: KICAD<major>_<base>, else the first other
    name in the order of the names (ENV_VAR_MAP is a std::map). KiCad 8.0.0 to
    10.0.4 take a name that matches "KICAD*_<base>" (also KICAD_USER_TEMPLATE_DIR),
    from 10.0.5 a name "KICAD<digits>_<base>" (IsVersionedEnvVar)."""
    exact = f"KICAD{env.major}_{base}"
    if exact in env.variables:
        return env.variables[exact]
    for name in sorted(env.variables):
        if not (name.startswith("KICAD") and name.endswith(f"_{base}")):
            continue
        middle = name[len("KICAD") : -len(base) - 1]
        if rules.version < (10, 0, 5) or (middle and middle.isdigit()):
            return env.variables[name]
    return None


def _expanded(env: KicadEnvironment, value: str) -> Path | None:
    """ExpandEnvVarSubstitutions, and no folder for a value that still holds a
    reference (KiCad from 10.0.4)."""
    value = expand(value, env)
    if "${" in value or "$(" in value:
        return None
    return Path(value)


def _search_value(rules: Rules, env: KicadEnvironment, value: str | None) -> Path | None:
    """A template folder of the search path of "kicad.kicad_pro" (kicad.cpp): the
    value as it is, or expanded from KiCad 10.0.4 (insertExpanded)."""
    if not value:
        return None
    return _expanded(env, value) if rules.version >= (10, 0, 4) else Path(value)


def _normalized(path: Path | None) -> Path | None:
    """wxFileName::Normalize with FN_NORMALIZE_FLAGS: "~" is the home folder."""
    return Path(os.path.expanduser(str(path))) if path is not None else None


def _page_value(rules: Rules, env: KicadEnvironment, value: str | None) -> Path | None:
    """A page folder of the template selector: the value as it is (KiCad 8 and 9),
    with the variables expanded (10.0.0 to 10.0.3: wxFileName::Normalize with
    wxPATH_NORM_ENV_VARS; KiCad puts its variables into the environment of the
    process), or expanded with no unresolved reference (from 10.0.4,
    resolveTemplateDir). KiCad 10 also expands "~"."""
    if not value:
        return None
    if rules.version >= (10, 0, 4):
        return _normalized(_expanded(env, value))
    if rules.version >= (10, 0, 0):
        return _normalized(Path(expand(value, env)))
    return Path(value)


def _user_value(env: KicadEnvironment) -> str | None:
    return env.variables.get("KICAD_USER_TEMPLATE_DIR")


def user_template_dir(rules: Rules, env: KicadEnvironment) -> Path | None:
    """KICAD_USER_TEMPLATE_DIR as a page of the template selector ("User Templates")."""
    return _page_value(rules, env, _user_value(env))


def system_template_dir(rules: Rules, env: KicadEnvironment) -> Path | None:
    """KICAD<major>_TEMPLATE_DIR as a page of the template selector ("System
    Templates")."""
    return _page_value(rules, env, _versioned(rules, env, "TEMPLATE_DIR"))


def _user_templates_path(env: KicadEnvironment) -> Path:
    """PATHS::GetUserTemplatesPath: "template" in the documents folder of KiCad."""
    return kicad_documents_dir(f"{env.major}.{env.minor}") / "template"


def _system_bases(stock_dir: Path | None) -> list[Path]:
    """The system folders of SystemDirsAppend that the server knows: the folders
    of the environment variable KICAD, and the data folder of the installation.
    For each base, SystemDirsAppend adds base, base/kicad, base/kicad/share,
    base/share and base/share/kicad."""
    bases = [Path(p) for p in os.environ.get("KICAD", "").split(os.pathsep) if p]
    found = []
    for base in bases:
        if sys.platform == "darwin":  # not for macOS: #ifndef __WXMAC__
            found.append(base)
            continue
        if base.name == "bin":
            base = base.parent
        found += [base, base / "kicad", base / "kicad" / "share", base / "share"]
        found.append(base / "share" / "kicad")
    if stock_dir is not None:
        found.append(stock_dir)
    return found


def project_file_search_path(
    rules: Rules, env: KicadEnvironment, stock_dir: Path | None
) -> list[Path]:
    """The folders where CreateNewProject looks for "kicad.kicad_pro", in the
    order of PGM_KICAD::SysSearch (kicad.cpp): KICAD_USER_TEMPLATE_DIR, the user
    templates folder, KICAD<major>_TEMPLATE_DIR, then "template" of the system
    folders."""
    paths = []
    user = _search_value(rules, env, _user_value(env))
    if user is not None:
        paths.append(user)
    paths.append(_user_templates_path(env))
    system = _search_value(rules, env, _versioned(rules, env, "TEMPLATE_DIR"))
    if system is not None:
        paths.append(system)
    paths += [base / "template" for base in _system_bases(stock_dir)]
    return paths


def project_file_template(
    rules: Rules, env: KicadEnvironment, stock_dir: Path | None
) -> Path | None:
    """The "kicad.kicad_pro" that CreateNewProject copies, or None."""
    for folder in project_file_search_path(rules, env, stock_dir):
        candidate = folder / "kicad.kicad_pro"
        if candidate.is_file():
            return candidate
    return None


def default_template_dir(rules: Rules, env: KicadEnvironment) -> Path | None:
    """The folder of the template "default" of KiCad 10, or None when KiCad makes
    the project with no template: KiCad 8 and 9, or KiCad 10.0.0 to 10.0.4 with no
    usable KICAD_USER_TEMPLATE_DIR (10.0.0 to 10.0.3: the value as it is; 10.0.4:
    expanded). From 10.0.5 the template is in the user templates folder."""
    if rules.version < (10, 0, 0):
        return None
    if rules.version >= (10, 0, 5):
        # EnsureDefaultProjectTemplate normalizes the path (FN_NORMALIZE_FLAGS: "~").
        return _normalized(_user_templates_path(env)) / DEFAULT_TEMPLATE
    value = _user_value(env)
    if not value:
        return None
    folder = _normalized(_expanded(env, value)) if rules.version >= (10, 0, 4) else Path(value)
    return folder / DEFAULT_TEMPLATE if folder is not None else None


def _pages(rules: Rules, env: KicadEnvironment, default_dir: Path | None) -> list[Path]:
    """The page folders of the template selector. From 10.0.5 also the template
    "default" itself (when KiCad could make it: default_dir), when the user and
    system pages do not hold its folder."""
    pages = [p for p in (user_template_dir(rules, env), system_template_dir(rules, env)) if p]
    if rules.version >= (10, 0, 5) and default_dir is not None:
        root = default_dir.parent
        if all(os.path.abspath(root) != os.path.abspath(page) for page in pages):
            pages.append(default_dir)
    return pages


def _page_templates(rules: Rules, page: Path) -> list[Path]:
    """The templates of a page (DIALOG_TEMPLATE_SELECTOR): the page itself when it
    has a "meta" folder, else its folders (from 10.0.1 only the folders with a
    "meta" folder)."""
    if not page.is_dir():
        return []
    if (page / META_DIR).is_dir():
        return [page]
    folders = [p for p in page.iterdir() if p.is_dir() and not _hidden(p)]
    if rules.version >= (10, 0, 1):
        folders = [p for p in folders if (p / META_DIR).is_dir()]
    return sorted(folders)


def find_template(
    rules: Rules, env: KicadEnvironment, name: str, default_dir: Path | None = None
) -> Path:
    """The template folder of a name (the folder name) on the pages of the
    template selector of KiCad (default_dir: the template "default" that KiCad 10
    made, or None).

    Raises:
        NewProjectError: No page or more than one page has the template.
    """
    found = [
        t
        for page in _pages(rules, env, default_dir)
        for t in _page_templates(rules, page)
        if t.name == name
    ]
    if not found:
        raise NewProjectError(
            f"KiCad has no template '{name}'. Templates: "
            f"{', '.join(list_templates(rules, env, default_dir)) or '(none)'}. Give the name, "
            "or the "
            "path of a template folder."
        )
    if len(found) > 1:
        raise NewProjectError(
            f"More than one template has the name '{name}': "
            f"{', '.join(str(p) for p in found)}. Give the path of the template folder."
        )
    return found[0]


def list_templates(
    rules: Rules, env: KicadEnvironment, default_dir: Path | None = None
) -> list[str]:
    """The names of the templates on the pages of the template selector."""
    pages = _pages(rules, env, default_dir)
    return sorted({t.name for page in pages for t in _page_templates(rules, page)})


def _hidden(path: Path) -> bool:
    """A hidden file or folder, which wxDir skips with no wxDIR_HIDDEN."""
    if sys.platform == "win32":
        return bool(getattr(path.stat(), "st_file_attributes", 0) & _HIDDEN)
    return path.name.startswith(".")


def _wild_match(pattern: str, name: str) -> bool:
    """wxMatchWild with dot_special: "*" and "?" are the wildcards, and a name
    that starts with "." does not match."""
    if name.startswith("."):
        return False

    def match(p: int, n: int) -> bool:
        while p < len(pattern):
            char = pattern[p]
            if char == "*":
                return any(match(p + 1, k) for k in range(n, len(name) + 1))
            if n >= len(name) or (char != "?" and char != name[n]):
                return False
            p += 1
            n += 1
        return n == len(name)

    return match(0, 0)


def _ignore_rules(folder: Path) -> list[str] | None:
    """The patterns of the .gitignore of a folder (EnsureGitFiles), or None."""
    path = folder / ".gitignore"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _ignored(patterns: list[str] | None, name: str, is_dir: bool) -> bool:
    for pattern in patterns or ():
        dir_only = pattern.endswith("/")
        if dir_only and not is_dir:
            continue
        if _wild_match(pattern[:-1] if dir_only else pattern, name):
            return True
    return False


def split_name(name: str) -> tuple[str, str | None]:
    """wxFileName::GetName and GetExt: the extension is after the last ".", and a
    name with only a first "." has no extension (None). "a." has the empty
    extension: the "." stays (wxFileName::HasExt)."""
    dot = name.rfind(".")
    if dot <= 0:
        return name, None
    return name[:dot], name[dot + 1 :]


def template_entries(rules: Rules, template_dir: Path) -> list[Path]:
    """The files and folders of a template that CreateProject copies, as
    PROJECT_TEMPLATE::GetFileList finds them (FILE_TRAVERSER, wxDir::Traverse
    with no hidden items)."""
    meta = str(template_dir / META_DIR)
    entries: list[Path] = []
    # m_gitIgnores of FILE_TRAVERSER: the patterns of each folder, read one time.
    patterns_of: dict[Path, list[str] | None] = {}

    def git_files(folder: Path) -> list[str] | None:
        if folder not in patterns_of:
            for name in (".gitignore", ".gitattributes"):
                if (folder / name).is_file():
                    entries.append(folder / name)
            patterns_of[folder] = _ignore_rules(folder) if rules.gitignore else None
        return patterns_of[folder]

    def walk(folder: Path) -> None:
        items = sorted(p for p in folder.iterdir() if not _hidden(p))
        files = [p for p in items if p.is_file()]
        folders = [p for p in items if p.is_dir()]
        patterns = None
        if rules.gitignore and items:
            patterns = git_files(folder)
        for path in files:
            if not rules.gitignore:
                git_files(folder)  # KiCad 8 and 9: with the first file of the folder
            elif _ignored(patterns, path.name, False):
                continue
            stem, ext = split_name(path.name)
            skip = "fp-info-cache" in stem
            if rules.skip_autosave:
                skip = skip or stem.startswith("_autosave-")
            if rules.lock_by_extension:
                skip = skip or "lck" in (ext or "")
            else:
                skip = skip or stem.startswith("~")
            if not skip:
                entries.append(path)
        for path in folders:
            if (
                str(path).startswith(meta)
                or str(path).endswith("-backups")
                or (
                    rules.gitignore and (path.name == ".git" or _ignored(patterns, path.name, True))
                )
            ):
                continue
            entries.append(path)
            if rules.gitignore:
                git_files(path)
            walk(path)

    walk(template_dir)
    return entries


def template_basename(template_dir: Path, entries: list[Path]) -> str:
    """The project name of a template: the name of its project files, or the
    folder name when they have more than one name."""
    names = {
        split_name(p.name)[0]
        for p in entries
        if p.is_file() and split_name(p.name)[1] in ("kicad_pro", "pro")
    }
    if len(names) > 1:
        return template_dir.name
    return names.pop() if names else ""


def _rename(text: str, basename: str, project_name: str) -> str:
    # wxString::Replace replaces each occurrence, and nothing for an empty text.
    return text.replace(basename, project_name) if basename else text


@dataclass(frozen=True)
class Copy:
    """One item that CreateProject copies: a file, or a folder that it makes."""

    source: Path
    target: Path
    folder: bool


def template_copies(
    rules: Rules, template_dir: Path, target_dir: Path, project_name: str
) -> list[Copy]:
    """The targets of PROJECT_TEMPLATE::CreateProject.

    The name of a file gets the project name in the place of the template name,
    but not a drawing sheet (.kicad_wks), a legacy symbol library (.lib, .dcm)
    or a file in a footprint library (.pretty). KiCad 10 also renames each
    folder with the template name (SEP + name + SEP, SEP + name + "-"). KiCad
    does this in the full path, also in the folders above the project folder:
    the server renames only the folders in the project folder, because KiCad
    would else write outside the project folder.
    """
    entries = template_entries(rules, template_dir)
    basename = template_basename(template_dir, entries)
    copies = []
    for entry in entries:
        relative = entry.relative_to(template_dir)
        folder_parts = list(relative.parts if entry.is_dir() else relative.parts[:-1])
        if rules.rename_folders and basename:
            path = "/" + "/".join(folder_parts) + "/"
            path = path.replace(f"/{basename}/", f"/{project_name}/")
            path = path.replace(f"/{basename}-", f"/{project_name}-")
            folder_parts = [p for p in path.split("/") if p]
        folder = target_dir.joinpath(*folder_parts)
        if entry.is_dir():
            copies.append(Copy(entry, folder, True))
            continue
        stem, ext = split_name(entry.name)
        if ext == "kicad_wks":
            name = stem
        elif stem.endswith(("-cache", "-rescue")):
            name = _rename(stem, basename, project_name)
        elif ext in ("dcm", "lib") or str(entry.parent).endswith(".pretty"):
            name = stem
        else:
            name = _rename(stem, basename, project_name)
        copies.append(Copy(entry, folder / (name if ext is None else f"{name}.{ext}"), False))
    return copies


def ensure_default_template(
    rules: Rules, env: KicadEnvironment
) -> tuple[Path | None, bool, str | None]:
    """Make the template "default" of KiCad 10 when it is not there, as
    ensureDefaultProjectTemplate (10.0.0 to 10.0.4) and
    EnsureDefaultProjectTemplate (from 10.0.5) do it: the folder, meta/info.html
    and default.kicad_pro. KiCad makes them at each "New Project".

    Returns:
        (the template folder, True when the tool made a file of it, None or a
        warning: the file has its content, but a step after the save failed), or
        (None, False, the reason) when KiCad has no template "default": KiCad 8
        and 9, no usable KICAD_USER_TEMPLATE_DIR (10.0.0 to 10.0.4), or a file
        of the template that cannot be written (KiCad then gives an invalid
        wxFileName).
    """
    folder = default_template_dir(rules, env)
    if folder is None:
        if rules.version < (10, 0, 0):
            return None, False, 'KiCad 8 and 9 have no template "default"'
        return None, False, "KICAD_USER_TEMPLATE_DIR is empty or holds an unresolved variable"
    made = False
    saved: list[str] = []
    try:
        info = folder / META_DIR / "info.html"
        if not info.is_file():
            info.parent.mkdir(parents=True, exist_ok=True)
            # wxFFile "w": text mode, as write_kicad_text.
            _save_new(
                info,
                "<html><head><title>Default</title></head><body>"
                "<h3>Default KiCad project template.</h3></body></html>",
                saved,
            )
            made = True
        project_file = folder / f"{DEFAULT_TEMPLATE}{PROJECT_EXT}"
        if not project_file.is_file():
            _save_new(project_file, default_project_file(rules), saved)
            made = True
    except OSError as e:
        return None, made, f"{folder} cannot be made ({e.strerror or e})"
    return folder, made, " ".join(saved) or None


@dataclass
class Plan:
    """The files of a new project, before the tool writes one."""

    target_dir: Path
    project_name: str
    # The items of the template (CreateProject).
    copies: list[Copy]
    # CreateNewProject: the files that the project does not have after the copy.
    write_project_file: bool
    project_file_source: Path | None
    write_schematic: bool
    write_board: bool
    # The files that exist and that the project replaces (a template), or the
    # files of the project folder (a project with no template: KiCad asks
    # before it uses a folder that is not empty).
    conflicts: list[Path]
    # True after write_project copied project_file_source.
    project_file_copied: bool = False

    def path(self, ext: str) -> Path:
        return self.target_dir / f"{self.project_name}{ext}"


def plan_project(
    rules: Rules,
    env: KicadEnvironment,
    stock_dir: Path | None,
    target_dir: Path,
    project_name: str,
    template_dir: Path | None,
) -> Plan:
    """The files of a new project from a template, or with no template.

    Raises:
        NewProjectError: The project would have only a legacy project file
            (.pro): KiCad converts it, the server cannot.
        OSError: A folder of the template cannot be read.
    """
    copies = template_copies(rules, template_dir, target_dir, project_name) if template_dir else []
    new_files = {c.target for c in copies if not c.folder}

    def present(ext: str) -> bool:
        path = target_dir / f"{project_name}{ext}"
        return path in new_files or path.is_file()

    if not present(PROJECT_EXT) and present(LEGACY_PROJECT_EXT):
        raise NewProjectError(
            f"The project would have only the legacy project file {project_name}.pro. KiCad "
            "converts it to a .kicad_pro file, the server cannot: create the project in KiCad."
        )
    write_project_file = not present(PROJECT_EXT)
    if template_dir is not None:
        conflicts = sorted(c.target for c in copies if not c.folder and c.target.exists())
    else:
        # KICAD_MANAGER_CONTROL::newProjectDirectory: wxDir::HasFiles.
        conflicts = (
            sorted(p for p in target_dir.iterdir() if p.is_file()) if target_dir.is_dir() else []
        )
    return Plan(
        target_dir=target_dir,
        project_name=project_name,
        copies=copies,
        write_project_file=write_project_file,
        project_file_source=(
            project_file_template(rules, env, stock_dir) if write_project_file else None
        ),
        write_schematic=not present(".kicad_sch"),
        write_board=not present(".kicad_pcb") and not present(".brd"),
        conflicts=conflicts,
    )


def write_project(rules: Rules, plan: Plan) -> tuple[list[Path], list[str]]:
    """Write the files of the plan: the copies of the template, then the files
    of CreateNewProject.

    As PROJECT_TEMPLATE::CreateProject: a folder that cannot be made is skipped
    with the items in it, and the project continues; a file that cannot be copied
    stops the project after the other copies (KiCad then does not call
    CreateNewProject). As CreateNewProject: a project file that cannot be copied
    gives the empty project file.

    Returns:
        (the files and folders that the tool wrote, the warnings).

    Raises:
        NewProjectError: Files of the template cannot be copied.
        OSError: A file of CreateNewProject cannot be written.
    """
    # NewProject and NewFromTemplate: Mkdir, then IsDirWritable before a copy.
    plan.target_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(plan.target_dir, os.W_OK):
        raise NewProjectError(f"Insufficient permissions to write to folder '{plan.target_dir}'.")
    written: list[Path] = []
    warnings: list[str] = []
    failed: list[str] = []
    for item in plan.copies:
        folder = item.target if item.folder else item.target.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            warnings.append(
                f"KiCad cannot make the folder {folder} ({e.strerror or e}): it skips "
                f"{item.source.name}, and so does the tool."
            )
            continue
        if item.folder:
            written.append(item.target)
            continue
        try:
            warning = copy_file(rules, item.source, item.target)
        except OSError as e:
            failed.append(f"{item.target} ({e.strerror or e})")
            continue
        written.append(item.target)
        if warning:
            warnings.append(warning)
    if failed:
        copied = ", ".join(str(p) for p in written) or "none"
        raise NewProjectError(
            f"The tool cannot copy these files of the template: {'; '.join(failed)}. As "
            f"KiCad, it copied the other items ({copied}) and wrote no other file."
        )
    if plan.write_project_file:
        target = plan.path(PROJECT_EXT)
        source = plan.project_file_source
        try:
            if source is None:
                raise FileNotFoundError("no kicad.kicad_pro")
            # wxCopyFile, with no MakeWriteable in CreateNewProject.
            copy_file(rules, source, target, writable=False)
            plan.project_file_copied = True
        except OSError as e:
            if source is not None:
                warnings.append(
                    f"KiCad cannot copy {source} ({e.strerror or e}): the project file is "
                    "the empty project file, as KiCad writes it."
                )
            _save_new(target, EMPTY_PROJECT_FILE.encode("utf-8"), warnings)
        written.append(target)
    # CreateNewProject writes the stub files with wxFFile "wb": LF on each system.
    if plan.write_schematic:
        _save_new(plan.path(".kicad_sch"), schematic_stub(rules).encode("utf-8"), warnings)
        written.append(plan.path(".kicad_sch"))
    if plan.write_board:
        _save_new(plan.path(".kicad_pcb"), board_stub(rules).encode("utf-8"), warnings)
        written.append(plan.path(".kicad_pcb"))
    return written, warnings


def _save_new(path: Path, data: str | bytes, warnings: list[str]) -> None:
    """Save a file of the new project: a text in text mode (write_kicad_text), bytes as
    they are. An error after the save (the file has its content) goes into the
    warnings, and the tool makes the other files.

    Raises:
        OSError: The file cannot be written.
    """
    try:
        if isinstance(data, str):
            write_kicad_text(path, data)
        else:
            save_file(path, data)
    except AfterSaveError as e:
        warnings.append(str(e))


def copy_file(rules: Rules, source: Path, target: Path, writable: bool = True) -> str | None:
    """wxCopyFile, then (KiCad 10.0.7, when writable) MakeWriteable.

    wxCopyFile on Linux and macOS opens the source, removes the target (a link
    is replaced, the file that it points to stays), makes the target with the
    permissions of the source and copies the content. Thus a target that is the
    source keeps its content. On Windows, CopyFile cannot copy a file onto itself.
    KiCad ignores an error of MakeWriteable: the tool gives it as a warning.

    Returns:
        A warning, or None.

    Raises:
        OSError: The file cannot be copied.
    """
    if sys.platform == "win32":
        if target.exists() and os.path.samefile(source, target):
            raise OSError(f"{target} is the source file")
        shutil.copy(source, target)
    else:
        with open(source, "rb") as src:
            if target.is_symlink() or target.exists():
                target.unlink()
            with open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        shutil.copymode(source, target)
    if not (writable and rules.make_writable):
        return None
    try:
        if sys.platform == "win32":
            attributes = getattr(target.stat(), "st_file_attributes", 0)
            if attributes & (_READONLY | _HIDDEN):
                import ctypes

                new = attributes & ~(_READONLY | _HIDDEN)
                if not ctypes.windll.kernel32.SetFileAttributesW(str(target), new):
                    raise OSError(f"Windows cannot change the attributes of {target}")
        else:
            target.chmod(target.stat().st_mode | stat.S_IWUSR)
    except OSError as e:
        return f"{target} is not writable ({e}). KiCad ignores this error, and so does the tool."
    return None
