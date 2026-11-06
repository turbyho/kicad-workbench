"""KiCad project creation tools for KiCad Workbench.

create_kicad_project makes a project as the project manager of the installed
KiCad makes it (utils/new_project.py): "New Project" with no template, or "New
Project from Template". Then it writes the title, the company and the date into
the title block of the root schematic that it wrote. KiCad does not set a
title block: this is the only change of the tool.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from ..parsers.sexpr import SExprError, parse, quote
from ..server import mcp
from ..utils.kicad_cli import get_kicad_version
from ..utils.kicad_lock import (
    check_board_not_open,
    check_project_not_open,
    check_schematic_not_open,
)
from ..utils.kicad_text import KiCadEncodingError, read_for_write, write_kicad_text
from ..utils.library_tables import LibraryTableError, kicad_environment, kicad_stock_dir
from ..utils.new_project import (
    EMPTY_PROJECT_FILE,
    NewProjectError,
    Plan,
    ensure_default_template,
    find_template,
    plan_project,
    rules,
    split_name,
    system_template_dir,
    user_template_dir,
    write_project,
)
from ..utils.tool_errors import error_answer

EMPTY_TEMPLATE = "empty"


class _RefusedError(Exception):
    """The tool writes no file: KiCad would ask the user, or KiCad has a file open.
    The text is the answer of the tool."""


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


def _template_dir(rule, env, template: str, default_dir: Path | None) -> Path:
    """The folder of a template: a path (it has a path separator), else a name on
    the pages of the template selector of KiCad.

    Raises:
        NewProjectError: No template has the name, or more than one.
    """
    if "/" in template or "\\" in template:
        folder = Path(template).expanduser()
        if not folder.is_dir():
            raise NewProjectError(f"The template folder {folder} does not exist.")
        # The selector of KiCad 10.0.1 and later shows only a folder with "meta". KiCad
        # 8, 9 and 10.0.0 show each folder: the tool takes a folder with "meta" or a
        # project file, not a folder of templates (it would copy all of them).
        has_meta = (folder / "meta").is_dir()
        projects = [p for p in folder.iterdir() if p.suffix in (".kicad_pro", ".pro")]
        if not has_meta and (rule.version >= (10, 0, 1) or not projects):
            raise NewProjectError(
                f"{folder} is no template of KiCad {'.'.join(map(str, rule.version))}: it "
                'has no folder "meta"'
                + ("." if rule.version >= (10, 0, 1) else " and no project file.")
            )
        return folder
    return find_template(rule, env, template, default_dir)


def _dialog_name(name: str) -> str:
    """The project name of a file name, as KICAD_MANAGER_CONTROL gets it from the
    file dialog: an extension other than "kicad_pro" stays in the name, then
    SetExt("kicad_pro") replaces the extension. "board.kicad_pro" and "board." give
    "board", "board.v2" gives "board.v2", ".kicad_pro" (a name with a first dot has
    no extension) gives ".kicad_pro"."""
    stem, ext = split_name(name)
    if ext is None or ext not in ("", "kicad_pro"):
        return name
    return stem


def _conflict_error(plan: Plan, template: bool) -> str:
    names = ", ".join(str(p.relative_to(plan.target_dir)) for p in plan.conflicts)
    if template:
        return (
            f"❌ Files of the template exist in {plan.target_dir}: {names}. KiCad asks before "
            "it replaces them. Ask the user, then use overwrite=True to replace them."
        )
    return (
        f"❌ The folder {plan.target_dir} is not empty: {names}. KiCad asks before it makes a "
        "project in a folder that is not empty. Ask the user, then use overwrite=True: the "
        "files of the folder stay."
    )


def _make_project(
    version: str | None,
    path: Path,
    project_name: str,
    template: str,
    overwrite: bool,
    title: str,
    company: str,
    date: str,
) -> tuple[list[str], list[str], str]:
    """Make the project (in a thread: file reads and writes).

    Returns:
        (the names that the tool wrote, the notes and warnings, the template).

    Raises:
        NewProjectError, LibraryTableError: KiCad makes no project here.
        _RefusedError: A file exists, or KiCad has a file open.
        OSError: A file cannot be read or written.
    """
    rule = rules(version)
    env = kicad_environment(version)
    notes = []
    default_dir = None
    if rule.version >= (10, 0, 0):
        # KiCad 10 has only "New Project": it makes the template "default" first.
        default_dir, made, reason = ensure_default_template(rule, env)
        if made and default_dir is not None:
            notes.append(
                f'KiCad {version} makes the template "default" in {default_dir} at each new '
                "project, when it is not there: the tool made it in the same way."
            )
        elif made:
            notes.append(f'⚠️ The tool made a part of the template "default": {reason}.')
        pages = user_template_dir(rule, env) or system_template_dir(rule, env)
        if default_dir is None and (rule.version < (10, 0, 5) or not pages):
            # NewProject: newProjectDirectory and CreateNewProject, with no template.
            if template != EMPTY_TEMPLATE:
                raise NewProjectError(
                    f"KiCad {version} shows no template selector, because it has no "
                    f'template "default" ({reason}). It makes only an empty project.'
                )
            notes.append(
                f'KiCad {version} has no template "default" ({reason}): it makes '
                "the project with no template, and so does the tool."
            )
        elif default_dir is None and template == EMPTY_TEMPLATE:
            raise NewProjectError(
                f'KiCad {version} has no template "default" ({reason}). Its "New Project" then '
                "offers only the templates of its pages: give one of them."
            )
    if template == EMPTY_TEMPLATE:
        template_dir = default_dir
        description = "empty" if template_dir is None else f"empty (template {template_dir})"
    else:
        template_dir = _template_dir(rule, env, template, default_dir)
        description = f"{template_dir.name} ({template_dir})"
    plan = plan_project(rule, env, kicad_stock_dir(version), path, project_name, template_dir)
    if plan.conflicts:
        if not overwrite:
            raise _RefusedError(_conflict_error(plan, template_dir is not None))
        if template_dir is not None:
            # The files of the template replace these files: KiCad must not have them
            # open, nor the project (KiCad saves its settings again).
            checks = [check_project_not_open(plan.path(".kicad_pro"))]
            for conflict in plan.conflicts:
                if conflict.suffix == ".kicad_sch":
                    checks.append(check_schematic_not_open(conflict))
                elif conflict.suffix == ".kicad_pcb":
                    checks.append(check_board_not_open(conflict))
            if error := next((c for c in checks if c), None):
                raise _RefusedError(error)

    written, warnings = write_project(rule, plan)
    notes += [f"⚠️ {w}" for w in warnings]
    if plan.write_project_file and plan.project_file_copied:
        notes.append(
            f"The project file is a copy of {plan.project_file_source}, as KiCad makes it."
        )
    elif plan.write_project_file and plan.project_file_source is None:
        notes.append(
            f"KiCad finds no kicad.kicad_pro: the project file is {EMPTY_PROJECT_FILE!r}, as "
            "KiCad writes it. (The server looks in the template folders of KiCad, $KICAD and "
            "the data folder of the installation. KiCad also looks in some program folders.)"
        )

    schematic = plan.path(".kicad_sch")
    if schematic in written:
        try:
            content = _set_title_block(read_for_write(schematic), title, company, date)
            write_kicad_text(schematic, content)
        except (SExprError, ValueError, KiCadEncodingError, OSError) as e:
            notes.append(f"⚠️ The tool did not write the title block of {schematic.name}: {e}")
    names = [str(p.relative_to(path)) + ("/" if p.is_dir() else "") for p in written]
    return names, notes, description


@mcp.tool()
async def create_kicad_project(
    project_path: str,
    project_name: str,
    title: str = "",
    company: str = "",
    template: str = EMPTY_TEMPLATE,
    overwrite: bool = False,
) -> str:
    """Create a new KiCad project, as the project manager of the installed KiCad
    (8, 9 or 10) makes it.

    With no template ("empty"), the project gets the files of "New Project" of
    KiCad: KiCad 8 and 9 copy the project file kicad.kicad_pro of KiCad and
    write an empty schematic and board. KiCad 10 makes the project from its
    template "default" (it makes that template when it is not there). With a
    template, the project gets the files of "New Project from Template": the
    files of the template with the project name in the place of the template
    name, then the files that are missing. KiCad does not change the files of a
    template. The tool then writes the title, the company and the date into the
    title block of the root schematic that it wrote.

    When the project folder is not empty (no template), or it has files of the
    template, KiCad asks the user. The tool then gives an error: ask the user,
    then call it again with overwrite=True.

    Args:
        project_path: Directory path for the project
        project_name: Name of the project. As in the file dialog of KiCad, the
            extension ".kicad_pro" (or a last ".") goes: "board.kicad_pro" gives "board".
        title: Optional project title (defaults to project_name)
        company: Optional company name
        template: "empty" (default) for a blank project, or a KiCad template:
            a name on the template pages of KiCad (KICAD_USER_TEMPLATE_DIR and
            KICAD<version>_TEMPLATE_DIR, e.g. "Arduino_Uno") or the path of a
            template folder.
        overwrite: True after the user confirmed: with a template, the files of
            the template replace the files with the same names; with no
            template, the tool makes the project in a folder that is not empty
            (the files of the folder stay, as in KiCad).

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
    project_name = _dialog_name(project_name)
    # The file dialog of KiCad gives a file name: not empty, in the project folder.
    if project_name.strip() in ("", ".", "..") or any(c in project_name for c in "/\\\0"):
        return (
            f"❌ The project_name {project_name!r} is not a file name: give a name with no "
            "path separator, not empty, not '.' or '..'."
        )
    try:
        path = Path(project_path)
        title_text = title or project_name
        date_str = datetime.now().strftime("%Y-%m-%d")
        version = await get_kicad_version()
        try:
            created, notes, description = await asyncio.to_thread(
                _make_project,
                version,
                path,
                project_name,
                template,
                overwrite,
                title_text,
                company,
                date_str,
            )
        except (NewProjectError, LibraryTableError) as e:
            return f"❌ {e}"
        except _RefusedError as e:
            return str(e)

        pro_file = path / f"{project_name}.kicad_pro"
        files_listing = "\n".join(f"- {n}" for n in created)
        notes_section = (
            "\n## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else ""
        )

        return f"""# ✅ KiCad {version} Project Created Successfully!

**Project Path:** {path}
**Project Name:** {project_name}
**Title:** {title_text}
**Company:** {company or '(none)'}
**Template:** {description}

## 📄 Files Created:

{files_listing}
{notes_section}
## 📖 How to Open in KiCad {version}:

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
        return error_answer(e, "Error creating project")
