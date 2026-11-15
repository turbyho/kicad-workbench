"""The tools of the IPC API: live edits of the board in the PCB editor of KiCad (phase 3).

KiCad does each change itself (utils/ipc_client.py). The tools need a running KiCad 10
with the board open in the PCB editor, and the extra "ipc" of the server (kicad-python).
"""

import asyncio

from ..server import mcp
from ..utils.ipc_client import (
    KICAD_PYTHON_VERSION,
    LIMITS,
    IpcError,
    kicad_python_version,
    known_versions,
    open_session,
    socket_text,
)
from ..utils.tool_errors import error_answer


def _limits_table(rules) -> list[str]:
    lines = ["| Limit | Solved in |", "|---|---|"]
    for key in rules.limits:
        limit = LIMITS[key]
        lines.append(f"| {limit.text} | KiCad {limit.solved_in} |")
    return lines


def _status(board_path: str) -> str:
    lines = ["# KiCad IPC status", ""]
    installed = kicad_python_version()
    lines.append(
        f"- kicad-python: {installed or 'not installed'} (the server needs {KICAD_PYTHON_VERSION})"
    )
    lines.append(f"- Socket: {socket_text()}")
    with open_session() as session:
        state = session.state
        lines.append(f"- KiCad: {state.version} (running)")
        for board in state.boards:
            lines.append(f"- PCB editor: open, board `{board}`")
        if not state.boards:
            lines.append("- PCB editor: not open")
        lines.append(f"- Schematic editor: {'open' if state.schematic_open else 'not open'}")
        # Why a write cannot operate now: the first problem that the tool finds.
        blocker = None
        if state.boards:
            label = f"Board `{board_path}`" if board_path else "Board"
            try:
                board = session.board(board_path or state.boards[0])
                if board_path:
                    lines.append(f"- {label}: ✅ open in the PCB editor")
                # GetItems calls checkForBusy first (KiCad 10.0.6): a dialog or an
                # interactive operation gives AS_BUSY.
                count = session.call("GetItems", lambda: len(board.get_footprints()))
                lines.append(f"- Footprints on the board: {count}")
            except IpcError as error:
                lines.append(f"- {label}: ❌ {error}")
                blocker = str(error)
        else:
            blocker = "no board is open in the PCB editor"
        blocker = blocker or state.write_problem()
        if blocker:
            lines.append(f"- Writes: ❌ not possible now. {blocker}")
        else:
            lines.append(
                "- Writes: ✅ possible now (the state can change: the user can open a dialog "
                "or the schematic editor)"
            )
        lines += ["", f"## Limits of KiCad {state.version}", ""]
        lines += _limits_table(state.rules)
    lines += ["", f"Known KiCad versions of the IPC tools: {known_versions()}."]
    return "\n".join(lines)


@mcp.tool()
async def ipc_status(board_path: str = "") -> str:
    """Show the state of the IPC connection to the running KiCad.

    The IPC tools change the board that is open in the PCB editor of a running KiCad.
    Use this tool first: it shows if the IPC tools can operate now, and the limits of
    the running KiCad version.

    Args:
        board_path: The path of a .kicad_pcb file (optional). The tool then shows if
            the PCB editor has this board.

    Returns:
        Markdown: the version of kicad-python and of the running KiCad, the socket, the
        open editors and boards, the number of footprints of the open board, if the
        tools can write now (the board of board_path is open, KiCad is not busy with a
        dialog, and the limits of the version let a write operate), and the limits of
        the running KiCad version with the version that solves each limit.
    """
    try:
        return await asyncio.to_thread(_status, board_path)
    except Exception as error:
        return error_answer(error, "Error reading the IPC status")
