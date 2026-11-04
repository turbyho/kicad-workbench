"""Read the 3D models and the embedded files of the footprints of a board file.

PCB_IO_KICAD_SEXPR_PARSER of KiCad 8.0.9, 9.0.9 and 10.0.6:

- Each (footprint "<library ID>" ...) of the board is a footprint, with the
  reference of its (property "Reference" ...).
- Each (model "<file>" ...) of a footprint is an FP_3DMODEL with m_Filename
  "<file>" (parse3DModel). A bare token hide (older files) or (hide yes)
  hides the model, (hide no) shows it (parseMaybeAbsentBool).
- (embedded_files (file (name "<name>") ...)) of a footprint and of the board
  holds the embedded files (KiCad 9 and later).

The STEP exporter and the 3D viewer look for a "kicad-embed://" model in the
embedded files of the footprint, then of the board.
"""

from dataclasses import dataclass

from .lib_symbols import hidden
from .pcb_parser import _field
from .sexpr import Node, parse


@dataclass(frozen=True)
class BoardFootprint:
    reference: str
    lib_id: str  # The library ID of the footprint, for example "Resistor_SMD:R_0603".
    models: tuple[tuple[str, bool], ...]  # (m_Filename, m_Show)
    embedded_files: tuple[str, ...]


def embedded_files(node: Node) -> tuple[tuple[str, str], ...]:
    """Return (name, type) of each file of the (embedded_files ...) of a node."""
    files = node.first("embedded_files")
    if files is None:
        return ()
    result = []
    for file in files.children("file"):
        name, kind = file.first("name"), file.first("type")
        result.append((name.value(0, "") if name else "", kind.value(0, "") if kind else ""))
    return tuple(result)


def _names(node: Node) -> tuple[str, ...]:
    return tuple(name for name, _kind in embedded_files(node))


def board_models(text: str) -> tuple[list[BoardFootprint], tuple[str, ...]]:
    """Return the footprints of a board with their 3D models, and the embedded files of the board.

    Raises:
        SExprError: The text is not an S-expression.
    """
    root = parse(text)
    footprints = [
        BoardFootprint(
            reference=_field(footprint, "Reference"),
            lib_id=footprint.value(0, ""),
            models=tuple(
                (model.value(0, ""), not hidden(model)) for model in footprint.children("model")
            ),
            embedded_files=_names(footprint),
        )
        for footprint in root.children("footprint")
    ]
    return footprints, _names(root)
