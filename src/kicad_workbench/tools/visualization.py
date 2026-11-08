"""Board visualization export tools.

These wrap kicad-cli's rendering and 3D/2D exporters so that MCP clients
(and the multimodal LLMs behind them) can *see* a design:

- render_pcb: 3D-rendered PNG/JPEG of the board (top/bottom/isometric views)
- export_pcb_3d: STEP model for mechanical CAD handoff
- export_schematic_svg: the schematic pages as SVGs (all pages, or the pages of a
  sub-sheet)

render_pcb returns a FastMCP Image so capable clients display the picture
inline instead of just a path.
"""

import asyncio
import collections
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastmcp.utilities.types import Image

from ..errors import InputError
from ..parsers.kicad_versions import version_numbers
from ..server import mcp
from ..utils.file_save import SaveFilesError, save_files
from ..utils.file_versions import with_board_versions, with_schematic_versions
from ..utils.kicad_cli import (
    KicadCliError,
    cli_root,
    cli_sheet_paths,
    decode_output,
    export_with_kicad_cli,
    get_kicad_version,
    installed_kicad_problem,
    run_kicad_cli_sync,
)
from ..utils.sch_hierarchy import (
    AmbiguousRootError,
    schematic_project_file,
)
from ..utils.tool_errors import error_answer

_RENDER_SIDES = {"top", "bottom", "left", "right", "front", "back"}
_RENDER_QUALITIES = {"basic", "high", "user", "job_settings"}
# The image files of "pcb render" (command_pcb_render.cpp of KiCad 9 and 10: the output
# file name, in lower case, ends with .png, .jpg or .jpeg), and their image format.
_RENDER_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


def _output_problem(out: Path, suffixes: tuple[str, ...]) -> str | None:
    """Return why the tool must not write its output file, or None. The extension
    must be one of suffixes: thus the output is never a KiCad file (also not the
    board)."""
    if out.suffix.lower() not in suffixes:
        return f"The output file {out} must have the extension {' or '.join(suffixes)}."
    return None


# The message of EXPORTER_STEP::buildFootprint3DShapes for a 3D model that it did not
# add: "Could not add 3D model to <reference>." (KiCad 8) or "... for <reference>."
# (KiCad 9 and 10), then the reason on the next line ("File not found: <path>" or
# "OpenCASCADE error: <text>"). The CLI reporter writes warnings and other messages
# to stdout with no mark of the severity (CLI_REPORTER::Report), thus only the text
# shows the warning.
_NO_MODEL = re.compile(r"^Could not add 3D model (?:to|for) (.+)\.$")
_NO_MODEL_REASONS = ("File not found: ", "OpenCASCADE error: ")


def _models_not_added(messages: list[str]) -> list[str]:
    """Return the 3D models that the STEP export did not add, from the messages of
    kicad-cli (CliExport.messages): "<reference>: <reason>"."""
    found = []
    for i, line in enumerate(messages):
        if match := _NO_MODEL.match(line):
            reason = messages[i + 1] if i + 1 < len(messages) else ""
            if not reason.startswith(_NO_MODEL_REASONS):
                reason = "kicad-cli gave no reason"
            found.append(f"{match.group(1)}: {reason}")
    return found


def _save_output(out: Path, data: bytes) -> list[str] | str:
    """Save the output file (file_save.save_files): the warnings, or the error."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        return save_files([(out, data)])
    except (OSError, SaveFilesError) as e:
        return f"❌ The tool cannot write {out}: {e}"


@mcp.tool()
@with_board_versions("pcb_path")
async def render_pcb(
    pcb_path: str,
    side: str = "top",
    output_path: str = "",
    width: int = 1600,
    height: int = 900,
    quality: str = "basic",
    rotate: str = "",
) -> Image | str | list[str | Image]:
    """Render a 3D view of a board as an image with kicad-cli (KiCad 9 or later).

    The image has the board and the 3D models of the footprints: a footprint with no
    3D model has no body in the image. Use the image for a visual review of the layout.

    Args:
        pcb_path: The .kicad_pcb file.
        side: The side of the camera: "top" (default), "bottom", "left", "right",
            "front" or "back".
        output_path: Optional image file: .png, or .jpg or .jpeg for a JPEG image.
            Default: <board>_render_<side>.png next to the board.
        width: The width of the image in pixels (default 1600).
        height: The height of the image in pixels (default 900).
        quality: "basic" (default, fast), "high" (ray tracing, slower), "user" or
            "job_settings" (the settings of KiCad).
        rotate: Optional rotation of the board "X,Y,Z" in degrees, for example
            "-30,0,25".

    Returns:
        The image, or an error. A warning (an older board, a step of the save)
        comes before the image.
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ PCB file not found: {pcb_path}"
        if side not in _RENDER_SIDES:
            return f"❌ Invalid side '{side}'. Options: {', '.join(sorted(_RENDER_SIDES))}"
        if quality not in _RENDER_QUALITIES:
            return (
                f"❌ Invalid quality '{quality}'. Options: {', '.join(sorted(_RENDER_QUALITIES))}"
            )

        # kicad-cli has "pcb render" from KiCad 9: the "pcb" of kicad-cli 8.0.9 has only
        # drc and export (spike/kicad-versions/pcb_render_check.py).
        kicad = await get_kicad_version()
        if problem := installed_kicad_problem(kicad):
            return f"❌ {problem}"
        if version_numbers(kicad)[0] < 9:
            return (
                f'❌ render_pcb needs KiCad 9 or later: kicad-cli {kicad} has no "pcb render". '
                "Tell the user: install KiCad 9 or later for a 3D view. With KiCad 8, "
                "export_pcb_3d gives a STEP model."
            )

        out = Path(output_path) if output_path else pcb.parent / f"{pcb.stem}_render_{side}.png"
        suffix = out.suffix.lower()
        if problem := _output_problem(out, tuple(_RENDER_FORMATS)):
            return f"❌ {problem}"
        command = ["pcb", "render", "--side", side, "--width", str(width)]
        command += ["--height", str(height), "--quality", quality]
        command += ["--rotate", rotate] if rotate else []
        try:
            made = await asyncio.to_thread(
                export_with_kicad_cli, command, pcb, f"output{suffix}", 180
            )
        except KicadCliError as e:
            return f"❌ PCB render failed: {e}"
        saved = await asyncio.to_thread(_save_output, out, made.data)
        if isinstance(saved, str):
            return saved
        image = Image(data=made.data, format=_RENDER_FORMATS[suffix])
        return [f"⚠️ {warning}" for warning in saved] + [image] if saved else image
    except FileNotFoundError as e:  # kicad-cli is not there
        return f"❌ {e}"
    except Exception as e:
        return error_answer(e, "Error rendering PCB")


@mcp.tool()
@with_board_versions("pcb_path")
async def export_pcb_3d(
    pcb_path: str,
    output_path: str = "",
    board_only: bool = False,
) -> str:
    """Export a board as a STEP 3D model with kicad-cli, for mechanical CAD.

    The model has the board and the 3D models of its footprints. kicad-cli does not
    add a 3D model that it cannot find or read: the tool then gives a warning with the
    footprints and the reason. Tell the user about each such footprint.

    Args:
        pcb_path: The .kicad_pcb file.
        output_path: Optional .step or .stp file. Default: <board>.step next to the
            board.
        board_only: True gives only the board, with no models of the footprints.

    Returns:
        The path and the size of the STEP file, a warning for each 3D model that is
        not in the file, and the messages of kicad-cli.
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ PCB file not found: {pcb_path}"

        out = Path(output_path) if output_path else pcb.parent / f"{pcb.stem}.step"
        if problem := _output_problem(out, (".step", ".stp")):
            return f"❌ {problem}"
        command = ["pcb", "export", "step", "--force"]
        command += ["--board-only"] if board_only else []
        try:
            made = await asyncio.to_thread(
                export_with_kicad_cli, command, pcb, f"output{out.suffix.lower()}", 300
            )
        except KicadCliError as e:
            return f"❌ STEP export failed: {e}"
        saved = await asyncio.to_thread(_save_output, out, made.data)
        if isinstance(saved, str):
            return saved
        notes = "".join(f"\n\n⚠️ {warning}" for warning in saved)
        missing = _models_not_added(made.messages)
        if missing:
            notes += (
                f"\n\n⚠️ The STEP file has no 3D model for {len(missing)} item(s): kicad-cli "
                "did not add them. Tell the user: correct the 3D model paths of these "
                "footprints (check_library_links gives the 3D models of the board and their "
                "state).\n" + "\n".join(f"- {item}" for item in missing)
            )
        size_mb = len(made.data) / (1024 * 1024)
        messages = "\n".join(made.messages) or "(no messages)"
        if missing:
            head = "⚠️ STEP 3D model exported, with no 3D model of some items."
        elif saved:
            head = "⚠️ STEP 3D model exported, with a warning of the save."
        else:
            head = "✅ STEP 3D model exported."
        return f"""{head}

**PCB:** {pcb_path}
**Output:** {out}
**Size:** {size_mb:.1f} MB

Import it into a mechanical CAD program (for example FreeCAD) for the enclosure.{notes}

## Messages of kicad-cli

```
{messages}
```"""
    except FileNotFoundError as e:  # kicad-cli is not there
        return f"❌ {e}"
    except Exception as e:
        return error_answer(e, "Error exporting STEP")


# The time of kicad-cli for the SVG files of a large project.
_SVG_TIMEOUT = 120.0


def svg_name(top_file: Path, names: tuple[str, ...]) -> str:
    """The name of the SVG file of a sheet path, as kicad-cli writes it
    (SCHEMATIC::GetUniqueFilenameForCurrentSheet): the name of the file of the
    top-level sheet with no extension, then "-" and the name of each sheet below
    it (the text of the Sheetname field, SCH_SHEET::GetName), with "_" for "/" and
    "\\" (SCH_PLOTTER::createSVGFiles)."""
    name = "-".join((top_file.stem, *names))
    return name.replace("/", "_").replace("\\", "_") + ".svg"


def _file_svg_names(
    root: Path, kicad: str | None, sch: Path
) -> tuple[list[str], list[str], list[str]]:
    """Return the names of the SVG files of the sheet paths of a file in the
    hierarchy that kicad-cli loads (cli_sheet_paths), the names of these that more
    than one sheet path gives (kicad-cli writes one page over the other), and the
    problems."""
    project = schematic_project_file(sch)
    paths, problems = cli_sheet_paths(root, kicad, project)
    # With more than one top-level sheet that kicad-cli loads (10.0.7 and later), the
    # first name of a path is the name of its top-level sheet: the file of the screen of
    # that sheet gives the first part of the file name. Two top-level sheets can have
    # one name: the index of the top-level sheet finds its file.
    # cli_root gives an error for more than one top-level sheet with an older kicad-cli.
    files = {p.top: p.file for p in paths if p.file is not None and len(p.names) <= 1}
    several = len({p.top for p in paths}) > 1
    target = sch.resolve()
    names, all_names = [], []
    for path in paths:
        if several and path.names:
            name = svg_name(files.get(path.top, root), path.names[1:])
        else:
            name = svg_name(root, path.names)
        all_names.append(name)
        if path.file is not None and path.file.resolve() == target:
            names.append(name)
    counts = collections.Counter(all_names)
    shared = sorted({name for name in names if counts[name] > 1})
    return names, shared, problems


def _export_svgs(source: Path, keep: set[str] | None) -> list[tuple[str, bytes]] | str:
    """Run kicad-cli sch export svg (all pages) in a temporary folder. Return the
    name and the content of each SVG file (with keep: only of these names), or the
    error. The thread owns the folder: a cancelled tool call does not stop
    kicad-cli, thus only the thread removes it."""
    directory = Path(tempfile.mkdtemp(prefix="kicad-workbench-svg-"))
    try:
        args = ["sch", "export", "svg", "--output", str(directory), str(source)]
        try:
            result = run_kicad_cli_sync(args, timeout=_SVG_TIMEOUT, english=True)
        except subprocess.TimeoutExpired:
            return f"❌ kicad-cli did not stop in {_SVG_TIMEOUT:g} s."
        svgs = sorted(directory.glob("*.svg"))
        if result.returncode != 0 or not svgs:
            # A line break between the two streams: the last line of stdout can have none.
            output = (decode_output(result.stdout) + "\n" + decode_output(result.stderr)).strip()
            return (
                f"❌ kicad-cli wrote no SVG file (exit code {result.returncode}):\n\n"
                f"{output or '(no output)'}"
            )
        return [(svg.name, svg.read_bytes()) for svg in svgs if keep is None or svg.name in keep]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@mcp.tool()
@with_schematic_versions("schematic_path")
async def export_schematic_svg(
    schematic_path: str,
    output_dir: str = "",
) -> str:
    """Export the pages of a schematic as SVG files with kicad-cli.

    kicad-cli renders the pages of the project with the hierarchy of its root
    schematic (the schematic of the project name), thus each page has the
    references of its sheet path. For the root schematic, the files are all pages
    of the project. For an other file (a sub-sheet), the files are the pages of
    its sheet paths (a sheet that the hierarchy uses two times gives two pages).
    An SVG file is a vector image: a zoom shows each detail with no loss. The tool
    needs kicad-cli.

    Args:
        schematic_path: A .kicad_sch file of the project.
        output_dir: Optional folder of the files. Default: <schematic>_svg next
            to the schematic.

    Returns:
        The SVG files of this export.
    """
    try:
        sch = Path(schematic_path)
        if not sch.is_file():
            return f"❌ Schematic file not found: {schematic_path}"

        out_dir = Path(output_dir) if output_dir else sch.parent / f"{sch.stem}_svg"
        kicad = await get_kicad_version()
        try:
            root = await asyncio.to_thread(cli_root, sch, "SVG files", kicad)
        except AmbiguousRootError as e:
            return f"❌ {e}. Export the root schematic that you want."
        except InputError as e:
            return f"❌ {e}"

        notes: list[str] = []
        names: list[str] = []
        incomplete = False
        if root is not None:
            # --pages of kicad-cli 8.0.9 to 10.0.7 gives only the page of the root for
            # SVG (createSVGFiles builds the sheet list only for m_plotAll, and --pages
            # sets it to false): the tool exports all pages and keeps the files of the
            # sheet paths of the file.
            names, shared, problems = await asyncio.to_thread(_file_svg_names, root, kicad, sch)
            notes += [f"⚠️ {problem}" for problem in problems]
            if not names:
                return f"❌ {sch} has no sheet in the hierarchy of {root}." + "".join(
                    f"\n\n{note}" for note in notes
                )
            if shared:
                incomplete = True
                notes.append(
                    f"⚠️ More than one sheet path gives the file name {', '.join(shared)}: "
                    "kicad-cli wrote one page over the other, thus the file can be the page "
                    "of an other sheet."
                )

        exported = await asyncio.to_thread(_export_svgs, root or sch, set(names) if names else None)
        if isinstance(exported, str):
            return exported
        if root is not None:
            written = {name for name, _data in exported}
            missing = [name for name in names if name not in written]
            if missing:
                incomplete = True
                notes.append(
                    f"⚠️ kicad-cli wrote no file {', '.join(missing)}: the tool does not find "
                    "these pages of the file."
                )
            if not exported:
                return f"❌ kicad-cli wrote no page of {sch.name}." + "".join(
                    f"\n\n{note}" for note in notes
                )
            notes.append(
                f"**Note:** `{sch.name}` is not the root schematic of its project. The files "
                f"are the pages of its sheet paths in the hierarchy of `{root.name}`, with the "
                "references of each path."
            )
        files = [(out_dir / name, data) for name, data in exported]

        def save() -> list[str]:
            out_dir.mkdir(parents=True, exist_ok=True)
            return save_files(files)

        try:
            saved = await asyncio.to_thread(save)
        except SaveFilesError as e:
            return f"❌ {e}"
        notes += [f"⚠️ {warning}" for warning in saved]

        listing = "\n".join(f"- {path.name}" for path, _data in files)
        sign = (
            "⚠️ Schematic SVG export is not complete."
            if incomplete
            else ("✅ Schematic SVG export complete.")
        )
        text = f"""{sign}

**Schematic:** {schematic_path}
**Output directory:** {out_dir}

**Files of this export ({len(files)}):**
{listing}"""
        return text + "".join(f"\n\n{note}" for note in notes)
    except FileNotFoundError as e:  # kicad-cli is not there
        return f"❌ {e}"
    except Exception as e:
        return error_answer(e, "Error exporting schematic SVG")
