"""The tools of the IPC API: live edits of the board in the PCB editor of KiCad (phase 3).

KiCad does each change itself (utils/ipc_client.py). The tools need a running KiCad 10
with the board open in the PCB editor, and the extra "ipc" of the server (kicad-python).
"""

import asyncio
import math
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
    NetClassSizes,
    ZoneSettings,
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
    footprint_update_loses_data and footprint_move_children of ipc_status). A
    footprint that is not on F.Cu or B.Cu gives an error.

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


# Step 3D: new items (tracks, vias, a copper zone, the board outline). Each tool makes
# one commit (BeginCommit, EndCommit): one undo step with its message.

UNDO_PREFIX = "kicad-workbench: "
# Zone.priority is a uint32 (board_types.proto).
UINT32_MAX = 2**32 - 1
# The limits of the zone dialog (zones.h of KiCad 10.0.6).
ZONE_THICKNESS_MIN_MM = 0.025  # ZONE_THICKNESS_MIN_VALUE_MM
ZONE_CLEARANCE_MAX_MM = 100.0  # ZONE_CLEARANCE_MAX_VALUE_MM
HATCH_PITCH_MIN_MM = 0.1  # ZONE_BORDER_HATCH_MINDIST_MM
HATCH_PITCH_MAX_MM = 2.0  # ZONE_BORDER_HATCH_MAXDIST_MM
DRC_NOTE = (
    "⚠️ KiCad ran no DRC: the API cannot run it, and KiCad checks no clearance of the "
    "items that a tool adds (limit no_drc). Tell the user: save the board "
    "(ipc_save_board), then run DRC (run_drc), or run DRC in the PCB editor."
)


class Point(BaseModel):
    """A point of the board in mm."""

    x_mm: float = Field(description="The X position in mm.")
    y_mm: float = Field(description="The Y position in mm.")


class TrackSegment(BaseModel):
    """One straight track."""

    start: Point = Field(description="The start of the track.")
    end: Point = Field(description="The end of the track.")
    layer: str = Field(description="A copper layer of the board, for example F.Cu or In1.Cu.")


# KiCad stores a coordinate or a length of the board as an int of nm (about 2147 mm).
MAX_NM = 2**31 - 1


def _storable(mm: float, error: str) -> int:
    """A value in mm as nm, when KiCad can store it (finite, an int of nm)."""
    if not math.isfinite(mm) or abs(mm) * 1_000_000 > MAX_NM:
        raise InputError(error)
    return round(mm * 1_000_000)


def _nm(mm: float, name: str, *, minimum: float | None = None, maximum: float | None = None) -> int:
    """A length in mm as nm, in its range: with no minimum, more than 0; else minimum or
    more, and maximum or less when it is given (as UNIT_BINDER::Validate of a KiCad
    dialog). Raises InputError out of the range."""
    nm = _storable(mm, f"{name} is not a length that KiCad can store: {mm}.")
    if minimum is None:
        if not mm > 0:
            raise InputError(f"{name} must be more than 0 mm, not {mm:g}.")
    elif maximum is not None:
        if not minimum <= mm <= maximum:
            raise InputError(f"{name} must be {minimum:g} to {maximum:g} mm, not {mm:g}.")
    elif mm < minimum:
        raise InputError(f"{name} must be {minimum:g} mm or more, not {mm:g}.")
    return nm


def _xy(point: Point) -> tuple[int, int]:
    error = f"A point has no position that KiCad can store: ({point.x_mm}, {point.y_mm})."
    return _storable(point.x_mm, error), _storable(point.y_mm, error)


def _cross(o: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> int:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_touch(p1, p2, q1, q2) -> bool:
    """True when the segments p1-p2 and q1-q2 have a common point."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and d1 and d2 and ((d3 > 0) != (d4 > 0)) and d3 and d4:
        return True

    def on(a, b, c):
        return (
            _cross(a, b, c) == 0
            and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
        )

    return on(q1, q2, p1) or on(q1, q2, p2) or on(p1, p2, q1) or on(p1, p2, q2)


def _polygon(outline: list[Point]) -> list[tuple[int, int]]:
    """The corners of a closed polygon in nm. Refuse a repeated corner (also the first
    corner again at the end) and a polygon with no area."""
    points = [_xy(point) for point in outline]
    if len(points) < 3:
        raise InputError(f"outline must have 3 or more points, not {len(points)}.")
    for index, point in enumerate(points):
        if point == points[index - 1]:
            raise InputError(
                f"outline has the same point two times one after the other (point "
                f"{index + 1 if index else len(points)}). Do not give the first point again "
                "at the end: the polygon is closed."
            )
    edges = list(zip(points, points[1:] + points[:1], strict=True))
    for i, (a1, a2) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue  # Neighbors share a corner.
            b1, b2 = edges[j]
            if _segments_touch(a1, a2, b1, b2):
                raise InputError(
                    f"outline intersects itself: the edges {i + 1} and {j + 1} have a common point."
                )
    area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in edges)
    if area == 0:
        raise InputError("outline has no area.")
    return points


def _check_net(session, board, net: str) -> None:
    """Refuse an empty net name, and a net that the board does not have: CreateItems
    makes a new net for an unknown name (limit unknown_net)."""
    if not net:
        raise InputError("net is empty: give the name of a net of the board.")
    if net not in session.net_names(board):
        raise InputError(
            f"The board has no net {net!r} (net names are case-sensitive). The tool changed "
            "nothing."
        )


def _check_layers(session, board, layers: list[str]) -> None:
    copper = session.copper_layers(board)
    bad = sorted({layer for layer in layers if layer not in copper})
    if bad:
        raise InputError(
            f"{', '.join(bad)}: not a copper layer of the board (the board has "
            f"{', '.join(copper)}). The tool changed nothing."
        )


def _created_table(created: list[BoardItem]) -> list[str]:
    lines = ["| Item | Layer | Position (mm) | ID |", "|---|---|---|---|"]
    for item in created:
        position = f"{_mm(item.position[0])}, {_mm(item.position[1])}" if item.position else ""
        lines.append(f"| {item.kind} | {cell(item.layer)} | {position} | {item.id} |")
    return lines


def _size(value: float | None, name: str, netclass_value: int | None, netclass: str):
    """The size of an argument, or of the netclass: (nm, source)."""
    if value is not None:
        return _nm(value, name), f"the argument {name}"
    if netclass_value is None:
        raise InputError(f"The netclass {netclass} gives no {name}: give {name}.")
    return netclass_value, f"the netclass {netclass}"


def _add_tracks(
    board_path: str,
    net: str,
    tracks: list[TrackSegment],
    vias: list[Point],
    width_mm: float | None,
    via_size_mm: float | None,
    via_drill_mm: float | None,
) -> str:
    if not tracks and not vias:
        raise InputError("tracks and vias are empty.")
    for track in tracks:
        if _xy(track.start) == _xy(track.end):
            raise InputError(
                f"A track has the same start and end ({track.start.x_mm:g}, "
                f"{track.start.y_mm:g}). The tool changed nothing."
            )
    with open_session() as session:
        board = session.board(board_path)
        if vias and session.state.rules.has_limit("free_via"):
            # A via on a track end is not free, as the PCB editor makes it.
            ends = {_xy(point) for track in tracks for point in (track.start, track.end)}
            alone = [v for v in vias if _xy(v) not in ends]
            if alone:
                raise InputError(
                    f"{_count(len(alone), 'via')} not at an end of a new track (first: "
                    f"{alone[0].x_mm:g}, {alone[0].y_mm:g}): KiCad "
                    f"{session.state.version} cannot add a free via. "
                    f"{LIMITS['free_via'].text} Tell the user: add such a via in the PCB "
                    "editor. The tool changed nothing."
                )
        _check_net(session, board, net)
        if tracks:
            _check_layers(session, board, [track.layer for track in tracks])
        # The netclass for a size that the arguments do not give, and for a via (its via
        # stack gives the form of the pad stack of a through via).
        sizes = (
            session.netclass(net)
            if vias or width_mm is None
            else NetClassSizes("", None, None, None, None)
        )
        lines_sizes = []
        items = []
        if tracks:
            width, source = _size(width_mm, "width_mm", sizes.track_width, sizes.name)
            lines_sizes.append(f"Track width: {_mm(width)} mm ({source}).")
            items += [
                session.track_message(_xy(t.start), _xy(t.end), width, t.layer, net) for t in tracks
            ]
        if vias:
            size, size_source = _size(via_size_mm, "via_size_mm", sizes.via_size, sizes.name)
            drill, drill_source = _size(via_drill_mm, "via_drill_mm", sizes.via_drill, sizes.name)
            if sizes.via_stack is None or not sizes.via_stack.copper_layers:
                raise InputError(
                    f"The netclass {sizes.name} gives no via stack with a copper layer: the "
                    "tool cannot make a via."
                )
            if drill >= size:
                raise InputError(
                    f"The via drill ({_mm(drill)} mm) must be smaller than the via size "
                    f"({_mm(size)} mm). The tool changed nothing."
                )
            lines_sizes.append(
                f"Via: through, size {_mm(size)} mm ({size_source}), drill {_mm(drill)} mm "
                f"({drill_source})."
            )
            items += [session.via_message(_xy(v), size, drill, sizes.via_stack, net) for v in vias]
        message = f"{UNDO_PREFIX}tracks and vias on {net}"
        created = session.commit_items(board, items, message)
    return "\n".join(
        [
            f"✅ KiCad added {_count(len(tracks), 'track')} and {_count(len(vias), 'via')} on "
            f'the net `{net}` in one undo step ("{message}").',
            "",
            *lines_sizes,
            "",
            *_created_table(created),
            "",
            DRC_NOTE,
        ]
    )


@mcp.tool()
async def ipc_add_tracks(
    board_path: str,
    net: str,
    tracks: list[TrackSegment] | None = None,
    vias: list[Point] | None = None,
    width_mm: float | None = None,
    via_size_mm: float | None = None,
    via_drill_mm: float | None = None,
) -> str:
    """Add straight tracks and through vias on one net of the board in the PCB editor.

    KiCad adds the items in one undo step. The sizes come from the netclass of the net,
    or from the arguments (the router of the PCB editor uses the sizes that the user
    selected in the editor, which can be other sizes). KiCad checks no
    clearance: the answer tells the user to run DRC. The tool refuses a net that the
    board does not have. KiCad 10.0.6 must have the schematic editor closed (KiCad bug
    #24966).

    When the running KiCad has the limit free_via (ipc_status), each via must be at an
    end of a new track of the same call: an other via gives an error. A track with the
    same start and end, and a via drill that is not smaller than the via size, give
    an error. After an error, the tool changed nothing.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.
        net: The name of the net, for example "GND" or "/SDA" (case-sensitive).
        tracks: The tracks: start, end (mm) and copper layer of each one.
        vias: The positions of the through vias (mm).
        width_mm: The track width. Default: the track width of the netclass.
        via_size_mm: The via diameter. Default: the via size of the netclass.
        via_drill_mm: The via drill. Default: the via drill of the netclass.

    Returns:
        Markdown: the result, the sizes and their source, a table of the new items with
        their IDs, and the DRC warning.
    """
    try:
        return await asyncio.to_thread(
            _add_tracks,
            board_path,
            net,
            tracks or [],
            vias or [],
            width_mm,
            via_size_mm,
            via_drill_mm,
        )
    except Exception as error:
        return error_answer(error, "Error adding the tracks")


def _add_zone(board_path: str, net: str, layer: str, outline: list[Point], **settings) -> str:
    points = _polygon(outline)
    if not 0 <= settings["priority"] <= UINT32_MAX:
        raise InputError(f"priority must be 0 to {UINT32_MAX}, not {settings['priority']}.")
    # The checks of the zone dialog (PANEL_ZONE_PROPERTIES::AcceptOptions, zones.h of
    # KiCad 10.0.6).
    min_thickness = _nm(
        settings["min_thickness_mm"], "min_thickness_mm", minimum=ZONE_THICKNESS_MIN_MM
    )
    spoke = _nm(settings["thermal_spoke_width_mm"], "thermal_spoke_width_mm", minimum=0)
    if spoke < min_thickness:
        raise InputError("Thermal spoke width cannot be smaller than the minimum width.")
    zone = ZoneSettings(
        # The clearance of a zone is a local clearance: DRC_ENGINE::EvalRules uses the
        # larger of it and the clearance of the netclass and the rules ("local clearances
        # (such as zones) ... have to be max'ed with netclass values"). No clearance: 0,
        # thus the current clearance of the netclass and the rules applies.
        clearance=0
        if settings["clearance_mm"] is None
        else _nm(
            settings["clearance_mm"], "clearance_mm", minimum=0, maximum=ZONE_CLEARANCE_MAX_MM
        ),
        min_thickness=min_thickness,
        connection=settings["connection"],
        # The zone dialog does not check the gap (AcceptOptions validates no
        # m_antipadClearance): only a value that KiCad can store.
        thermal_gap=_storable(
            settings["thermal_gap_mm"],
            f"thermal_gap_mm is not a length that KiCad can store: {settings['thermal_gap_mm']}.",
        ),
        thermal_spoke_width=spoke,
        island_removal=settings["island_removal"],
        priority=settings["priority"],
        border_style=settings["border_style"],
        border_pitch=_nm(
            settings["border_pitch_mm"],
            "border_pitch_mm",
            minimum=HATCH_PITCH_MIN_MM,
            maximum=HATCH_PITCH_MAX_MM,
        ),
        name=settings["name"],
    )
    with open_session() as session:
        board = session.board(board_path)
        _check_net(session, board, net)
        _check_layers(session, board, [layer])
        if zone.name and zone.name in session.zone_names(board):
            # The zone dialog makes the name unique (BOARD::GetUniqueZoneName).
            raise InputError(
                f"The board has a zone with the name {zone.name!r}. Give an other name. The "
                "tool changed nothing."
            )
        message = f"{UNDO_PREFIX}zone on {net}"
        created = session.commit_items(
            board,
            [session.zone_message(layer, points, net, zone)],
            message,
        )
    rows = [
        ("Net, layer", f"{net}, {layer}"),
        (
            "Clearance",
            f"{_mm(zone.clearance)} mm of the zone: KiCad uses the larger of it and the "
            "current clearance of the netclass and the rules"
            if zone.clearance
            else "none of the zone (0): KiCad uses the current clearance of the netclass and "
            "the rules",
        ),
        ("Minimum width", f"{_mm(zone.min_thickness)} mm"),
        ("Pad connection", zone.connection),
        ("Thermal gap", f"{_mm(zone.thermal_gap)} mm"),
        ("Thermal spoke width", f"{_mm(zone.thermal_spoke_width)} mm"),
        ("Fill", "solid"),
        ("Island removal", zone.island_removal),
        ("Priority", str(zone.priority)),
        ("Border", f"{zone.border_style}, pitch {_mm(zone.border_pitch)} mm"),
    ]
    return "\n".join(
        [
            f'✅ KiCad added a copper zone on the net `{net}` in one undo step ("{message}").',
            "",
            "| Setting | Value |",
            "|---|---|",
            *(f"| {name} | {cell(value)} |" for name, value in rows),
            "",
            *_created_table(created),
            "",
            "ℹ️ The zone has no fill yet. Fill the zones with ipc_refill_zones.",
            DRC_NOTE,
        ]
    )


@mcp.tool()
async def ipc_add_zone(
    board_path: str,
    net: str,
    layer: str,
    outline: list[Point],
    min_thickness_mm: float,
    connection: Literal["thermal", "solid", "none", "thermal_pth"],
    island_removal: Literal["always", "never"],
    priority: int,
    border_style: Literal["solid", "hatched_edge", "hatched_full"],
    border_pitch_mm: float,
    thermal_gap_mm: float,
    thermal_spoke_width_mm: float,
    clearance_mm: float | None = None,
    name: str = "",
) -> str:
    """Add a copper zone with a solid fill on one net and one copper layer of the board in
    the PCB editor.

    The API gives no default zone settings: the settings are required arguments. When
    the user did not give them, ask the user. KiCad adds the zone in one undo step, with
    no fill: fill it with ipc_refill_zones. KiCad checks no clearance: the answer tells
    the user to run DRC. KiCad 10.0.6 must have the schematic editor closed (KiCad bug
    #24966). An outline that crosses itself, has the same point two times one after the
    other, or has no area gives an error.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.
        net: The name of the net, for example "GND" (case-sensitive).
        layer: A copper layer of the board, for example F.Cu.
        outline: The corners of the zone outline in mm (3 or more points, the first point
            not again at the end).
        min_thickness_mm: The minimum width of the fill, 0.025 mm or more.
        connection: The connection of pads: "thermal" (thermal reliefs), "solid",
            "none", or "thermal_pth" (thermal reliefs for through-hole pads only).
        island_removal: Remove isolated islands of the fill: "always" or "never". (The
            mode with a minimum area is not possible: the API gives no unit for it.)
        priority: The zone priority (0 or more).
        border_style: The border of the zone in the editor: "solid", "hatched_edge" or
            "hatched_full".
        border_pitch_mm: The pitch of the border hatch, 0.1 to 2 mm.
        thermal_gap_mm: The thermal relief gap (KiCad keeps it also for a solid
            connection: a pad can have its own thermal connection).
        thermal_spoke_width_mm: The thermal spoke width, not smaller than
            min_thickness_mm.
        clearance_mm: An own clearance of the zone, 0 to 100 mm: KiCad uses the larger
            of it and the current clearance of the netclass and the rules. Default:
            none (0), thus only the netclass and the rules apply.
        name: The name of the zone (optional, not the name of an other zone).

    Returns:
        Markdown: the result, the settings, the new zone with its ID, and the DRC
        warning.
    """
    try:
        return await asyncio.to_thread(
            _add_zone,
            board_path,
            net,
            layer,
            outline,
            min_thickness_mm=min_thickness_mm,
            connection=connection,
            island_removal=island_removal,
            priority=priority,
            border_style=border_style,
            border_pitch_mm=border_pitch_mm,
            clearance_mm=clearance_mm,
            thermal_gap_mm=thermal_gap_mm,
            thermal_spoke_width_mm=thermal_spoke_width_mm,
            name=name,
        )
    except Exception as error:
        return error_answer(error, "Error adding the zone")


def _add_board_outline(board_path: str, outline: list[Point], line_width_mm: float | None) -> str:
    points = _polygon(outline)
    with open_session() as session:
        board = session.board(board_path)
        existing, unreadable = session.edge_cuts_shapes(board)
        if unreadable:
            raise IpcError(
                f"KiCad gave {_count(unreadable, 'shape')} of the board that the tool cannot "
                "read: the tool cannot tell if they are on Edge.Cuts. The tool changed "
                "nothing."
            )
        if existing:
            # The tools delete no items (limit delete_results of KiCad 10.0.6).
            raise InputError(
                f"The board has {_count(len(existing), 'shape')} on Edge.Cuts. The tool adds "
                "an outline only to a board with none. Tell the user: delete the Edge.Cuts "
                "shapes in the PCB editor, then call the tool again. The tool changed nothing."
            )
        if line_width_mm is not None:
            width, source = _nm(line_width_mm, "line_width_mm"), "the argument line_width_mm"
        else:
            width = session.edge_line_width(board)
            if width is None:
                raise InputError("KiCad gave no line width for Edge.Cuts: give line_width_mm.")
            source = "the graphics defaults of the board for Edge.Cuts"
        message = f"{UNDO_PREFIX}board outline"
        created = session.commit_items(board, [session.outline_message(points, width)], message)
    return "\n".join(
        [
            f"✅ KiCad added the board outline (a polygon of {len(outline)} corners) on "
            f'Edge.Cuts in one undo step ("{message}").',
            "",
            f"Line width: {_mm(width)} mm ({source}).",
            "",
            *_created_table(created),
            "",
            DRC_NOTE,
        ]
    )


@mcp.tool()
async def ipc_add_board_outline(
    board_path: str, outline: list[Point], line_width_mm: float | None = None
) -> str:
    """Add the board outline: one closed polygon on Edge.Cuts of the board in the PCB
    editor, when the board has no Edge.Cuts shapes.

    KiCad adds the outline in one undo step. A board that has Edge.Cuts shapes gives an
    error: the tools delete no items (KiCad 10.0.6 gives no result of a delete, limit
    delete_results); the user deletes the old outline in the PCB editor. Footprints can
    have Edge.Cuts shapes too: the tool does not count them. KiCad 10.0.6 must have the
    schematic editor closed (KiCad bug #24966). An outline that crosses itself, has the
    same point two times one after the other, or has no area gives an error.

    Args:
        board_path: The path of the .kicad_pcb file that is open in the PCB editor.
        outline: The corners of the outline in mm (3 or more points, the first point not
            again at the end).
        line_width_mm: The line width. Default: the graphics defaults of the board for
            Edge.Cuts.

    Returns:
        Markdown: the result, the line width and its source, the new shape with its ID,
        and the DRC warning.
    """
    try:
        return await asyncio.to_thread(_add_board_outline, board_path, outline, line_width_mm)
    except Exception as error:
        return error_answer(error, "Error adding the board outline")
