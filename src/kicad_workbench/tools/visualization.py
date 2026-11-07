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
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastmcp.utilities.types import Image

from ..errors import InputError
from ..server import mcp
from ..utils.file_save import SaveFilesError, save_files
from ..utils.kicad_cli import (
    cli_root,
    cli_sheet_paths,
    decode_output,
    get_kicad_version,
    run_kicad_cli,
    run_kicad_cli_sync,
)
from ..utils.sch_hierarchy import (
    AmbiguousRootError,
    schematic_project_file,
)
from ..utils.tool_errors import error_answer

_RENDER_SIDES = {"top", "bottom", "left", "right", "front", "back"}
_RENDER_QUALITIES = {"basic", "high", "user", "job_settings"}


@mcp.tool()
async def render_pcb(
    pcb_path: str,
    side: str = "top",
    output_path: str = "",
    width: int = 1600,
    height: int = 900,
    quality: str = "basic",
    rotate: str = "",
) -> Image | str:
    """Render a 3D view of the PCB to a PNG image.

    Produces a photorealistic render of the assembled board via kicad-cli
    (footprints must have 3D models assigned; otherwise plain board geometry
    is rendered). Returns the image itself, so multimodal clients show it
    directly — useful for AI-assisted layout review.

    Args:
        pcb_path: Path to the .kicad_pcb file.
        side: Camera side: top, bottom, left, right, front, back.
        output_path: Output PNG (default: <pcb>_render_<side>.png next to
            the PCB).
        width: Image width in pixels.
        height: Image height in pixels.
        quality: Render quality: basic (fast) or high (slower, raytraced).
        rotate: Optional board rotation 'X,Y,Z' in degrees for isometric
            views, e.g. '-30,0,25'.

    Returns:
        The rendered image, or an error message string.
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

        out = Path(output_path) if output_path else pcb.parent / f"{pcb.stem}_render_{side}.png"
        out.parent.mkdir(parents=True, exist_ok=True)

        args = [
            "pcb",
            "render",
            "--output",
            str(out),
            "--side",
            side,
            "--width",
            str(width),
            "--height",
            str(height),
            "--quality",
            quality,
        ]
        if rotate:
            args += ["--rotate", rotate]
        args.append(str(pcb))

        result = await run_kicad_cli(args, timeout=180)

        if result.returncode != 0 or not out.exists():
            stderr = decode_output(result.stderr).strip()
            return (
                f"❌ PCB render failed (kicad-cli rc={result.returncode}):\n\n"
                f"{stderr or '(no stderr, and no output file produced)'}"
            )

        return Image(path=str(out))
    except FileNotFoundError as e:  # kicad-cli is not there
        return f"❌ {e}"
    except Exception as e:
        return error_answer(e, "Error rendering PCB")


@mcp.tool()
async def export_pcb_3d(
    pcb_path: str,
    output_path: str = "",
    board_only: bool = False,
) -> str:
    """Export the PCB as a STEP 3D model for mechanical CAD handoff.

    Includes board body, footprints and their assigned 3D models.

    Args:
        pcb_path: Path to the .kicad_pcb file.
        output_path: Output .step file (default: <pcb>.step next to the PCB).
        board_only: Export only the board body, without component models.

    Returns:
        Confirmation with the output path and file size.
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ PCB file not found: {pcb_path}"

        out = Path(output_path) if output_path else pcb.parent / f"{pcb.stem}.step"
        out.parent.mkdir(parents=True, exist_ok=True)

        args = ["pcb", "export", "step", "--output", str(out), "--force", str(pcb)]
        if board_only:
            args.insert(-1, "--board-only")

        result = await run_kicad_cli(args, timeout=300)

        if result.returncode != 0 or not out.exists():
            stderr = decode_output(result.stderr).strip()
            return (
                f"❌ STEP export failed (kicad-cli rc={result.returncode}):\n\n"
                f"{stderr or '(no stderr, and no output file produced)'}"
            )

        size_mb = out.stat().st_size / (1024 * 1024)
        return f"""✅ STEP 3D model exported.

**PCB:** {pcb_path}
**Output:** {out}
**Size:** {size_mb:.1f} MB

Import into Fusion 360 / SolidWorks / FreeCAD for enclosure design."""
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
            output = decode_output(result.stdout + result.stderr).strip()
            return (
                f"❌ kicad-cli wrote no SVG file (exit code {result.returncode}):\n\n"
                f"{output or '(no output)'}"
            )
        return [(svg.name, svg.read_bytes()) for svg in svgs if keep is None or svg.name in keep]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@mcp.tool()
async def export_schematic_svg(
    schematic_path: str,
    output_dir: str = "",
) -> str:
    """Export schematic pages to SVG images.

    kicad-cli renders the pages of the project with the hierarchy of its root
    schematic (the schematic of the project name), thus each page has the
    references of its sheet path. For the root schematic, the files are all pages
    of the project. For an other file (a sub-sheet), the files are the pages of
    its sheet paths (a sheet that the hierarchy uses two times gives two pages).
    SVGs are vector graphics: clients and humans can zoom into any detail without
    quality loss. The tool needs kicad-cli.

    Args:
        schematic_path: A .kicad_sch file of the project.
        output_dir: Output directory (default: <schematic>_svg/ next to it).

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
