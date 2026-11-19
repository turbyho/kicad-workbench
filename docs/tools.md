# Tools

The server has 75 tools. Each tool returns Markdown text (the image tools also return
PNG images). An error starts with "❌", a warning with "⚠️". An error that the tool
does not expect (a defect of the tool) also gives the traceback. The agent selects
the tools: this list tells you what the agent can do, and with which limits.

A tool call that takes longer than 20 s becomes a background job (an MCP client stops
a call after its timeout: Claude Desktop and the MCP SDK after 60 s). The answer starts
with "⏳" and gives the job ID; the work continues. `job_status` gives the result. See
"Background jobs" below.

An argument with `?` is optional. A path argument is the path of a file of the
project: `.kicad_sch` (schematic), `.kicad_pcb` (board), `.kicad_pro` (project), or a
netlist file of `generate_netlist`.

## Exact and heuristic tools

Most tools give the result of KiCad: they use kicad-cli or `pcbnew`, or the rules of
the KiCad source code for the installed KiCad. They read a file as the installed KiCad
loads it: the format follows the file version only where the parser of KiCad branches
on it. A schematic file, a board or a library of
an older version than the installed KiCad gives a warning with the step for the user,
and an edit tool refuses an older schematic (the README, "File versions"). The search
tools give no item of a library file that the installed KiCad cannot read (a newer
file). Some tools use heuristics: they find a component type from its value or
reference, a group of a net (power, interface, signal) from its name, or a
differential pair from the names of two nets. These tools are marked "heuristic"
below. Use their result as a start for a review, not as a fact.

## Schematics

| Tool | What it does |
|---|---|
| `get_schematic_info(file_path)` | The title block, the counts of components and nets, the components by reference prefix, and the sheets |
| `list_schematic_components(file_path, filter_type?, filter_value?, filter_dnp?)` | The components, with a filter by the start of the reference (for example "R" or "U"), by value or by DNP |
| `list_schematic_nets(file_path, filter_power?)` | The nets of the labels and the power symbols of one file. A net with no label of a sub-sheet needs the full hierarchy (the netlist tools). `filter_power` gives only the nets whose name has "gnd", "vcc", "vdd", "vss", "+" or "-" (heuristic) |
| `search_symbols(file_path, pattern, search_fields?)` | The symbols whose reference, value or library ID matches a regular expression (the table has at most 50 rows) |
| `get_symbol_details(file_path, reference)` | The fields and the pins of one component of the project. The fields are each field of the placed symbol as KiCad 10 loads it (also the user fields, for example MPN). The pins (number, name, electrical type) come from the netlist of kicad-cli. `trace_netlist_connection` gives the net of each pin |
| `search_components_by_type(file_path, component_type, search_in_subsheets?)` | Components of a type, for example "sensor" or "display" (heuristic: value, footprint and nets), in the file and in all sheets below it |

The component tools give each placed symbol of a file one time for each sheet path
of the file, with the reference and the unit that KiCad gives that path: a sheet
that the hierarchy uses two times gives two references, and a symbol with "U?" in
its Reference field has the reference of its instance. They need kicad-cli for the
version of KiCad, whose rules give the references.

## Sheet hierarchy

| Tool | What it does |
|---|---|
| `analyze_hierarchical_nets(file_path, filter_pattern?, show_hierarchy?)` | The nets of all sheets (the netlist of kicad-cli) by group, and the sheet paths. The groups power, interface and signal come from the net names (heuristic). The answer gives at most 20 power, 30 interface and 20 signal nets, each with its number of pins |
| `trace_hierarchical_connection(file_path, reference, pin_number?)` | The sheet and the connections of a component, at each level of the hierarchy |

## Netlist

| Tool | What it does |
|---|---|
| `generate_netlist(schematic_path)` | Makes the netlist of the schematic with kicad-cli, and gives its path |
| `trace_netlist_connection(netlist_path, reference, pin_number?)` | The nets of the pins of a component, and the pins on each net |
| `get_netlist_nets(netlist_path, filter_pattern?)` | The nets of the netlist with their pins (the first 10 pins of each net) |
| `get_netlist_components(netlist_path, filter_ref?)` | The components of the netlist with their nets |

The netlist of kicad-cli is the most exact source for the connections. Use it before
the schematic tools when the connection is important.

The netlist tools above read the file of `generate_netlist`: make it again after a
change of the schematic. The hierarchy tools, `search_components_by_type` and
`get_symbol_details` make the netlist with kicad-cli at each call. Thus they always
give the current state of the schematic, and they need kicad-cli. For a large
project, each call takes some seconds.

For a sub-sheet or an other top-level sheet, these tools and the ERC tools use the
schematic of the project name, as for the BOM. A KiCad 10 project with more than one
top-level sheet needs kicad-cli of KiCad 10.0.7 or later, which loads all top-level
sheets: kicad-cli 10.0.0 to 10.0.6 loads only one, thus these tools give an error, not
a result with some sheets.

The hierarchy tools and `search_components_by_type` get the components from the
netlist: each component
of each sheet path, at each level of the hierarchy, with the reference of that path
(a sheet that the hierarchy uses two times gives two references). The netlist has
no power symbols and no symbol with no library symbol.

## Boards

These tools import `pcbnew` in the server process. Without `pcbnew` (but with KiCad
installed), they read the board file as text, and each answer has the warning
"pcbnew not available" with the limits of the tool (see
[installation.md](installation.md), "Requirements"). With no KiCad, they give the
error "KiCad is not installed".
When `pcbnew` cannot read the board (for example a board of a newer KiCad), the
tools give an error: the text reader does not know the rules of such a board. The
text reader reads only the boards of KiCad 8 to 10; an older board needs `pcbnew`, or
a save in KiCad 8 or later.

| Tool | What it does |
|---|---|
| `list_pcb_footprints(file_path, filter_layer?)` | The footprints, optionally of one layer |
| `get_pcb_statistics(file_path)` | The size of the board, the layers, the counts, the design rules |
| `find_tracks_by_net(file_path, net_name)` | The tracks, vias and copper zones of one net, with their lengths, widths and layers |
| `analyze_pcb_nets(file_path)` | The distribution of track widths and vias, the nets by length, the layers |
| `analyze_pcb_signal_integrity(file_path, net_name?)` | Differential pairs and their length difference (heuristic: the pairs come from the net names, for example `USB_P` and `USB_N`), RF tracks, the longest signal nets |
| `analyze_pcb_power_integrity(file_path)` | The copper zones of the power and ground nets (count, all copper layers of each zone, filled state), the GND coverage of the copper layers, and the routing of the power nets (heuristic: the power and ground nets come from the net names) |

## Checks

| Tool | What it does |
|---|---|
| `run_erc(schematic_path)` | ERC of kicad-cli, with the severities of the project. It adds the annotation check of the ERC dialog (no annotation, a duplicate reference, units with different values), which kicad-cli does not run |
| `get_erc_violations(schematic_path, severity?)` | The ERC violations, with a filter by severity |
| `export_erc_report(schematic_path, output_path?)` | The ERC report of kicad-cli as a JSON file |
| `run_drc(pcb_path, refill_zones?)` | DRC of kicad-cli. When the schematic with the same name is next to the board, also the schematic parity check. By default the DRC checks the zone fills of the file; `refill_zones=True` fills the zones again in memory first, as the DRC dialog of KiCad does by default (KiCad 10) |
| `get_drc_violations(pcb_path, violation_type?, refill_zones?)` | The DRC violations, with a filter by type |
| `export_drc_report(pcb_path, output_path?, refill_zones?)` | The DRC report of kicad-cli as a JSON file |
| `check_schematic_pin_pad_mapping(schematic_path, references?)` | For each symbol: the pins with no pad, the pins with no number, the pads with no pin, and the symbols whose footprint KiCad does not find. It uses the netlist and the rules of "Update PCB from Schematic" |
| `check_pin_pad_mapping(symbol_lib_id, footprint_lib_id?, project_path?)` | The same check for one library symbol and one footprint |
| `check_library_links(project_path)` | The links of a project to its libraries: the library checks of ERC and DRC, the footprints of the schematic, and the 3D models (as the STEP export finds them) |
| `detect_pin_conflicts(schematic_path, severity?)` | The items of the ERC of kicad-cli about the connections of the symbol pins, with the pin map, the severities and the exclusions of the project: pin conflicts (`pin_to_pin`), inputs and power inputs with no driver, pins with no connection, labels on only one pin, pins of one number on different nets, and the pins of units that are not placed. It uses the last ERC report until a schematic file changes |

## Libraries

The library tools read the library tables of the installed KiCad and of the project,
as the choosers of KiCad: a library of the project table replaces a global library
with the same name. The first search reads all libraries (some seconds). Then an
index in the cache folder of the user is used until a library file changes.

| Tool | What it does |
|---|---|
| `list_libraries(kind?, project_path?)` | The symbol or footprint libraries, with the table, the state and the path |
| `search_library_symbols(query, project_path?, library?, limit?, include_hidden?)` | Symbols whose library ID, description, keywords or default footprint have each word of the query, in the order of the symbol chooser |
| `search_library_footprints(query, project_path?, library?, limit?, include_hidden?)` | Footprints whose library ID, description or tags have each word of the query |
| `get_library_symbol(lib_id, project_path?)` | The fields, units, body styles, footprint filters and pins (with alternate functions) of a symbol, as KiCad places it. A derived symbol gets the content of its parents |
| `get_library_footprint(lib_id, project_path?)` | The type, attributes, size, courtyard, fields, pads (with padstacks) and 3D models of a footprint, read by `pcbnew` |
| `render_library_item(lib_id, item_type, max_width_px, max_height_px, layout?, region?, pages?, project_path?, output_dir?, include_hidden?, max_images?)` | PNG images of a symbol (each unit and body style) or a footprint, drawn by kicad-cli. The agent gives the largest image that its vision input reads. Tiles keep the smallest text legible (at least 15 px, at least 300 DPI) |

### Project library tables

These tools change the project tables (`sym-lib-table`, `fp-lib-table` next to the
`.kicad_pro` file), as KiCad 10 does: "New Library" of the symbol editor and of the
footprint editor, and the table dialog. The global tables do not change. The tools
need an installed KiCad 10.0, and they refuse a project that KiCad has open: KiCad
reads the project tables only when it opens the project. They take only libraries of
the KiCad format. KiCad 10.0 cannot read a value with a quote or a backslash back from
the table, thus the tools refuse these characters in a new name, URI or description,
and keep the bytes of the other rows. With KiCad 10.0.7 and later, the old table goes
to `<table>.bak` first, as KiCad does.

| Tool | What it does |
|---|---|
| `create_project_library(project_path, kind, name, folder?)` | A new, empty library (a `.kicad_sym` file in the KiCad 10 format, or an empty `.pretty` folder) and its row in the project table. It does not replace a library that exists |
| `add_library_to_table(project_path, kind, library_path, nickname?, description?)` | Adds a KiCad library that exists to the project table. The URI gets a path variable of KiCad or `${KIPRJMOD}` when one holds the library. A library of an older file version gives a warning with the upgrade step, a newer one an error. A relative path is relative to the project folder |
| `remove_library_from_table(project_path, kind, nickname)` | Removes the row from the project table. The library files stay |
| `set_library_table_row(project_path, kind, nickname, enabled?, visible?)` | Enables or disables a row, and shows or hides a symbol library |
| `copy_symbol_to_library(project_path, lib_id, target_library, new_name?, flatten?, overwrite?)` | Copies a symbol into a library of the project table, as "Save As" of the symbol editor: a derived symbol goes with its parents (or `flatten=True`), the Value follows a new name when it was equal to the old name. A name that the target has needs `overwrite=True`. KiCad saves the library (kicad-cli), and a second save must give the same bytes |
| `copy_footprint_to_library(project_path, lib_id, target_library, new_name?, overwrite?)` | Copies a footprint into a library of the project table, as "Save As" of the footprint editor. A footprint that is not on F.Cu is refused |
| `set_library_symbol_properties(project_path, lib_id, fields?, keywords?, footprint_filters?)` | Changes the fields, the keywords and the footprint filters of a symbol of a project library, as the Symbol Properties dialog: a new field is hidden at (0, 0) with the text size of the symbol editor settings, the mandatory fields cannot be removed, a path to a file that exists gets "file://" (as the dialog). KiCad saves the library (kicad-cli) |
| `create_derived_symbol(project_path, parent_lib_id, name, keep_footprint?, keep_datasheet?, transfer_user_fields?, keep_user_field_content?)` | Makes a new symbol that extends a symbol of the same project library, as "Derive from existing symbol" of the symbol editor: copies of the mandatory fields (the Footprint and the Datasheet empty unless kept), the user fields with the transfer options of the dialog. A name that the library has is an error. KiCad saves the library (kicad-cli) |
| `rename_library_symbol(project_path, lib_id, new_name)` | Gives a symbol of a project library an other name, as "Rename" of the symbol editor: trim and escapes (a space stays), the Value follows when it was equal to the old name, the derived symbols extend the new name. A name that the library has (with no difference of case) is an error. A warning gives the placed symbols with the old lib_id: KiCad does not change them |
| `delete_library_symbol(project_path, lib_id, delete_derived?)` | Deletes a symbol of a project library, as "Delete" of the symbol editor. A symbol with derived symbols needs `delete_derived=True` (KiCad deletes them all). A warning gives the placed symbols with a deleted lib_id |
| `rename_library_footprint(project_path, lib_id, new_name)` | Gives a footprint of a project library an other name, as "Rename" of the footprint editor: a new file, the old file removed, the Value follows when it was equal to the old name. A name that the library has, or that the file name changes, is an error. A warning gives the Footprint fields (schematic, project symbol libraries) and the board footprints with the old library ID |
| `delete_library_footprint(project_path, lib_id)` | Removes the file of a footprint of a project library, as "Delete" of the footprint editor. A warning gives the uses of the footprint in the project |
| `upgrade_project_libraries(project_path, libraries?, include_outside?)` | Converts the libraries of an older file version of the project tables with `kicad-cli sym upgrade` and `kicad-cli fp upgrade`. KiCad saves the result again: the bytes must not change and the names must stay, else the tool writes nothing for that library. No backup: the output names a library that git cannot give back. A library outside the project folder only when `libraries` names it or with `include_outside=True` |

## Bill of materials

| Tool | What it does |
|---|---|
| `export_bom(schematic_path, output_path?, fields?, group_by?, exclude_dnp?, variant?, all_variants?)` | The BOM of the full project, as `kicad-cli sch export bom` makes it: the selected fields (default: all), one line for each reference or groups, the DNP filter, and the variants of KiCad 10 (one variant, or a CSV file for each variant). When the units of one package have a different Value, MPN, Value_Confirmed or MPN_Confirmed, the answer starts with an error of the design: the BOM and the CSV file are still written, and their line of the package has the values joined with a comma (KiCad 9 and 10) or `-- mixed values --` (KiCad 8) |
| `check_symbol_fields(schematic_path)` | The check of the confirmed MPNs (Value, MPN, Value_Confirmed, MPN_Confirmed), for a user with no datasheet-lens. See [datasheet-lens.md](datasheet-lens.md) |

## Edits

The schematic edit tools (`add_component_from_library`, `add_wire`, the label tools)
and `set_symbol_fields` write the file in the format of its file version, refuse a
project with a schematic file of an older version than the installed KiCad, and
refuse a file that KiCad has open. `setup_pcb_layout` gives only a warning for an
older schematic, because `pcbnew` writes the new board in the version of the installed
KiCad. `create_kicad_project` makes new files. See [editing.md](editing.md).

| Tool | What it does |
|---|---|
| `create_kicad_project(project_path, project_name, title?, company?, template?, overwrite?)` | A new project as the project manager of the installed KiCad makes it: "New Project", or "New Project from Template" (see [editing.md](editing.md)) |
| `add_component_from_library(file_path, library_name, symbol_name, reference, value, footprint?, x?, y?, unit?, sheet_path?, snap_to_grid?)` | Places a symbol of a library, as the schematic editor places it |
| `add_wire(file_path, points, snap_to_grid?)` | A wire through two or more points, with the junctions and the wire changes of the schematic editor |
| `add_label(file_path, text, x, y, orientation?, snap_to_grid?)` | A local label |
| `add_global_label(file_path, reference, pin, label_name, shape?, x?, y?, snap_to_grid?)` | A global label on a pin of a component |
| `add_hierarchical_label(file_path, reference, pin, label_name?, shape?, x?, y?, snap_to_grid?)` | A hierarchical label on a pin of a component |
| `set_symbol_fields(schematic_path, changes)` | Writes fields of placed symbols, as KiCad writes them: each unit of a reference, a new field hidden, the fields of a variant of KiCad 10. It checks all changes and files first |
| `set_symbol_attributes(schematic_path, changes)` | Writes the attributes of placed symbols as the Symbol Properties dialog: Do not populate, Exclude from BOM, board, position files and simulation, for each unit, also of a variant of KiCad 10. A variant keeps the old value of a changed attribute of the symbol, as KiCad. KiCad 10 only |
| `setup_pcb_layout(schematic_path, width?, height?, unit?, overwrite?)` | A new board with an outline, written by `pcbnew` of the installed KiCad. It does not replace an existing board without `overwrite=True` |

## Live board edits (IPC)

The IPC tools change the board that is open in the PCB editor of a running KiCad, through
the IPC API of KiCad. KiCad does each change itself. They need the extra `ipc` of the
server, and the KiCad API in the KiCad settings (see
[installation.md](installation.md#the-ipc-tools)). The server has IPC rules only for
KiCad 10.0.6 now. More tools come in the next versions.

| Tool | What it does |
|---|---|
| `ipc_status(board_path?)` | The state of the connection: the version of kicad-python and of the running KiCad, the open editors and boards, if the tools can write now, and the limits of the running KiCad version with the version that solves each limit |
| `ipc_get_selection(board_path)` | The items that the user selected in the PCB editor: type, reference, net, layer, position and ID |
| `ipc_select_items(board_path, references?, nets?, mode?)` | Selects footprints by reference, and the tracks, arcs, vias and shapes of nets (the item types of "Select All Tracks in Net"). The selection filter of the editor does not apply: the API cannot read it. `mode`: `replace`, `add`, `remove` or `clear`. The selection is no change of the board (no undo step) |
| `ipc_place_footprints(board_path, placements, flip_direction, override_locks?)` | Places footprints: KiCad 10.0.6 can only change the side (a flip around the position of the footprint). `flip_direction` is `left_right` or `top_bottom`: the API cannot read the flip direction of the PCB editor. One call is one undo step. A locked footprint flips only with `override_locks`, as "Override locks" of the PCB editor. A position, a rotation or a lock gives an error with the limit of the KiCad version |
| `ipc_save_board(board_path)` | Saves the board of the editor into its file, as File > Save. A changed file version gives a warning |
| `ipc_refill_zones(board_path, timeout_s?)` | Fills all zones, as "Fill All Zones", and waits for the end of the fill (its own undo step). It gives the time and the number of filled copper zones. It cannot see a cancel of the fill by the user |
| `ipc_add_tracks(board_path, net, tracks?, vias?, width_mm?, via_size_mm?, via_drill_mm?)` | Adds straight tracks and through vias on one net, in one undo step. The sizes come from the netclass of the net, or from the arguments. A net that the board does not have gives an error. KiCad 10.0.6 cannot make a free via: a via must be at an end of a new track of the same call |
| `ipc_add_zone(board_path, net, layer, outline, min_thickness_mm, connection, island_removal, priority, border_style, border_pitch_mm, thermal_gap_mm, thermal_spoke_width_mm, clearance_mm?, name?)` | Adds a copper zone (fill mode solid) on one net and one layer, in one undo step. The zone is not filled yet: fill it with `ipc_refill_zones`. The API gives no default zone settings: they are required, with the limits of the zone dialog of KiCad. With no `clearance_mm`, the zone has no own clearance (0): KiCad uses the current clearance of the netclass and the rules; a given clearance is a minimum (KiCad uses the larger value) |
| `ipc_add_board_outline(board_path, outline, line_width_mm?)` | Adds the board outline as one closed polygon on Edge.Cuts, in one undo step, to a board with no Edge.Cuts shapes. The line width comes from the graphics defaults of the board. The tools delete no items (KiCad 10.0.6 gives no result of a delete): the user deletes an old outline in the PCB editor |

## Exports and images

| Tool | What it does |
|---|---|
| `export_schematic_svg(schematic_path, output_dir?)` | SVG files made by kicad-cli with the full hierarchy: all pages for the root schematic, the pages of its sheet paths for a sub-sheet |
| `render_pcb(pcb_path, side?, output_path?, width?, height?, quality?, rotate?)` | A 3D view of the board as PNG or JPEG (the extension of `output_path`), made by kicad-cli of KiCad 9 or later (kicad-cli 8 has no "pcb render") |
| `export_pcb_3d(pcb_path, output_path?, board_only?)` | A STEP model of the board, made by kicad-cli. A 3D model that kicad-cli did not add gives a warning with the footprint and the reason |
| `export_gerber(pcb_path, output_dir?, refill_zones?)` | Gerber and drill files, made by kicad-cli with the plot settings and the drill origin of the board; `refill_zones` fills the zones again before the plot (KiCad 10.0) |

## Parts registry

The registry is open and needs no login (default [PartReel](https://partreel.com);
`PARTS_REGISTRY_URL` changes it).

| Tool | What it does |
|---|---|
| `search_parts_registry(query, limit?)` | Parts whose ID, name, family, category, manufacturer or keywords match |
| `get_registry_part(part_id)` | The record of a part: the download URLs, the datasheet link, the dimensions |
| `download_registry_part(part_id, save_dir, formats?)` | Downloads the files of a part: the footprint and the symbol, or the formats that `formats` gives (also the 3D model). `save_dir` must exist, in the working folder, the home folder or a folder of `PARTS_REGISTRY_SAVE_ROOTS`. Only HTTPS from the hosts of the registry, at most 50 MB for each file, and only the file extensions of each format |

## Background jobs

| Tool | What it does |
|---|---|
| `job_status(job_id?, cancel?)` | With no ID: the jobs of the server process (running, done, failed, cancelled). With an ID: the state of the job, or its result (the same text as the tool gives). `cancel` stops the task at its next step. Work in a thread does not stop: kicad-cli, the Python of KiCad or a file write can still end and change files; only the schematic edit tools (`add_component_from_library`, `add_wire`, the label tools) do not save when the cancel comes before their save |

Each tool call runs as a task. The server waits for it up to 20 s: a call that ends in
time gives its result as before. A longer call becomes a job. A call of the same tool
with the same arguments while the job runs starts no second run: it waits for the same
job (the same arguments after the defaults of the tool; 30 and 30.0 are the same
number). The list of `job_status` also has the calls that run in their first 20 s. The
jobs are in the memory of the server process: a restart of the server (of the
MCP client) loses them. A finished job stays 1 h (the 50 newest finished jobs). The
model is the model of the MCP Tasks extension (a task ID, its state, its result, a
cancel).

