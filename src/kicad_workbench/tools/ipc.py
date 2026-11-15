"""The tools of the IPC API: live edits of the board in the PCB editor of KiCad (phase 3).

KiCad does each change itself (utils/ipc_client.py). The tools need a running KiCad 10
with the board open in the PCB editor, and the extra "ipc" of the server (kicad-python).
"""

import asyncio
from collections import Counter

from ..errors import InputError
from ..server import mcp
from ..utils.ipc_client import (
    KICAD_PYTHON_VERSION,
    LIMITS,
    BoardItem,
    IpcError,
    kicad_python_version,
    known_versions,
    open_session,
    socket_text,
)
from ..utils.tool_errors import error_answer
from .library import cell


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
                # A dialog or an interactive operation gives AS_BUSY.
                session.check_busy(board)
                lines.append("- KiCad busy: no")
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
        open editors and boards, if KiCad is busy, if the tools can write now (the board of board_path is open, KiCad is not busy with a
        dialog, and the limits of the version let a write operate), and the limits of
        the running KiCad version with the version that solves each limit.
    """
    try:
        return await asyncio.to_thread(_status, board_path)
    except Exception as error:
        return error_answer(error, "Error reading the IPC status")


# The largest number of rows of an item table. The counts of each type cover all items.
MAX_ROWS = 100

SELECT_MODES = ("replace", "add", "remove", "clear")

# The type label of an item that KiCad gave with no data.
NO_DATA_KIND = "(no data)"

FILTER_NOTE = (
    "ℹ️ The selection filter of the PCB editor does not apply: the API cannot read it, "
    "and AddToSelection selects each item directly (SELECTION_TOOL::AddItemsToSel of "
    "KiCad 10.0.6)."
)
NET_NOTE = (
    'ℹ️ A net gives its tracks, arcs, vias and shapes: the item types of "Select All '
    'Tracks in Net" (SelectAllItemsOnNet). Pads and zones are not in it. With a '
    "selection filter that the user changed, the action of KiCad can select fewer items."
)


def _count(number: int, noun: str) -> str:
    """A number and a noun, for example "1 item" or "3 items"."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _mm(nm: int) -> str:
    """A length in nm as mm, with no rounding (1 nm = 0.000001 mm)."""
    sign = "-" if nm < 0 else ""
    whole, rest = divmod(abs(nm), 1_000_000)
    return f"{sign}{whole}.{rest:06d}".rstrip("0").rstrip(".")


def _items_text(items: list[BoardItem]) -> list[str]:
    """The counts of each item type and a table of the items (MAX_ROWS rows)."""
    if not items:
        return ["No items."]
    counts = Counter(item.kind or NO_DATA_KIND for item in items)
    lines = ["Items: " + ", ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))]
    lines += [
        "",
        "| Type | Reference, number, name or text | Net | Layer | Position (mm) | ID |",
        "|---|---|---|---|---|---|",
    ]
    for item in items[:MAX_ROWS]:
        if item.problem:
            lines.append(f"| {cell(item.kind or NO_DATA_KIND)} | ⚠️ {cell(item.problem)} | | | | |")
            continue
        position = f"{_mm(item.position[0])}, {_mm(item.position[1])}" if item.position else ""
        cells = (item.kind, item.text, item.net, item.layer, position, item.id)
        lines.append("| " + " | ".join(cell(text) for text in cells) + " |")
    if len(items) > MAX_ROWS:
        lines.append("")
        lines.append(f"The table shows {MAX_ROWS} of {len(items)} items.")
    problems = sum(1 for item in items if item.problem)
    if problems:
        shown = sum(1 for item in items[:MAX_ROWS] if item.problem)
        lines.append("")
        lines.append(
            f"⚠️ The tool cannot read {problems} of the items ({shown} of them in the table)."
        )
    return lines


def _get_selection(board_path: str) -> str:
    with open_session() as session:
        board = session.board(board_path)
        items = session.selection(board)
    lines = [f"# Selection in the PCB editor: {_count(len(items), 'item')}", ""]
    return "\n".join(lines + _items_text(items))


@mcp.tool()
async def ipc_get_selection(board_path: str) -> str:
    """Show the items that the user selected in the PCB editor of the running KiCad.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.

    Returns:
        Markdown: the number of selected items of each type, and a table of the items
        (type; reference of a footprint, number of a pad, name of a zone or text; net;
        layers; position in mm; ID). The table shows 100 items or less. An item that
        KiCad or kicad-python cannot give has a warning.
    """
    try:
        return await asyncio.to_thread(_get_selection, board_path)
    except Exception as error:
        return error_answer(error, "Error reading the selection")


def _select_items(board_path: str, references: list[str], nets: list[str], mode: str) -> str:
    if mode not in SELECT_MODES:
        raise InputError(f"mode must be one of {', '.join(SELECT_MODES)}, not {mode!r}.")
    references = list(dict.fromkeys(references))
    nets = list(dict.fromkeys(nets))
    if mode == "clear" and (references or nets):
        raise InputError('mode "clear" takes no references and no nets.')
    if mode != "clear" and not (references or nets):
        raise InputError(f'mode "{mode}" needs references, nets or the two.')
    notes = []
    with open_session() as session:
        board = session.board(board_path)
        targets: list[str] = []
        missing = []
        if references:
            # A reference of the board can be on more than one footprint.
            footprints = session.footprints(board)
            for reference in references:
                found = [fp.id for fp in footprints if fp.text == reference]
                if not found:
                    missing.append(f"reference {reference}")
                if len(found) > 1:
                    notes.append(f"⚠️ {len(found)} footprints have the reference {reference}.")
                targets += found
        if nets:
            names = set(session.net_names(board))
            missing += [f"net {net}" for net in nets if net not in names]
        if missing:
            raise InputError(
                "The board has no "
                + ", no ".join(missing)
                + ". The tool did not change the selection. References and net names are "
                "case-sensitive."
            )
        if nets:
            net_items = session.net_items(board, nets)
            notes += [
                f"ℹ️ The net {net} has no tracks, arcs, vias or shapes."
                for net in nets
                if not any(item.net == net for item in net_items)
            ]
            targets += [item.id for item in net_items if item.id]
            notes.append(NET_NOTE)
        targets = list(dict.fromkeys(targets))
        # The changes of the selection need an editor that is not busy: GetSelection does
        # not check it.
        session.check_busy(board)
        before = session.selection(board)
        try:
            if mode in ("replace", "clear"):
                session.clear_selection(board)
            if mode == "remove":
                selection = session.remove_from_selection(board, targets)
            elif mode != "clear" and targets:
                selection = session.add_to_selection(board, targets)
            else:
                selection = session.selection(board)
        except Exception as error:
            restored = _restore(session, board, before)
            if isinstance(error, IpcError):
                raise IpcError(f"{error} {restored}") from None
            raise
    selected = {item.id for item in selection}
    before_ids = {item.id for item in before}
    if mode in ("replace", "add"):
        failed = [item_id for item_id in targets if item_id not in selected]
        changed = len([item_id for item_id in targets if item_id not in before_ids])
        if failed:
            notes.append(f"⚠️ KiCad did not select {_count(len(failed), 'item')} (ID not found).")
        notes.append(FILTER_NOTE)
    elif mode == "remove":
        failed = [item_id for item_id in targets if item_id in selected]
        changed = len([item_id for item_id in targets if item_id in before_ids])
        if failed:
            notes.append(f"⚠️ {_count(len(failed), 'item')} of the request are still selected.")
    else:
        failed, changed = [], len(before)
    mark = "⚠️" if failed else "✅"
    size = _count(len(selection), "item")
    title = {
        "replace": f"{mark} The selection has {size}",
        "add": f"{mark} Added {_count(changed, 'item')}: the selection has {size}",
        "remove": f"{mark} Removed {_count(changed, 'item')}: the selection has {size}",
        "clear": f"{mark} Cleared the selection ({_count(changed, 'item')}): it has {size}",
    }[mode]
    lines = [title, ""]
    if notes:
        lines += notes + [""]
    return "\n".join(lines + _items_text(selection))


def _restore(session, board, before: list[BoardItem]) -> str:
    """Set the selection of the start of the tool again after an error, and return the
    text for the user."""
    ids = [item.id for item in before if item.id]
    try:
        session.clear_selection(board)
        if ids:
            session.add_to_selection(board, ids)
    except IpcError as error:
        return (
            "The tool could have changed the selection before the error, and it could not "
            f"set the selection of the start again ({error})."
        )
    if len(ids) < len(before):
        return (
            "The tool set the selection of the start again, except "
            f"{_count(len(before) - len(ids), 'item')} with no ID."
        )
    return "The tool set the selection of the start again."


@mcp.tool()
async def ipc_select_items(
    board_path: str,
    references: list[str] | None = None,
    nets: list[str] | None = None,
    mode: str = "replace",
) -> str:
    """Select items in the PCB editor of the running KiCad, by reference or by net.

    The selection is no change of the board: KiCad makes no undo step for it. The
    selection filter of the PCB editor does not apply: the API cannot read it.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.
        references: The references of footprints, for example ["R1", "U3"]. They are
            case-sensitive.
        nets: The net names, for example ["GND", "/SDA"]. They are case-sensitive. A net
            gives its tracks, arcs, vias and shapes: the item types of "Select All
            Tracks in Net" of KiCad.
        mode: "replace" (clear the selection, then select the items), "add", "remove"
            (remove the items from the selection), or "clear" (no references and no
            nets).

    Returns:
        Markdown: the selection after the change (the number of items of each type and a
        table of 100 items or less), and notes: a reference on more than one footprint,
        a net with no items, an item that KiCad did not select. A reference or a net
        that the board does not have gives an error, and the selection does not change.
        After an other error, the tool sets the selection of the start again.
    """
    try:
        return await asyncio.to_thread(
            _select_items, board_path, list(references or []), list(nets or []), mode
        )
    except Exception as error:
        return error_answer(error, "Error changing the selection")
