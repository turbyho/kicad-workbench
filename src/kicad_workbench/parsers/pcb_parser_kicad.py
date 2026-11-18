"""PCB file parser using KiCad Python API (pcbnew)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import WorkbenchError
from ..utils.file_handlers import validate_kicad_file
from ..utils.kicad_text import KiCadEncodingError, read_as_kicad
from .kicad_versions import header_version, kicad_release
from .pcb_items import PCBTrack, PCBVia, PCBZone


class BoardLoadError(WorkbenchError):
    """pcbnew cannot read the board."""


def _no_board_reason(path, kicad: str) -> str:
    """The text for a board that pcbnew of KiCad 9 or 10 does not load: LoadBoard
    then gives None and no reason. A file version that is newer than the board
    version of the KiCad release is a known reason."""
    prefix = f"pcbnew of KiCad {kicad} cannot read {path}"
    try:
        version = header_version(read_as_kicad(path), "kicad_pcb")
    except (OSError, KiCadEncodingError):
        version = None
    release = kicad_release(kicad)
    if version is not None and release is not None and version > release.board:
        return (
            f"{prefix}: its file version {version} is newer than the version that KiCad "
            f"{kicad} writes ({release.board}). Open it with a newer KiCad."
        )
    return (
        f"{prefix}, and pcbnew gives no reason. Open the board in the PCB editor of KiCad "
        "to see the error."
    )


@dataclass
class PCBFootprint:
    """Footprint from PCB file."""

    reference: str
    value: str
    footprint_id: str
    position: tuple[float, float]
    rotation: float
    pad_count: int
    layer: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class PCBNet:
    """Net from PCB file."""

    name: str
    code: int
    node_count: int = 0


class PCBParserKiCad:
    """PCB file parser using KiCad's official Python API (pcbnew)."""

    def __init__(self, file_path: str) -> None:
        self.file_path = validate_kicad_file(file_path, ".kicad_pcb")

        # tools/pcb.py uses this reader only when it can import pcbnew.
        import pcbnew

        kicad = pcbnew.GetMajorMinorPatchVersion()
        try:
            board = pcbnew.LoadBoard(str(self.file_path))
        except OSError as e:  # KiCad 8: the IO_ERROR of the board reader
            raise BoardLoadError(
                f"pcbnew of KiCad {kicad} cannot read {self.file_path}: {e}"
            ) from e
        if board is None:  # KiCad 9 and 10
            raise BoardLoadError(_no_board_reason(self.file_path, kicad))
        self.board = board
        self._pcbnew = pcbnew

    def _to_mm(self, iu_value: int | float) -> float:
        """Convert KiCad internal units (nm) to mm."""
        return float(iu_value) / 1e6

    def _pos_mm(self, pos) -> tuple[float, float]:
        """Convert KiCad position to (x_mm, y_mm)."""
        return (pos.x / 1e6, pos.y / 1e6)

    def _tracks(self) -> list:
        """Return the tracks, arcs and vias of the board.

        BOARD.GetTracks() iterates with it.next() in KiCad 10.0.6 and earlier.
        SWIG 4.4 and later do not have this method, thus the iteration fails
        (KiCad issue 25423, fixed in KiCad 10.0.7). Access by index works with
        each SWIG version. Cast() gives the type of each item (PCB_TRACK,
        PCB_ARC, PCB_VIA), as the iterator of KiCad does.
        """
        tracks = self.board.Tracks()
        return [tracks[i].Cast() for i in range(len(tracks))]

    def _layer_name(self, layer: int) -> str:
        """The standard name of a copper layer (F.Cu, In1.Cu, B.Cu), as the board file
        and the text reader give it (user decision, 2026-11-18): not the user name of
        the layer table that BOARD::GetLayerName gives ("Top"). The tracks, the vias
        and the footprints are on copper layers. BOARD::GetStandardLayerName gives
        LayerName, which does not translate a copper layer (KiCad 8, 9 and 10).
        LSET.Name of KiCad 8.0.9 gives a wxChar object in Python, not a text."""
        return self._pcbnew.BOARD.GetStandardLayerName(layer)

    # ── Footprints ──────────────────────────────────────────

    def get_footprints(self) -> list[PCBFootprint]:
        footprints = []
        for fp in self.board.GetFootprints():
            pos = fp.GetPosition()
            properties: dict[str, str] = {}
            for key in ("Reference", "Value", "Footprint", "Datasheet"):
                try:
                    value = fp.GetProperty(key)
                    if value:
                        properties[key] = value
                except Exception:
                    pass

            footprints.append(
                PCBFootprint(
                    reference=fp.GetReference(),
                    value=fp.GetValue(),
                    # The text of the board file: LIB_ID::Format.
                    footprint_id=fp.GetFPIDAsString(),
                    position=self._pos_mm(pos),
                    rotation=fp.GetOrientation().AsDegrees(),
                    pad_count=fp.GetPadCount(),  # all pads, also NPTH
                    layer=self._layer_name(fp.GetLayer()),
                    properties=properties,
                )
            )
        return footprints

    def get_footprint_by_reference(self, reference: str) -> PCBFootprint | None:
        for footprint in self.get_footprints():
            if footprint.reference == reference:
                return footprint
        return None

    # ── Nets ────────────────────────────────────────────────

    def get_nets(self) -> list[PCBNet]:
        nets = []
        net_info = self.board.GetNetInfo()
        for _name, net in net_info.NetsByName().items():
            name = str(_name)
            if name:
                nets.append(PCBNet(name=name, code=net.GetNetCode()))
        return nets

    # ── Tracks ──────────────────────────────────────────────

    def get_tracks(self) -> list[PCBTrack]:
        """Get all track segments (excludes vias)."""
        pcbnew = self._pcbnew
        tracks = []
        for track in self._tracks():
            if isinstance(track, pcbnew.PCB_VIA):
                continue
            tracks.append(PCBTrack(
                start=self._pos_mm(track.GetStart()),
                end=self._pos_mm(track.GetEnd()),
                width=self._to_mm(track.GetWidth()),
                layer=self._layer_name(track.GetLayer()),
                net=track.GetNetname(),
                length=self._to_mm(track.GetLength()),
            ))
        return tracks

    # ── Vias ────────────────────────────────────────────────

    def get_vias(self) -> list[PCBVia]:
        """Get all vias (uses GetWidth(layer) to avoid headless assert)."""
        pcbnew = self._pcbnew
        vias = []
        for track in self._tracks():
            if not isinstance(track, pcbnew.PCB_VIA):
                continue
            top_layer = track.TopLayer()
            size_mm = self._to_mm(track.GetWidth(top_layer))
            vias.append(PCBVia(
                position=self._pos_mm(track.GetStart()),
                size=size_mm,
                drill=self._to_mm(track.GetDrill()),
                top_layer=self._layer_name(top_layer),
                bottom_layer=self._layer_name(track.BottomLayer()),
                net=track.GetNetname(),
            ))
        return vias

    # ── Zones ───────────────────────────────────────────────

    def get_zones(self) -> list[PCBZone]:
        """The copper zones of the board, as the Zone Manager of KiCad selects them
        (ZONE_SETTINGS_BAG): no rule area, no teardrop, and only a zone on a copper layer.

        The layers of a zone are the copper layers of its layer set that the board
        enables (PCB_IO_KICAD_SEXPR::format(ZONE) saves these), from the top, with their
        standard names. A zone keeps its layers in its layer set (ZONE::SetLayerSet):
        GetLayerName() reads the layer of BOARD_ITEM, which a zone does not set, thus
        it gives F.Cu for each zone.
        """
        zones = []
        for zone in self._copper_zones():
            zones.append(PCBZone(
                net_name=zone.GetNetname(),
                layers=[
                    self._layer_name(layer)
                    for layer in zone.GetLayerSet().CuStack()
                    if self.board.IsLayerEnabled(layer)
                ],
                filled=zone.IsFilled(),
            ))
        return zones

    def _copper_zones(self) -> list:
        """The ZONE items of the copper zones (ZONE_SETTINGS_BAG, see get_zones)."""
        return [
            zone
            for zone in self.board.Zones()
            if not zone.GetIsRuleArea() and not zone.IsTeardropArea() and zone.IsOnCopperLayer()
        ]

    # ── Board Info ──────────────────────────────────────────

    def get_board_info(self) -> dict[str, Any]:
        title_block = self.board.GetTitleBlock()
        bbox = self.board.ComputeBoundingBox(True)
        size = bbox.GetSize()

        return {
            "title": title_block.GetTitle(),
            "date": title_block.GetDate(),
            "revision": title_block.GetRevision(),
            "company": title_block.GetCompany(),
            "file_path": str(self.file_path),
            "board_width_mm": self._to_mm(size.x),
            "board_height_mm": self._to_mm(size.y),
            "footprints_count": len(self.board.GetFootprints()),
            "tracks_count": sum(
                1 for t in self._tracks()
                if not isinstance(t, self._pcbnew.PCB_VIA)
            ),
            "vias_count": sum(
                1 for t in self._tracks()
                if isinstance(t, self._pcbnew.PCB_VIA)
            ),
            "nets_count": self.board.GetNetCount(),
            "zones_count": len(self._copper_zones()),
        }

    # ── Design Rules ────────────────────────────────────────

    def get_design_rules(self) -> dict[str, Any]:
        ds = self.board.GetDesignSettings()
        pcbnew = self._pcbnew
        return {
            "board_thickness_mm": pcbnew.ToMM(ds.GetBoardThickness()),
            "copper_layers": ds.GetCopperLayerCount(),
            "smallest_clearance_mm": pcbnew.ToMM(ds.GetSmallestClearanceValue()),
            "biggest_clearance_mm": pcbnew.ToMM(ds.GetBiggestClearanceValue()),
            "current_track_width_mm": pcbnew.ToMM(ds.GetCurrentTrackWidth()),
            "current_via_drill_mm": pcbnew.ToMM(ds.GetCurrentViaDrill()),
            "current_via_size_mm": pcbnew.ToMM(ds.GetCurrentViaSize()),
            "current_diff_pair_width_mm": pcbnew.ToMM(ds.GetCurrentDiffPairWidth()),
            "current_diff_pair_gap_mm": pcbnew.ToMM(ds.GetCurrentDiffPairGap()),
            "drc_epsilon_mm": pcbnew.ToMM(ds.GetDRCEpsilon()),
            "hole_plating_mm": pcbnew.ToMM(ds.GetHolePlatingThickness()),
        }

    # ── Aggregate Stats ─────────────────────────────────────

    def get_net_track_stats(self) -> dict[str, dict[str, Any]]:
        """Per-net track statistics: total_length, segment_count, widths, layers."""
        stats: dict[str, dict[str, Any]] = {}
        for track in self.get_tracks():
            net = track.net or "(unnamed)"
            if net not in stats:
                stats[net] = {
                    "total_length": 0.0,
                    "segment_count": 0,
                    "widths": [],
                    "layers": set(),
                }
            s = stats[net]
            s["total_length"] += track.length
            s["segment_count"] += 1
            s["widths"].append(track.width)
            s["layers"].add(track.layer)
        # Convert sets to sorted lists for JSON
        for s in stats.values():
            s["layers"] = sorted(s["layers"])
        return stats

    def get_via_stats(self) -> dict[str, Any]:
        """Aggregate via statistics."""
        vias = self.get_vias()
        if not vias:
            return {"count": 0}

        drill_counts: dict[str, int] = {}
        layer_span_counts: dict[str, int] = {}
        net_via_counts: dict[str, int] = {}

        for v in vias:
            drill_key = f"{v.drill:.3f}mm"
            drill_counts[drill_key] = drill_counts.get(drill_key, 0) + 1

            span = f"{v.top_layer} -> {v.bottom_layer}"
            layer_span_counts[span] = layer_span_counts.get(span, 0) + 1

            net = v.net or "(unnamed)"
            net_via_counts[net] = net_via_counts.get(net, 0) + 1

        return {
            "count": len(vias),
            "drill_sizes": dict(sorted(drill_counts.items(), key=lambda x: -x[1])),
            "layer_spans": layer_span_counts,
            "top_via_nets": dict(sorted(net_via_counts.items(), key=lambda x: -x[1])[:20]),
        }
