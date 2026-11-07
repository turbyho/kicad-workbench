# Schematic edits

KiCad has no Python interface for schematics in KiCad 8, 9 and 10. Thus the edit tools
write the S-expression file of the schematic themselves, with the rules of the KiCad
source code for the file version (the KiCad version that saved the file). They write
the file in that format. Use the schematic editor of KiCad for the design work, and
the edit tools for well-defined changes: a symbol from a library, a wire, a label,
the fields of a symbol.

## Files that KiCad has open

KiCad does not read a change to a file that it has open, and its next save removes
the change. Thus each edit tool refuses the write when KiCad has the file open: KiCad
writes the lock file `~<file name>.lck` next to it. Close the schematic editor or the
PCB editor before the edit, and open the file again after the edit.

- After a crash of KiCad, the lock file stays and the tools refuse the write. Open
  the file in KiCad and close it again, or delete the lock file.
- The schematic editor locks only the root schematic. To find the root schematic of a
  sub-sheet, the tools read the sheet hierarchy of the projects in the folder of the
  sub-sheet and in its parent folders. They cannot find the root schematic of a
  sub-sheet outside the folder tree of its project (for example
  `../common/power.kicad_sch`). Thus the tools do not see when KiCad has such a
  sub-sheet open.

## Symbols from a library

`add_component_from_library` finds the library in the library tables of the installed
KiCad and of the project, as the symbol chooser of KiCad: a library of the project
table replaces a global library with the same name. The tool gets the KiCad version
from kicad-cli, to find the global library table.

- The tool copies the symbol into the schematic. A derived symbol gets the units, the
  pins and the drawings of its parents, as in KiCad (`LIB_SYMBOL::Flatten`). When the
  schematic has the symbol already, the tool uses that copy.
- The placed symbol gets the fields of the library symbol at their library
  positions, the flags `in_bom`, `on_board`, `exclude_from_sim` and `in_pos_files` of
  the library symbol, and the reference of its sheet path, as KiCad places a symbol.
- The tool does not move the fields (KiCad: autoplace).
- A sheet file that the hierarchy uses more than one time needs `sheet_path`: the
  sheet that gets the reference.

## The connection grid

The connection grid is `schematic.connection_grid_size` of the project file (default
50 mils, 1.27 mm). ERC finds a pin or a wire end off this grid (`endpoint_off_grid`).
`add_component_from_library`, `add_wire`, `add_label`, `add_global_label` and
`add_hierarchical_label` have the argument `snap_to_grid` (default `True`):

- `True`: the tool moves a symbol to the nearest position that puts its pins on the
  grid. It moves each wire point and each label to the nearest grid point. A point on
  a connection point of the sheet does not move, also when it is off the grid: a pin,
  a wire end, a bus entry end, a junction, a no-connect flag, a label or a sheet pin.
  For a label, also a point on a wire or a bus. Thus a new wire or label connects to
  an item off the grid. A horizontal or vertical wire segment stays horizontal or
  vertical.
- `False`: the tool uses the positions as they are.

The output gives the position that the tool writes, and each pin or wire end off the
grid. It does not tell about these points when the project ignores the ERC check.

## Wires and junctions

`add_wire` and `add_component_from_library` add junctions as the schematic editor of
KiCad does, with the rules of the file version:

- A junction goes where three or more wire directions meet, for example a wire end
  or a pin in the middle of a wire. The wire there splits into two wires.
- Before that, the tool merges the segments of a new wire that are on one straight
  line, and merges a new wire with a wire on the same straight line that it touches
  or overlaps.
- The tool removes the part of a wire between two pins of a symbol (KiCad:
  `TrimWire`).
- A KiCad wire has two points. `add_wire` adds one wire for each segment.

The tool refuses the change in these cases:

- The rule of KiCad uses data that the file does not give (the box of a symbol with
  a hidden pin, the pen width of a bus entry from its net class), and the result
  depends on it.
- Old wires of the sheet that the schematic editor would change first (two wires on
  one straight line that `CleanUp` merges, a wire through two pins that `TrimWire`
  cuts) are at a point of the decision. The tool does these steps of `CleanUp` and
  `TrimWire` only for the new items.

A pin or a wire end does not connect in the middle of a wire: KiCad needs a junction
there. Make sure that a new connection is in the netlist: `generate_netlist` and
`trace_netlist_connection` show it.

## Fields of placed symbols

`set_symbol_fields` writes fields of placed symbols, as KiCad writes them:

- The tool writes each field into each unit of the reference: one package has one
  `Value` and one `MPN`.
- A field that the symbol does not have becomes a new field, hidden as KiCad 9 and 10
  make it. In a file of KiCad 8 a new field is visible. `Value_Confirmed` and
  `MPN_Confirmed` are always hidden.
- With a variant (KiCad 10), the tool writes the fields of that variant into each
  unit. A value equal to the field of the symbol removes the field from the variant.
- The tool checks all changes and all files first, and writes no file when a check
  fails.
- A symbol in a sheet that the hierarchy uses more than one time has one set of
  fields for all its references (`U201` and `U301`). Give the same fields for each
  of these references, or write different fields through a variant. Else the tool
  refuses the change.

## Projects and boards

- `create_kicad_project` makes a project as the project manager of the installed
  KiCad makes it, with the rules of its release (8.0.0 to 10.0.7, from the KiCad
  source). See "New projects" below.
- `setup_pcb_layout` makes a new board with an outline, through `pcbnew` of the
  installed KiCad. The board has the file format of the installed KiCad. When that is
  newer than the KiCad of the schematic, the output has a warning: the older KiCad
  cannot open the board. The tool does not replace an existing board without
  `overwrite=True`: a new board removes all footprints, tracks and zones.

## New projects

`create_kicad_project` makes the files that KiCad makes. With no template, they are
the files of "New Project":

- KiCad 8 and 9: a copy of the project file `kicad.kicad_pro` of KiCad (the first one
  in `KICAD_USER_TEMPLATE_DIR`, the user templates folder, `KICAD<version>_TEMPLATE_DIR`
  and the data folder of KiCad), and an empty schematic and board as KiCad writes them.
- KiCad 10: the files of the template "default" in the user templates folder (from
  10.0.5; before it, in `KICAD_USER_TEMPLATE_DIR`), then the empty schematic and board.
  KiCad makes this template when it is not there, and the tool does the same.

With a template (`template="Arduino_Uno"`, or the path of a template folder), they
are the files of "New Project from Template":

- The files of the template, with the project name in the place of the template name.
  A drawing sheet (`.kicad_wks`), a legacy symbol library (`.lib`, `.dcm`) and the files
  of a footprint library (`.pretty`) keep their names, as in KiCad. KiCad 10 also
  renames the folders with the template name.
- KiCad does not copy the folder `meta`, the folders that end with `-backups`, hidden
  files, `fp-info-cache` and lock files. KiCad 8 and 9 also skip the autosave files.
  KiCad 10 skips the items that the `.gitignore` of their folder names.
- Then the files that are missing: the project file, the schematic and the board.
- KiCad does not change the files of the template: the UUIDs, the project file and
  the schematic stay as they are.

The tool then writes the title, the company and the date into the title block of the
root schematic that it wrote. KiCad does not do this.

Before KiCad uses a folder that is not empty (no template), or replaces files of the
template, it asks the user. The tool then gives an error with the files. Ask the user,
then call the tool again with `overwrite=True`. With no template, the files of the
folder stay, as in KiCad. With a template, the files of the template replace them.
The tool refuses to replace the files of a project, a schematic or a board that KiCad
has open.

When KiCad cannot make an item, the tool does what KiCad does: a folder that KiCad
cannot make is skipped with the items in it; a file of a template that KiCad cannot
copy stops the project after the other copies (no project file, schematic or board
is added); a `kicad.kicad_pro` that KiCad cannot copy gives the empty project file.
The output gives each of these events. KiCad 10 makes the template "default" first;
when it cannot, KiCad 10.0.0 to 10.0.4 make only an empty project with no template,
and KiCad 10.0.5 and later offer only the other templates.

A template is a name on the template pages of KiCad (KiCad 10.0.1 and later: only the
folders with a `meta` folder), or the path of a folder with a `meta` folder or a
project file.

Two points differ from KiCad on purpose:

- KiCad 10 replaces the template name in the full path of a folder, also in the
  folders above the project folder. Thus KiCad can write the files outside the
  project folder. The tool renames only the folders in the project folder.
- KiCad shows the files that it replaces with names that do not agree with the copy in
  some cases (`GetDestinationFiles`). The tool shows the files that the copy replaces.

A release with no rules (KiCad 7, a development build, a release after 10.0.7) and a
template with only a legacy project file (`.pro`) give an error.
