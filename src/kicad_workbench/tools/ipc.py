"""The tools of the IPC API: live edits of the board in the PCB editor of KiCad (phase 3).

KiCad does each change itself (utils/ipc_client.py). The tools need a running KiCad 10
with the board open in the PCB editor, and the extra "ipc" of the server (kicad-python).
"""

import asyncio
import os
import time
from collections import Counter
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, Field

from ..errors import InputError
from ..parsers.kicad_versions import RELEASES
from ..server import mcp
from ..utils.file_stamp import FileStamp, file_stamp
from ..utils.file_versions import board_file_version
from ..utils.ipc_client import (
    KICAD_PYTHON_VERSION,
    LIMITS,
    TIMEOUT_MS,
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
        lines.append(f"| {limit.text} | {limit.solved_in} |")
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
                session.board(board_path or state.boards[0])
                if board_path:
                    lines.append(f"- {label}: ✅ open in the PCB editor")
            except IpcError as error:
                lines.append(f"- {label}: ❌ {error}")
                blocker = str(error)
        else:
            blocker = "No board is open in the PCB editor."
        if blocker:
            lines.append(f"- Writes: ❌ not possible now. {blocker}")
        else:
            # The API has no request that tells if KiCad is busy: each request can get
            # AS_BUSY ("KiCad is busy performing an operation", envelope.proto).
            lines.append(
                "- Writes: ✅ the board is open in the PCB editor. KiCad refuses each "
                "request while it is busy (a dialog, an interactive operation): the tool "
                "then gives the error."
            )
        problem = state.commit_problem()
        if problem:
            lines.append(
                f"- Commits: ❌ not possible now. {problem} The IPC tools of this version use no commit."
            )
        else:
            lines.append("- Commits: ✅ possible now")
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
        open editors and boards, the line "Writes" (the board of board_path is open in
        the PCB editor; KiCad refuses each request while it is busy, and the API has no
        request that tells this before),
        the line "Commits" (a commit with BeginCommit is possible: KiCad 10.0.6 stops on
        a commit while the schematic editor is open), and the limits of the running KiCad
        version with the version that solves each limit.
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


# Step 3C of phase 3: the first changes of the board.

# The fields of a placement that need an update of the footprint (UpdateItems): the tool
# has no rules for them yet (the open decision of step 3C, limits
# footprint_update_loses_data and footprint_move_children).
_UPDATE_FIELDS = ("x_mm", "y_mm", "rotation_deg", "locked")

# The copper layer of each side of the board.
_SIDE_LAYER = {"top": "F.Cu", "bottom": "B.Cu"}


class FootprintPlacement(BaseModel):
    """The placement of one footprint."""

    reference: str = Field(description="The reference of the footprint, for example U3.")
    side: Literal["top", "bottom"] | None = Field(
        default=None, description='The side of the board: "top" or "bottom".'
    )
    x_mm: float | None = Field(default=None, description="The X position in mm.")
    y_mm: float | None = Field(default=None, description="The Y position in mm.")
    rotation_deg: float | None = Field(default=None, description="The rotation in degrees.")
    locked: bool | None = Field(default=None, description="Lock or unlock the footprint.")


def _update_refusal(rules, version: str, updates: list[str], asked: set[str]) -> str:
    """The error for the fields of _UPDATE_FIELDS (asked: the names of the fields in the
    request): a limit of the KiCad version for each field that the request has."""
    if rules.has_limit("footprint_update_loses_data"):
        reasons = [
            f"KiCad {version} cannot change them with no loss of data: "
            f"{LIMITS['footprint_update_loses_data'].text}"
        ]
    else:
        reasons = []
        if asked & {"x_mm", "y_mm", "rotation_deg"}:
            reasons.append(LIMITS["footprint_move_children"].text)
        if "locked" in asked:
            reasons.append(f"The tool cannot change the lock of a footprint with KiCad {version}.")
    return (
        "The tool cannot change the position, the rotation or the lock of a footprint: "
        f"{' '.join(reasons)} Asked: {'; '.join(updates)}. The tool changed nothing. Only "
        "side is possible."
    )


# The flip directions of FlipItems (BFD_LEFT_RIGHT, BFD_TOP_BOTTOM).
FlipDirection = Literal["left_right", "top_bottom"]


def _place_footprints(
    board_path: str,
    placements: list[FootprintPlacement],
    flip_direction: FlipDirection,
    override_locks: bool,
) -> str:
    if not placements:
        raise InputError("placements is empty.")
    if flip_direction not in get_args(FlipDirection):
        raise InputError(
            f'flip_direction must be "left_right" or "top_bottom", not {flip_direction!r}.'
        )
    counts = Counter(placement.reference for placement in placements)
    twice = sorted(reference for reference, count in counts.items() if count > 1)
    if twice:
        raise InputError(f"placements has more than one entry for {', '.join(twice)}.")
    updates = [
        f"{placement.reference}: "
        + ", ".join(name for name in _UPDATE_FIELDS if getattr(placement, name) is not None)
        for placement in placements
        if any(getattr(placement, name) is not None for name in _UPDATE_FIELDS)
    ]
    with open_session() as session:
        board = session.board(board_path)
        if updates:
            asked = {
                name
                for placement in placements
                for name in _UPDATE_FIELDS
                if getattr(placement, name) is not None
            }
            raise IpcError(
                _update_refusal(session.state.rules, session.state.version, updates, asked)
            )
        by_reference: dict[str, list[BoardItem]] = {}
        unreadable = 0
        for footprint in session.footprints(board):
            if footprint.problem:
                unreadable += 1
                continue
            by_reference.setdefault(footprint.text, []).append(footprint)
        problems, rows, flips = [], [], []
        for placement in placements:
            found = by_reference.get(placement.reference, [])
            if len(found) != 1:
                if found:
                    problems.append(
                        f"{placement.reference}: {len(found)} footprints have this reference"
                    )
                elif unreadable:
                    problems.append(
                        f"{placement.reference}: no footprint that the tool can read has this "
                        f"reference (references are case-sensitive; KiCad gave "
                        f"{_count(unreadable, 'footprint')} that the tool cannot read)"
                    )
                else:
                    problems.append(
                        f"{placement.reference}: the board has no footprint with this "
                        "reference (references are case-sensitive)"
                    )
                continue
            footprint = found[0]
            if footprint.layer not in _SIDE_LAYER.values():
                problems.append(f"{placement.reference}: the footprint is on {footprint.layer}")
                continue
            flip = bool(placement.side) and _SIDE_LAYER[placement.side] != footprint.layer
            if flip and footprint.locked is None and not override_locks:
                problems.append(
                    f"{placement.reference}: KiCad gave no lock state of the footprint (the "
                    "PCB editor does not flip a locked footprint: give override_locks=true "
                    "to flip it in all cases)"
                )
                continue
            if flip and footprint.locked and not override_locks:
                # The Flip of the PCB editor leaves out a locked footprint
                # (FilterCollectorForLockedItems), except with "Override locks".
                problems.append(
                    f"{placement.reference}: the footprint is locked (the PCB editor flips it "
                    "only with Override locks: give override_locks=true)"
                )
                continue
            if flip:
                flips.append(footprint.id)
            rows.append((placement, footprint))
        if problems:
            raise InputError("The tool changed nothing: " + "; ".join(problems) + ".")
        flipped: dict[str, BoardItem] = {}
        messages: list[str] = []
        unknown = 0
        if flips:
            flipped, messages, unknown = session.flip_items(board, flips, flip_direction)
    failed = [item_id for item_id in flips if item_id not in flipped]
    lines = []
    if unknown:
        lines.append(
            f"⚠️ KiCad flipped {_count(unknown, 'item')} that the tool cannot read: the tool "
            f"cannot tell which footprints they are. {_count(len(flips) - len(failed), 'footprint')} "
            'flipped for sure, in one undo step ("Flipped items via API"). Tell the user: '
            "look at the board in KiCad."
        )
        if messages:
            lines.append(f"Messages of KiCad: {'; '.join(messages)}.")
    elif failed:
        done = len(flips) - len(failed)
        lines.append(
            f"⚠️ KiCad did not flip {_count(len(failed), 'footprint')}"
            + (
                f". It flipped {_count(done, 'footprint')} in one undo step "
                '("Flipped items via API").'
                if done
                else ". The board did not change."
            )
        )
        if messages:
            lines.append(f"Messages of KiCad: {'; '.join(messages)}.")
    elif flips:
        lines.append(
            f'✅ Flipped {_count(len(flips), "footprint")} in one undo step ("Flipped items via API").'
        )
    else:
        lines.append("✅ No change: each footprint is on the side of the request.")
    if flips:
        lines += [
            "",
            f"Flip: {flip_direction.replace('_', '/')} around the position of the footprint.",
        ]
    lines += [
        "",
        "| Reference | Side before | Side after | Position (mm) | Note |",
        "|---|---|---|---|---|",
    ]
    for placement, footprint in rows:
        item = flipped.get(footprint.id)
        new = item or footprint
        position = f"{_mm(new.position[0])}, {_mm(new.position[1])}" if new.position else ""
        note = ""
        if footprint.id in failed:
            note = "not known" if unknown else "not flipped"
        if item is not None and footprint.locked:
            note = "locked (override_locks)"
        lines.append(
            f"| {cell(placement.reference)} | {footprint.layer} | {new.layer} | {position} | "
            f"{cell(note)} |"
        )
    return "\n".join(lines)


@mcp.tool()
async def ipc_place_footprints(
    board_path: str,
    placements: list[FootprintPlacement],
    flip_direction: FlipDirection,
    override_locks: bool = False,
) -> str:
    """Place footprints on the board in the PCB editor of the running KiCad.

    KiCad does the change. One call is one undo step. The tool checks all placements
    first, and changes nothing when a check fails. The tool can only change the side:
    a change of the position, the rotation or the lock gives an error (the limits
    footprint_update_loses_data and footprint_move_children of ipc_status).

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.
        placements: For each footprint: the reference, and the side ("top" or
            "bottom"). x_mm, y_mm, rotation_deg and locked give an error.
        flip_direction: The flip of a footprint to the other side: "left_right" (around
            the Y axis) or "top_bottom" (around the X axis). KiCad flips around the
            position of the footprint. The API cannot read the flip direction of the PCB
            editor (Editing Options): when the user did not give the direction, ask the
            user.
        override_locks: Flip also a locked footprint, as the PCB editor does with
            "Override locks". With false, a locked footprint that must change its side
            gives an error.

    Returns:
        Markdown: the result, the flip direction, and a table with the
        side before and after and the position of each footprint.
    """
    try:
        return await asyncio.to_thread(
            _place_footprints, board_path, placements, flip_direction, override_locks
        )
    except Exception as error:
        return error_answer(error, "Error placing the footprints")


def _release_of_board(version: int | None) -> str | None:
    """The KiCad release that writes a board version, or None."""
    return next((release.name for release in RELEASES if release.board == version), None)


def _disk_state(path: Path) -> tuple[int, int, FileStamp] | None:
    """The inode, the modification time and the stamp of the bytes of a file, or None.

    A save changes the bytes, or it writes a new file (a new inode) or a new time. The
    stamp of the bytes also finds a save on a file system with coarse times."""
    if not path.exists():
        return None
    status = path.stat()
    return status.st_ino, status.st_mtime_ns, file_stamp(path)


def _save_board(board_path: str) -> str:
    path = Path(board_path)
    with open_session() as session:
        board = session.board(board_path)
        # SavePcbFile of KiCad 10.0.6 opens an error dialog (DisplayError) when it cannot
        # write a file, and a modal dialog blocks the API: the tool checks the files
        # first. The project file comes from the project of the open document.
        folder = path.resolve().parent
        if not os.access(folder, os.W_OK):
            raise InputError(f"The folder {folder} is not writable. The tool did not save.")
        for file in (path, session.project_file(board)):
            if file is not None and file.exists() and not os.access(file, os.W_OK):
                raise InputError(f"{file} is not writable. The tool did not save.")
        before = board_file_version(path)
        state = _disk_state(path)
        start = time.monotonic()
        # The answer comes after SaveBoard. An error dialog of a save that failed is
        # modal in the request: the answer then does not come in time (NO_ANSWER).
        session.save_board(board)
        seconds = time.monotonic() - start
    after = board_file_version(path)
    if _disk_state(path) == state:
        return (
            f"⚠️ KiCad answered the save, but the file `{board_path}` did not change (the "
            "same bytes, inode and time). Tell the user: look for a message of KiCad in "
            "the PCB editor."
        )
    lines = [f"✅ KiCad saved the board `{board_path}` ({seconds:.2f} s)."]
    if after is None:
        lines.append(
            "⚠️ The tool cannot read the file version of the saved board. Tell the user: "
            "open the board in KiCad to check it."
        )
    elif before is not None and before != after:
        old = _release_of_board(before)
        new = _release_of_board(after)
        lines.append(
            f"⚠️ KiCad changed the file version from {before} to {after} (KiCad "
            f"{new or 'of the running version'}). "
            + (f"KiCad {old} cannot open the board now. " if old else "")
            + "Tell the user."
        )
    return "\n".join(lines)


@mcp.tool()
async def ipc_save_board(board_path: str) -> str:
    """Save the board of the PCB editor of the running KiCad into its file.

    KiCad saves the board as File > Save does, also the project file. KiCad writes the
    file version of the running KiCad.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.

    Returns:
        Markdown: the result, and a warning when KiCad changed the file version.
    """
    try:
        return await asyncio.to_thread(_save_board, board_path)
    except Exception as error:
        return error_answer(error, "Error saving the board")


# The interval of the requests while KiCad gives AS_BUSY after RefillZones.
REFILL_POLL_S = 0.2


def _refill_zones(board_path: str, timeout_s: float) -> str:
    if not 1 <= timeout_s <= 3600:
        raise InputError(f"timeout_s must be 1 to 3600, not {timeout_s}.")
    # Each request of the session can wait for the fill (KiCad 10.0.6 and 10.0.8), thus
    # the timeout of each answer is the longer of TIMEOUT_MS and timeout_s.
    with open_session(timeout_ms=max(TIMEOUT_MS, int(timeout_s * 1000))) as session:
        board = session.board(board_path)
        _, filled_before = session.copper_zones(board)
        start = time.monotonic()
        session.refill_zones(board)
        # board_commands.proto: KiCad gives AS_BUSY to each request until the fill has
        # completed. Thus repeat the request while it gets AS_BUSY. (KiCad 10.0.6 and
        # 10.0.8 do not give AS_BUSY: the request waits for the end of the fill.)
        while True:
            try:
                copper, filled = session.copper_zones(board)
                break
            except IpcError as error:
                if error.no_answer:
                    raise IpcError(
                        f"KiCad started the fill, but it gave no answer in {timeout_s:g} s. "
                        "The fill can still run: look at KiCad, then call the tool again "
                        "with a larger timeout_s.",
                        no_answer=True,
                    ) from None
                if not error.busy:
                    raise
                if time.monotonic() - start >= timeout_s:
                    raise IpcError(
                        f"KiCad started the fill, but it was still busy after {timeout_s:g} "
                        "s (AS_BUSY). The fill can still run: look at KiCad, then call the "
                        "tool again with a larger timeout_s.",
                        busy=True,
                    ) from None
                time.sleep(REFILL_POLL_S)
        seconds = time.monotonic() - start
    lines = [
        f'ℹ️ KiCad ended "Fill All Zones" in {seconds:.2f} s: {filled} of {copper} copper '
        f"zones have a fill (before: {filled_before}).",
        "",
        'The fill is its own undo step in KiCad ("Fill Zone(s)").',
        f"⚠️ {LIMITS['refill_progress'].text} A cancel keeps the fill of the start: tell "
        "the user to look at the zones when the fill was long.",
    ]
    if filled < copper:
        lines.append(
            f"⚠️ {copper - filled} copper zones have no fill: no area to fill, or the user "
            "cancelled the fill."
        )
    return "\n".join(lines)


@mcp.tool()
async def ipc_refill_zones(board_path: str, timeout_s: float = 120) -> str:
    """Fill all zones of the board in the PCB editor of the running KiCad.

    KiCad fills the zones as "Fill All Zones" does, with its progress dialog. The tool
    waits for the end of the fill.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.
        timeout_s: The longest time to wait for the end of the fill, 1 to 3600 s.

    Returns:
        Markdown: the time of the fill and the number of the copper zones with a fill,
        before and after. KiCad gives no result of the fill: the tool cannot see if the
        user cancelled it.
    """
    try:
        return await asyncio.to_thread(_refill_zones, board_path, timeout_s)
    except Exception as error:
        return error_answer(error, "Error filling the zones")
