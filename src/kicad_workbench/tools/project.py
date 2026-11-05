"""KiCad project creation tools for KiCad Workbench.

An empty project gets the files in the format of the installed KiCad: pcbnew
of that KiCad writes the board and the project file, and the schematic is an
empty sheet as that KiCad writes it (KiCad 10 and later: converted by
"kicad-cli sch upgrade"). Template projects are copied the way KiCad's "New
Project from Template" does it.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from ..parsers.kicad_versions import (
    KICAD8,
    KICAD9,
    KicadFileError,
    Release,
    schematic_text_version,
    version_numbers,
)
from ..parsers.sexpr import edited, parse, quote
from ..server import mcp
from ..utils.kicad_cli import decode_output, get_kicad_version, run_kicad_cli_sync
from ..utils.kicad_python import find_kicad_python, new_project_board_sync
from ..utils.kicad_text import (
    KiCadEncodingError,
    read_as_kicad,
    read_for_write,
    read_kicad_json,
    write_kicad_text,
)
from ..utils.kicad_version import find_kicad_install

EMPTY_TEMPLATE = "empty"

# The oldest major version of KiCad that the server supports.
_MINIMUM_MAJOR = 8
# The first major version of KiCad with "kicad-cli sch upgrade"
# (kicad/cli/command_sch_upgrade.cpp: not in tag 9.0.9, in tag 10.0.0).
_SCH_UPGRADE_MAJOR = 10


def _empty_schematic(release: Release) -> str:
    """The schematic of a new project, as KiCad of the release writes an empty
    sheet (SCH_IO_KICAD_SEXPR::Format): the header, the paper, an empty
    lib_symbols section and the root sheet instance. KiCad 9 and later also
    write embedded_fonts (not in tag 8.0.9)."""
    fonts = "\t(embedded_fonts no)\n" if release.schematic >= KICAD9.schematic else ""
    return (
        "(kicad_sch\n"
        f"\t(version {release.schematic})\n"
        '\t(generator "eeschema")\n'
        f'\t(generator_version "{release.name}")\n'
        f'\t(uuid "{uuid.uuid4()}")\n'
        '\t(paper "A4")\n'
        "\t(lib_symbols)\n"
        '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n'
        f"{fonts})\n"
    )


def _is_development(numbers: tuple[int, int]) -> bool:
    """True for a development build of KiCad: the minor version 99 (9.99, 10.99)."""
    return numbers[1] == 99


def _schematic_start(numbers: tuple[int, int], cli_readable: bool) -> tuple[Release, bool]:
    """The release whose format the new schematic gets, and True when the tool
    asks "kicad-cli sch upgrade" to convert it.

    numbers is the (major, minor) version of pcbnew, which writes the board and
    the project file, and of kicad-cli (_cli_problem makes sure that they are
    equal). kicad-cli has no command that makes a schematic. The releases KiCad 8
    and 9 have no "sch upgrade": the schematic gets their own format. KiCad 10
    and later convert a schematic of KiCad 9. A development build (minor
    version 99) gets the format of KiCad 9 (9.99 and later) or of KiCad 8
    (8.99), and the tool asks kicad-cli to convert it: kicad-cli tells if it can.
    """
    release = KICAD9 if numbers[0] >= 9 else KICAD8
    upgrade = cli_readable and (numbers[0] >= _SCH_UPGRADE_MAJOR or _is_development(numbers))
    return release, upgrade


def _kicad_template_dirs() -> list[Path]:
    """Return the installed KiCad's template directories."""
    kicad = find_kicad_install()
    if not kicad:
        return []
    install_path, version_marker = kicad
    # Linux/macOS installs are detected at share/kicad (or SharedSupport)
    # already; Windows at the top-level versioned directory.
    if version_marker in ("linux", "macos"):
        template_dir = install_path / "template"
    else:
        template_dir = install_path / "share" / "kicad" / "template"
    return [template_dir] if template_dir.is_dir() else []


def _list_kicad_templates() -> list[str]:
    return sorted(
        d.name
        for base in _kicad_template_dirs()
        for d in base.iterdir()
        if d.is_dir() and any(d.glob("*.kicad_pro"))
    )


def _find_kicad_template(template: str) -> Path | None:
    """Resolve a template name (or a path to a template directory)."""
    candidates = [base / template for base in _kicad_template_dirs()] + [Path(template)]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.kicad_pro")):
            return candidate
    return None


def _copy_template(template_dir: Path, dest_dir: Path, project_name: str) -> list[str]:
    """Copy a template into dest_dir, mirroring KiCad's PROJECT_TEMPLATE::CreateProject.

    meta/ (the template chooser's icon and description) is skipped. File names
    have the template's project base name replaced by project_name; directories
    such as footprint libraries (*.pretty) keep their names because the
    project's library tables refer to them.

    Returns the names of the created top-level entries.
    """
    basename = _template_basename(template_dir)

    created = []
    for src in sorted(template_dir.iterdir()):
        if src.is_dir():
            if src.name == "meta":
                continue
            shutil.copytree(src, dest_dir / src.name, dirs_exist_ok=True)
            created.append(f"{src.name}/")
        else:
            name = src.name
            if src.suffix != ".kicad_wks":
                name = src.stem.replace(basename, project_name) + src.suffix
            shutil.copy(src, dest_dir / name)
            created.append(name)
    return created


# The fields of a title block in the order of TITLE_BLOCK::Format.
_TITLE_BLOCK_ORDER = ("title", "date", "rev", "company", "comment")


def _set_title_block(content: str, title: str, company: str, date: str) -> str:
    """Set the title, the date and the company in the (title_block ...) of a
    schematic, in the order of TITLE_BLOCK::Format (title, date, rev, company,
    comments). An empty value keeps the field of the file. A schematic with no
    title block gets one after (paper ...), as SCH_IO_KICAD_SEXPR::Format writes
    it.

    Raises:
        SExprError: The text is not a valid S-expression.
        ValueError: The schematic has no title block and no paper.
    """
    root = parse(content)
    block = root.first("title_block")
    entries = (
        [] if block is None else [(c.head, c.source[c.start : c.end]) for c in block.children()]
    )
    for head, value in (("title", title), ("date", date), ("company", company)):
        if not value:
            continue
        text = f"({head} {quote(value)})"
        index = next((i for i, (h, _text) in enumerate(entries) if h == head), None)
        if index is None:
            entries.append((head, text))
        else:
            entries[index] = (head, text)
    rank = {head: i for i, head in enumerate(_TITLE_BLOCK_ORDER)}
    entries.sort(key=lambda entry: rank.get(entry[0], len(rank)))
    new_block = "(title_block" + "".join(f"\n\t\t{text}" for _head, text in entries) + "\n\t)"
    if block is not None:
        return content[: block.start] + new_block + content[block.end :]
    paper = root.first("paper")
    if paper is None:
        raise ValueError("The schematic has no (paper ...): KiCad writes it in each schematic.")
    return content[: paper.end] + "\n\t" + new_block + content[paper.end :]


def _new_root_uuid(content: str, root_uuid: str) -> str:
    """Give the root sheet of a schematic a new UUID: its (uuid ...) and each token
    that holds the old UUID (the instance paths "/<root uuid>/..." of the symbols
    and sheets)."""
    root = parse(content)
    node = root.first("uuid")
    old = node.value(0) if node is not None else None
    if not old:
        return content
    edits = [
        (atom.start, atom.end, atom.text.replace(old, root_uuid))
        for item in root.walk()
        for atom in item.atoms()
        if old in atom.text
    ]
    return content[: root.start] + edited(root, edits) + content[root.end :]


def _template_basename(template_dir: Path) -> str:
    """The project name of a template: the name of its one project file, or the
    name of its folder."""
    pro_files = list(template_dir.glob("*.kicad_pro"))
    return pro_files[0].stem if len(pro_files) == 1 else template_dir.name


class _TemplateTexts(NamedTuple):
    """The new root schematic and project file of a template project, or None for a
    file that the template does not have."""

    schematic: str | None
    project_file: str | None


def _template_texts(
    template_dir: Path, project_name: str, title: str, company: str, date: str
) -> _TemplateTexts:
    """Make the root schematic and the project file of a template project with its
    own name, root sheet UUID and title block, before a file is copied: a file of
    the template that the tool cannot read gives no file.

    Raises:
        OSError: A file of the template cannot be read.
        SExprError, ValueError: The parser cannot read the template schematic, it
            has no title block and no paper, or the project file is not JSON.
        KiCadEncodingError: KiCad cannot load the template schematic.
    """
    root_uuid = str(uuid.uuid4())
    basename = _template_basename(template_dir)
    source = template_dir / f"{basename}.kicad_sch"
    schematic = None
    if source.is_file():
        schematic = _new_root_uuid(read_for_write(source), root_uuid)
        schematic = _set_title_block(schematic, title, company, date)
    source = template_dir / f"{basename}.kicad_pro"
    project_file = None
    if source.is_file():
        data = read_kicad_json(source)
        if not isinstance(data, dict):
            raise ValueError(f"{source} is not a JSON object.")
        data.setdefault("meta", {})["filename"] = f"{project_name}.kicad_pro"
        if data.get("sheets"):
            data["sheets"] = [[root_uuid, "Root"]]
        # KiCad writes the project file as UTF-8 text, not with \u escapes.
        project_file = json.dumps(data, indent=2, ensure_ascii=False)
    return _TemplateTexts(schematic, project_file)


def _copy_template_project(
    template_dir: Path, path: Path, project_name: str, texts: _TemplateTexts
) -> list[str]:
    """Copy a template, and write its new root schematic and project file."""
    created = _copy_template(template_dir, path, project_name)
    if texts.schematic is not None:
        write_kicad_text(path / f"{project_name}.kicad_sch", texts.schematic)
    if texts.project_file is not None:
        (path / f"{project_name}.kicad_pro").write_text(texts.project_file, encoding="utf-8")
    return created


# The files of an empty project: pcbnew writes the board, the project file and the
# local settings, the tool writes the schematic.
_PROJECT_FILES = (".kicad_pro", ".kicad_prl", ".kicad_sch", ".kicad_pcb")


def _cli_problem(pcbnew_version: str, cli_version: str | None) -> str | None:
    """The reason why the versions of pcbnew and kicad-cli give no valid project,
    or None. The two must be the same KiCad (major and minor version)."""
    numbers = version_numbers(pcbnew_version)
    if numbers[0] < _MINIMUM_MAJOR:
        return (
            f"pcbnew is KiCad {pcbnew_version}. The server supports KiCad {_MINIMUM_MAJOR} "
            "and later."
        )
    cli_numbers = version_numbers(cli_version) if cli_version else None
    if cli_numbers is not None and cli_numbers != numbers:
        return (
            f"kicad-cli is KiCad {cli_version}, but the Python with pcbnew is KiCad "
            f"{pcbnew_version}. The board and the schematic must be of the same KiCad. "
            "Set KICAD_CLI and KICAD_PYTHON to the programs of one KiCad."
        )
    return None


def _create_empty_project_sync(
    path: Path,
    project_name: str,
    title: str,
    company: str,
    date: str,
    cli_version: str | None,
) -> tuple[list[str], list[str]]:
    """Write an empty project with KiCad's own tooling.

    The tool makes the files in a temporary folder in the project folder, and
    moves them into the project folder only when all of them are complete. Call
    it in one thread: the thread owns the temporary folder, because a cancel of
    the tool does not stop pcbnew and kicad-cli.

    Returns (created file names, warnings).

    Raises:
        FileNotFoundError: No Python with pcbnew is found.
        RuntimeError: pcbnew fails to write the board, the versions of pcbnew and
            kicad-cli give no valid project, or a file of the project exists now.
        subprocess.TimeoutExpired: pcbnew did not stop in time.
    """
    warnings = []
    with tempfile.TemporaryDirectory(dir=path, prefix=".new-project-") as directory:
        work = Path(directory)
        # pcbnew also writes the default .kicad_pro and .kicad_prl next to the board.
        pcbnew_version = new_project_board_sync(
            work / f"{project_name}.kicad_pcb", title, company, date
        )
        problem = _cli_problem(pcbnew_version, cli_version)
        if problem is not None:
            raise RuntimeError(problem)

        numbers = version_numbers(pcbnew_version)
        cli_readable = cli_version is not None and version_numbers(cli_version) is not None
        release, upgrade = _schematic_start(numbers, cli_readable)
        sch_file = work / f"{project_name}.kicad_sch"
        content = _empty_schematic(release)
        write_kicad_text(sch_file, _set_title_block(content, title, company, date))

        # The own format of the release of pcbnew: KiCad 8 or 9.
        native = numbers[0] in (8, 9) and not _is_development(numbers)
        later = (
            f"the schematic has the format of KiCad {release.name}. KiCad {pcbnew_version} "
            "converts it when it saves the schematic."
        )
        if not native and cli_version is None:
            warnings.append(f"kicad-cli gave no version (it is not found, or it failed): {later}")
        elif not native and not cli_readable:
            warnings.append(
                f"kicad-cli gave the version text {cli_version!r}, which the server cannot "
                f"read: {later}"
            )
        if upgrade:
            failure = None
            try:
                result = run_kicad_cli_sync(["sch", "upgrade", str(sch_file)], timeout=120)
                if result.returncode != 0:
                    failure = decode_output(result.stderr).strip()
            except (FileNotFoundError, RuntimeError) as e:
                failure = str(e)
            except subprocess.TimeoutExpired as e:
                failure = f"it did not stop in {e.timeout:g} s"
            if failure is not None:
                warnings.append(f"`kicad-cli sch upgrade` failed ({failure}): {later}")
            else:
                try:
                    schematic_text_version(read_as_kicad(sch_file))
                except (KicadFileError, KiCadEncodingError) as e:
                    warnings.append(f"The new schematic: {e} The other tools cannot read it.")

        names = [
            f"{project_name}{ext}"
            for ext in _PROJECT_FILES
            if (work / f"{project_name}{ext}").exists()
        ]
        _move_all(work, path, names)
    return names, warnings


def _missing_folders(path: Path) -> list[Path]:
    """The folders of path that do not exist, the deepest first."""
    missing = []
    while not path.exists():
        missing.append(path)
        if path.parent == path:
            break
        path = path.parent
    return missing


def _remove_empty_folders(folders: list[Path]) -> None:
    """Remove the folders that the tool made, the deepest first, while they are
    empty. A folder with a file of an other program stays."""
    for folder in folders:
        if not folder.is_dir() or any(folder.iterdir()):
            return
        folder.rmdir()


def _move_all(source: Path, target: Path, names: list[str]) -> None:
    """Move the files from source into target, all or none.

    Raises:
        RuntimeError: A file exists in target (an other program made it after the
            first check), or a move failed (the moved files go back).
    """
    existing = [name for name in names if (target / name).exists()]
    if existing:
        raise RuntimeError(
            f"{', '.join(existing)} now exist in {target} (an other program made them). "
            "The tool wrote no file."
        )
    moved = []
    try:
        for name in names:
            # A file that an other program makes between the check above and this
            # move is replaced: the two steps are not one operation.
            os.replace(source / name, target / name)
            moved.append(name)
    except OSError as e:
        for name in reversed(moved):
            os.replace(target / name, source / name)
        raise RuntimeError(
            f"The tool cannot move the files into {target}: {e}. It wrote no file."
        ) from e


@mcp.tool()
async def create_kicad_project(
    project_path: str,
    project_name: str,
    title: str = "",
    company: str = "",
    template: str = EMPTY_TEMPLATE,
) -> str:
    """Create a new KiCad project.

    By default the project is empty: a blank board, schematic and project
    file in the file format of the installed KiCad (8, 9, 10 or later).

    Args:
        project_path: Directory path for the project
        project_name: Name of the project (without extension)
        title: Optional project title (defaults to project_name)
        company: Optional company name
        template: "empty" (default) for a blank project, or a KiCad template
            to start from, by name (e.g. "Arduino_Uno", "RaspberryPi-HAT") or
            as a path to a template directory. Templates come with their own
            board outline, footprints and schematic.

    Returns:
        Confirmation message with created files
    """
    # KiCad writes these values into its files as UTF-8. Check them before any
    # file changes, so that a value that is not valid text (a lone surrogate)
    # does not leave a project that is only partly made.
    for field, value in (("project_name", project_name), ("title", title), ("company", company)):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as e:
            return f"❌ The {field} is not valid text: {e}"
    try:
        path = Path(project_path)
        date_str = datetime.now().strftime("%Y-%m-%d")
        title_text = title or project_name

        existing = [
            f"{project_name}{ext}"
            for ext in _PROJECT_FILES
            if (path / f"{project_name}{ext}").exists()
        ]
        if existing:
            return (
                f"❌ A project named '{project_name}' already exists in {path} "
                f"({', '.join(existing)}). Choose another name or directory."
            )

        version = await get_kicad_version()
        numbers = version_numbers(version) if version else None
        if numbers is not None and numbers[0] < _MINIMUM_MAJOR:
            return (
                f"❌ KiCad {version} is installed. The server supports KiCad "
                f"{_MINIMUM_MAJOR} and later."
            )

        if template == EMPTY_TEMPLATE:
            if find_kicad_python() is None:
                return """❌ KiCad's Python (pcbnew) not found.

Creating a project needs KiCad installed:
  macOS:   brew install --cask kicad
  Linux:   sudo apt install kicad
  Windows: https://www.kicad.org/download/

If KiCad is installed in a non-standard location, set KICAD_PYTHON to its
python executable.
"""
            new_folders = _missing_folders(path)
            path.mkdir(parents=True, exist_ok=True)
            try:
                created, warnings = await asyncio.to_thread(
                    _create_empty_project_sync,
                    path,
                    project_name,
                    title_text,
                    company,
                    date_str,
                    version,
                )
            except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as e:
                # The tool wrote no file: remove the folders that it made.
                _remove_empty_folders(new_folders)
                if isinstance(e, subprocess.TimeoutExpired):
                    return f"❌ pcbnew did not stop in {e.timeout:g} s. The tool wrote no file."
                return f"❌ {e}"
            template_desc = "empty"
        else:
            template_dir = _find_kicad_template(template)
            if template_dir is None:
                available = _list_kicad_templates()
                listing = ", ".join(available) if available else "(no KiCad templates found)"
                return (
                    f"❌ KiCad template '{template}' not found.\n\n"
                    f'Available templates: {listing}\n\nUse template="empty" for a blank project.'
                )
            try:
                texts = await asyncio.to_thread(
                    _template_texts, template_dir, project_name, title_text, company, date_str
                )
            except (OSError, ValueError, KiCadEncodingError) as e:
                return f"❌ The template {template_dir}: {e} The tool wrote no file."
            path.mkdir(parents=True, exist_ok=True)
            created = await asyncio.to_thread(
                _copy_template_project, template_dir, path, project_name, texts
            )
            warnings = []
            template_desc = f"{template_dir.name} ({template_dir})"

        kicad_label = f"KiCad {version}" if version else "KiCad"
        pro_file = path / f"{project_name}.kicad_pro"
        files_listing = "\n".join(f"- {n}" for n in created)
        warnings_section = (
            "\n## ⚠️ Warnings:\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
            if warnings
            else ""
        )

        return f"""# ✅ {kicad_label} Project Created Successfully!

**Project Path:** {path}
**Project Name:** {project_name}
**Title:** {title_text}
**Company:** {company or '(none)'}
**Template:** {template_desc}

## 📄 Files Created:

{files_listing}
{warnings_section}
## 📖 How to Open in {kicad_label}:

1. Open KiCad
2. File → Open Project...
3. Navigate to: {pro_file}
4. Click Open

## 🔧 Next Steps:

1. Open schematic editor to add components
2. Use `add_component_from_library` to add parts
3. Use `add_wire` and `add_global_label` for connections
4. Update PCB from schematic when ready
"""

    except Exception as e:
        import traceback
        return f"❌ Error creating project: {e}\n\n{traceback.format_exc()}"
