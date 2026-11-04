"""PCB layout and routing tools."""

import math
import re
import subprocess
from pathlib import Path

from ..parsers.kicad_versions import (
    KicadFileError,
    header_version,
    is_release_schematic,
    release_of_schematic,
    schematic_text_version,
)
from ..server import mcp
from ..utils.kicad_cli import decode_output, run_kicad_cli
from ..utils.kicad_lock import check_board_not_open, file_stamp, write_board
from ..utils.kicad_python import board_text
from ..utils.kicad_text import KiCadEncodingError, read_as_kicad

_KICAD_CLI_MISSING_MSG = """⚠️ kicad-cli not found (PATH or KiCad install directory).

Gerber export requires KiCad's command-line tools.

Please:
1. Install KiCad 7+ (https://www.kicad.org/)
2. Ensure kicad-cli is in system PATH, or set the KICAD_CLI
   environment variable to its full path
   (on Windows typically C:\\Program Files\\KiCad\\<ver>\\bin\\kicad-cli.exe)

**Manual export:**
Open the board in KiCad's PCB editor → File → Fabrication Outputs → Gerbers.
"""


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
async def setup_pcb_layout(
    schematic_path: str,
    width: float = 100.0,
    height: float = 100.0,
    unit: str = "mm",
    overwrite: bool = False,
) -> str:
    """Initialize an empty PCB with the specified board outline dimensions.

    Writes a .kicad_pcb next to the schematic containing only the board
    outline (Edge.Cuts), a standard 2-layer stack and default design rules —
    no footprints, tracks or nets.

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
        schematic_path: Path to the .kicad_sch the board is based on (used to
            derive the .kicad_pcb path and board title).
        width: Board width in the given unit.
        height: Board height in the given unit.
        unit: "mm" (default) or "mil".
        overwrite: Replace an existing .kicad_pcb file. Set it only after
            the user agreed to lose the existing board.

    Returns:
        Confirmation reporting the actual outline size, layer count and
        element counts read back from the written file, and the warning
        about the file version when there is one.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ Schematic file not found: {schematic_path}"

        pcb_path = sch_path.with_suffix(".kicad_pcb")
        stamp = file_stamp(pcb_path)
        if stamp is not None and not overwrite:
            return (
                f"Error: The board file {pcb_path} exists. This tool writes a new, "
                "empty board, which removes all footprints, tracks and zones of the "
                "existing board. Thus the tool did not write the file.\n\n"
                "Ask the user if the existing board can be replaced. Only when the "
                "user agrees, do this operation again with overwrite=True."
            )

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

        # The version of the schematic tells if its KiCad can open the board.
        try:
            version = schematic_text_version(read_as_kicad(sch_path))
        except (KicadFileError, KiCadEncodingError) as e:
            return f"❌ {sch_path}: {e} The tool did not write the board."

        # The guard of write_board() is also before pcbnew, which takes seconds.
        if open_error := check_board_not_open(pcb_path):
            return open_error

        try:
            pcb_content, kicad = await board_text(pcb_path.name, sch_path.stem, (w, h))
        except (FileNotFoundError, RuntimeError) as e:
            return f"❌ {e}"
        except subprocess.TimeoutExpired as e:
            return f"❌ pcbnew did not stop in {e.timeout:g} s. The tool did not write the board."
        board_version = header_version(pcb_content, "kicad_pcb")
        if board_version is None:
            return "❌ pcbnew wrote a board with no file version. The tool did not write the board."
        warning = _version_warning(board_version, kicad, version)

        open_error = write_board(pcb_path, pcb_content, stamp)
        if open_error:
            return open_error

        # Report what is actually in the file, never features the write did
        # not produce (issue #17).
        report = _summarize_pcb(pcb_path)
        return f"""✅ PCB layout initialized.

{warning}**Schematic:** {schematic_path}
**PCB File:** {pcb_path}
**Made by:** pcbnew of KiCad {kicad}

{report}

Next steps:
1. Import the netlist from the schematic (Update PCB from schematic)
2. Place footprints and route connections
3. Run DRC, then export Gerbers (export_gerber)
"""

    except Exception as e:
        import traceback
        return f"❌ Error setting up PCB layout: {e}\n\n{traceback.format_exc()}"


def _summarize_pcb(pcb_path: Path) -> str:
    """Read a .kicad_pcb back and report its actual outline / layers / elements.

    For a freshly-initialised board the only items carrying start/end coords
    are the four Edge.Cuts gr_lines, so the min/max of those coords is the
    exact board outline.
    """
    text = pcb_path.read_text(encoding="utf-8")

    coords = [
        (float(mx), float(my))
        for mx, my in re.findall(
            r"\((?:start|end)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\)", text
        )
    ]
    if coords:
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        outline = f"{max(xs) - min(xs):.3f} × {max(ys) - min(ys):.3f} mm"
    else:
        outline = "(could not parse Edge.Cuts outline)"

    layer_count = len(re.findall(r'^\s*\(\d+\s+"', text, re.MULTILINE))
    n_footprints = len(re.findall(r'\(footprint\s+"', text))
    n_segments = len(re.findall(r'\(segment\s', text))
    n_nets = len(re.findall(r'\(net\s+\d+\s+"', text))

    return (
        f"**Dimensions (read back):** {outline}\n"
        f"**Layers:** {layer_count}\n"
        f"**Elements:** {n_footprints} footprint(s), "
        f"{n_segments} track(s), {n_nets} net(s) declared"
    )


@mcp.tool()
async def export_gerber(
    pcb_path: str,
    output_dir: str = "",
) -> str:
    """Export a PCB to Gerber + drill files for fabrication via kicad-cli.

    Runs KiCad's command-line exporter against the .kicad_pcb and reports the
    files actually produced. Requires KiCad 7+ installed with kicad-cli on PATH.

    Args:
        pcb_path: Path to the .kicad_pcb file.
        output_dir: Directory for output (default: a "gerber" folder next to
            the PCB).

    Returns:
        Confirmation listing the generated files, or an actionable error.
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
        # "--output-dir" — KiCad 7-10 all use this form. With no --layers it
        # plots every layer defined in the board, so it generalises to N-layer.
        try:
            rc, err = await _run(["pcb", "export", "gerbers", "-o", str(out_path), str(pcb)])
        except FileNotFoundError:
            return _KICAD_CLI_MISSING_MSG
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
            return (
                f"⚠️ kicad-cli reported success but produced no files in "
                f"{out_path}.{drill_note}"
            )

        listing = "\n".join(f"- {n}" for n in new_files)
        return f"""✅ Gerber export complete.

**PCB:** {pcb_path}
**Output directory:** {out_path}

**Files generated ({len(new_files)}):**
{listing}{drill_note}

Send all of these to your fabricator (e.g. JLCPCB, PCBWay, OSH Park).
"""

    except Exception as e:
        import traceback
        return f"❌ Error exporting Gerber: {e}\n\n{traceback.format_exc()}"
