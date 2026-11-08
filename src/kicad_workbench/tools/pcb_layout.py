"""PCB layout and routing tools."""

import asyncio
import math
import subprocess
from pathlib import Path

from ..errors import InputError
from ..parsers.kicad_versions import (
    KicadFileError,
    header_version,
    is_release_schematic,
    release_of_schematic,
    schematic_text_version,
)
from ..parsers.sexpr import SExprError, parse
from ..server import mcp
from ..utils.file_stamp import FileStamp, file_stamp
from ..utils.file_versions import with_board_versions, with_schematic_versions
from ..utils.kicad_cli import decode_output, get_kicad_version, no_kicad_message, run_kicad_cli
from ..utils.kicad_lock import check_board_not_open, write_board, write_with_warning
from ..utils.kicad_python import KicadPythonError, board_text, check_same_kicad
from ..utils.kicad_text import KiCadEncodingError, read_as_kicad
from ..utils.tool_errors import error_answer

# The largest coordinate of KiCad: 2**31 - 1 nm.
_MAX_SIZE_MM = 2147.483647


def _version_warning(board_version: int, kicad: str, schematic_version: int) -> str:
    """Return the text about a board that the KiCad of the schematic can be too old for, or ''."""
    release = release_of_schematic(schematic_version)
    if board_version <= release.board:
        return ""
    if is_release_schematic(schematic_version):
        return (
            f"⚠️ **Warning:** The board has the file version {board_version} of the installed "
            f"KiCad {kicad}. The schematic has the file version {schematic_version}. "
            f"KiCad {release.name} cannot open the board. Tell the user. To use the board, "
            f"the user must open the project in KiCad {kicad} or later; that KiCad converts "
            "the schematic when it saves it.\n\n"
        )
    # The board version of the nightly build that saved the schematic is not known.
    return (
        f"**Note:** The board has the file version {board_version} of the installed KiCad "
        f"{kicad}. A nightly build saved the schematic (file version {schematic_version}). "
        f"KiCad {release.name} cannot open the board. The nightly build can open it only if "
        f"it reads the board version {board_version}.\n\n"
    )


@mcp.tool()
@with_schematic_versions("schematic_path")
async def setup_pcb_layout(
    schematic_path: str,
    width: float = 100.0,
    height: float = 100.0,
    unit: str = "mm",
    overwrite: bool = False,
) -> str:
    """Make a new board with an outline, next to the schematic.

    The board has the name of the schematic, a rectangle on Edge.Cuts, two copper
    layers and the default design rules. It has no footprints, tracks or nets: use
    "Update PCB from Schematic" of KiCad for the footprints.

    pcbnew of the installed KiCad makes the board (a Python with pcbnew is
    necessary), thus the board has the file format of the installed KiCad, as
    when the user makes it in the PCB editor. When that is newer than the
    KiCad of the schematic, the output has a warning: tell the user that the
    older KiCad cannot open the board. The tool does not change the project
    file.

    When the .kicad_pcb file exists, the tool does not write it and returns
    an error. A new board removes all footprints, tracks and zones of the
    existing board. Ask the user before you set overwrite=True.

    Args:
        schematic_path: The .kicad_sch file. The board gets its name and its
            title.
        width: The width of the board in the unit (default 100).
        height: The height of the board in the unit (default 100).
        unit: "mm" (default) or "mil".
        overwrite: True replaces an existing .kicad_pcb file. Set it only after
            the user agreed to lose the existing board.

    Returns:
        The size of the outline, the layers and the counts of items, read
        back from the written file, and the warning about the file version
        when there is one.
    """
    try:
        # The outline goes to pcbnew in millimetres (issue #17: the old code
        # wrote nanometres as millimetres, a 100 km outline).
        if unit == "mil":
            w = width * 0.0254
            h = height * 0.0254
        elif unit == "mm":
            w = float(width)
            h = float(height)
        else:
            return f'❌ The unit must be "mm" or "mil", not {unit!r}.'
        # pcbnew keeps a coordinate as a 32-bit number of nanometres.
        if not all(math.isfinite(size) and 0 < size <= _MAX_SIZE_MM for size in (w, h)):
            return (
                f"❌ The width and the height must be more than 0 mm and not more than "
                f"{_MAX_SIZE_MM} mm, not {width} × {height} {unit}."
            )

        sch_path = Path(schematic_path)
        pcb_path = sch_path.with_suffix(".kicad_pcb")
        checked = await asyncio.to_thread(_check_files, sch_path, pcb_path, overwrite)
        if isinstance(checked, str):
            return checked
        stamp, version = checked

        cli = await get_kicad_version()
        if cli is None:
            return f"❌ {no_kicad_message()}"
        try:
            pcb_content, kicad = await board_text(pcb_path.name, sch_path.stem, (w, h))
            check_same_kicad(kicad, "the Python with pcbnew", cli)
        except (FileNotFoundError, KicadPythonError) as e:
            return f"❌ {e} The tool did not write the board."
        except subprocess.TimeoutExpired as e:
            return f"❌ pcbnew did not stop in {e.timeout:g} s. The tool did not write the board."
        board_version = header_version(pcb_content, "kicad_pcb")
        if board_version is None:
            return "❌ pcbnew wrote a board with no file version. The tool did not write the board."
        warning = _version_warning(board_version, kicad, version)

        open_error, saved, report = await asyncio.to_thread(
            _write_and_summarize, pcb_path, pcb_content, stamp
        )
        if open_error:
            return open_error
        return f"""✅ PCB layout initialized.

{warning}**Schematic:** {schematic_path}
**PCB File:** {pcb_path}
**Made by:** pcbnew of KiCad {kicad}

{report}

Next steps:
1. Import the netlist from the schematic (Update PCB from schematic)
2. Place footprints and route connections
3. Run DRC, then export Gerbers (export_gerber){saved}
"""

    except Exception as e:
        return error_answer(e, "Error setting up PCB layout")


def _check_files(sch_path: Path, pcb_path: Path, overwrite: bool) -> tuple[FileStamp, int] | str:
    """Return the stamp of the board file and the file version of the schematic, or the
    error. The function blocks (file reads): run it off the event loop."""
    if not sch_path.exists():
        return f"❌ Schematic file not found: {sch_path}"
    try:
        pcb_path.stat()
        board_exists = True
    except FileNotFoundError:
        board_exists = False
    if board_exists and not overwrite:
        return (
            f"❌ The board file {pcb_path} exists. This tool writes a new, "
            "empty board, which removes all footprints, tracks and zones of the "
            "existing board. Thus the tool did not write the file.\n\n"
            "Ask the user if the existing board can be replaced. Only when the "
            "user agrees, do this operation again with overwrite=True."
        )
    # The write refuses the new board when an other program changes the
    # board file (or makes it) after this point.
    stamp = file_stamp(pcb_path)

    # The version of the schematic tells if its KiCad can open the board.
    try:
        version = schematic_text_version(read_as_kicad(sch_path))
    except (KicadFileError, KiCadEncodingError) as e:
        return f"❌ {sch_path}: {e} The tool did not write the board."

    # The guard of write_board() is also before pcbnew, which takes seconds.
    if open_error := check_board_not_open(pcb_path):
        return open_error
    return stamp, version


def _write_and_summarize(
    pcb_path: Path, content: str, stamp: FileStamp
) -> tuple[str | None, str, str]:
    """Write the board, and return (the error of the write or None, the warning of the
    write, the summary of the board). The function blocks: run it off the event loop."""
    open_error, saved = write_with_warning(write_board, pcb_path, content, stamp)
    if open_error:
        return open_error, saved, ""
    # Report what is in the file, never features the write did not produce
    # (issue #17). The board is written: an error of the summary is a warning.
    try:
        report = _summarize_pcb(read_as_kicad(pcb_path))
    except (OSError, KiCadEncodingError, SExprError, InputError) as e:
        report = f"⚠️ The tool cannot read back the board that it wrote: {e}"
    return None, saved, report


def _summarize_pcb(text: str) -> str:
    """Report the outline, the layers and the items of a board that pcbnew made.

    The text is the board file that the tool wrote. The script of the tool gives a new
    board the outline as four segments on Edge.Cuts (kicad_python._board_text_sync:
    PCB_SHAPE with SHAPE_T_SEGMENT, gr_line in the file), thus the minimum and the
    maximum of their points are the outline. An other item on Edge.Cuts gives an
    outline that is not known.

    Raises:
        SExprError, InputError: The text is not a valid S-expression, or a point
            is not a number.
    """
    board = parse(text)
    edge = [
        item
        for item in board.children()
        if (item.head or "").startswith("gr_")
        and (layer := item.first("layer")) is not None
        and layer.value(0) == "Edge.Cuts"
    ]
    points = [
        (point.number(0), point.number(1)) if point is not None else (None, None)
        for item in edge
        for point in (item.first("start"), item.first("end"))
    ]
    complete = all(None not in point for point in points)
    if edge and all(item.head == "gr_line" for item in edge) and complete:
        xs = [x for x, _y in points]
        ys = [y for _x, y in points]
        outline = f"{max(xs) - min(xs):.3f} × {max(ys) - min(ys):.3f} mm"
    else:
        outline = (
            "(not known: the board has no outline of gr_line items with a start and an end "
            "on Edge.Cuts)"
        )
    layers = board.first("layers")
    counts = {head: len(board.children(head)) for head in ("footprint", "segment", "net")}

    return (
        f"**Dimensions (read back):** {outline}\n"
        f"**Layers:** {len(layers.children()) if layers is not None else 0}\n"
        f"**Elements:** {counts['footprint']} footprint(s), "
        f"{counts['segment']} track(s), {counts['net']} net(s) declared"
    )


@mcp.tool()
@with_board_versions("pcb_path")
async def export_gerber(
    pcb_path: str,
    output_dir: str = "",
) -> str:
    """Export the Gerber and drill files of a board with kicad-cli, for fabrication.

    The tool runs "kicad-cli pcb export gerbers", then "kicad-cli pcb export drill",
    and gives the files that kicad-cli wrote.

    Args:
        pcb_path: The .kicad_pcb file.
        output_dir: Optional folder of the files. Default: the folder "gerber"
            next to the board.

    Returns:
        The files that kicad-cli wrote, or an error.
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ PCB file not found: {pcb_path}"

        out_path = Path(output_dir) if output_dir else pcb.parent / "gerber"
        out_path.mkdir(parents=True, exist_ok=True)
        # Track name + mtime: a re-export rewrites the same files, and a pure
        # name diff would then look like "nothing was produced".
        before = {p.name: p.stat().st_mtime_ns for p in out_path.iterdir()}

        async def _run(args: list[str]) -> tuple[int, str]:
            r = await run_kicad_cli(args, timeout=180)
            return r.returncode, decode_output(r.stderr).strip()

        # Gerbers first (required); drill is best-effort afterwards. Note the
        # subcommand is "gerbers" (plural) and the output flag is "-o", not
        # "--output-dir" — KiCad 8, 9 and 10 use this form. With no --layers it
        # plots every layer defined in the board, so it generalises to N-layer.
        try:
            rc, err = await _run(["pcb", "export", "gerbers", "-o", str(out_path), str(pcb)])
        except FileNotFoundError as e:  # kicad-cli is not there
            return f"❌ {e}"
        if rc != 0:
            return (
                f"❌ Gerber export failed (kicad-cli rc={rc}):\n\n"
                f"{err or '(no stderr)'}"
            )

        drill_note = ""
        try:
            rc2, err2 = await _run(["pcb", "export", "drill", "-o", str(out_path), str(pcb)])
            if rc2 != 0:
                drill_note = (
                    f"\n\n⚠️ Drill export returned rc={rc2}: "
                    f"{err2 or '(no stderr)'} (Gerbers still produced)"
                )
        except FileNotFoundError:
            # kicad-cli was available for gerber, so this shouldn't happen —
            # surface it rather than swallow.
            drill_note = "\n\n⚠️ kicad-cli unavailable for drill export."

        new_files = sorted(
            p.name
            for p in out_path.iterdir()
            if p.name not in before or p.stat().st_mtime_ns != before[p.name]
        )
        if not new_files:
            return f"❌ kicad-cli reported success but produced no files in {out_path}.{drill_note}"

        listing = "\n".join(f"- {n}" for n in new_files)
        return f"""✅ Gerber export complete.

**PCB:** {pcb_path}
**Output directory:** {out_path}

**Files generated ({len(new_files)}):**
{listing}{drill_note}

Send all of these to your fabricator (e.g. JLCPCB, PCBWay, OSH Park).
"""

    except Exception as e:
        return error_answer(e, "Error exporting Gerber")
