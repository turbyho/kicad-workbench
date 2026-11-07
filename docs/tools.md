# Tools

The server has 59 tools. Each tool returns Markdown text (the image tools also return
PNG images). An error starts with "❌", a warning with "⚠️". An error that the tool
does not expect (a defect of the tool) also gives the traceback. The agent selects
the tools: this list tells you what the agent can do, and with which limits.

An argument with `?` is optional. A path argument is the path of a file of the
project: `.kicad_sch` (schematic), `.kicad_pcb` (board), `.kicad_pro` (project), or a
netlist file of `generate_netlist`.

## Exact and heuristic tools

Most tools give the result of KiCad: they use kicad-cli or `pcbnew`, or the rules of
the KiCad source code for the file version. Before a tool works with a schematic
project, the installed KiCad converts and saves the schematic files of an older
version (the README, "File versions"). Some tools use heuristics: they find a
function from a net name, a component type from its value or reference, or a
differential pair from the names of two nets. These tools are marked "heuristic"
below. Use their result as a start for a review, not as a fact.

## Schematics

| Tool | What it does |
|---|---|
| `get_schematic_info(file_path)` | The title block, the counts of components and nets, the components by reference prefix, and the sheets |
| `list_schematic_components(file_path, filter_type?, filter_value?, filter_dnp?)` | The components, with a filter by type, value or DNP |
| `list_schematic_nets(file_path, filter_power?)` | The nets, optionally only the power nets |
| `search_symbols(file_path, pattern, search_fields?)` | The symbols whose reference, value or library ID matches a regular expression |
| `get_symbol_details(file_path, reference)` | The fields and the pins of one component of the project. The pins (number, name, electrical type) come from the netlist of kicad-cli. `trace_netlist_connection` gives the net of each pin |
| `search_components_by_type(file_path, component_type, search_in_subsheets?)` | Components of a type, for example "sensor" or "display" (heuristic: value, footprint and nets), in the file and in all sheets below it |

The component tools give each placed symbol of a file one time for each sheet path
of the file, with the reference and the unit that KiCad gives that path: a sheet
that the hierarchy uses two times gives two references, and a symbol with "U?" in
its Reference field has the reference of its instance. They need kicad-cli for the
version of KiCad, whose rules give the references.

## Sheet hierarchy

| Tool | What it does |
|---|---|
| `analyze_hierarchical_nets(file_path, filter_pattern?, show_hierarchy?)` | The nets of all sheets (the netlist of kicad-cli) by group, and the sheet paths |
| `trace_hierarchical_connection(file_path, reference, pin_number?)` | The sheet and the connections of a component, at each level of the hierarchy |

## Netlist

| Tool | What it does |
|---|---|
| `generate_netlist(schematic_path)` | Makes the netlist of the schematic with kicad-cli, and gives its path |
| `trace_netlist_connection(netlist_path, reference, pin_number?)` | The nets of the pins of a component, and the pins on each net |
| `get_netlist_nets(netlist_path, filter_pattern?)` | The nets of the netlist with their pins |
| `get_netlist_components(netlist_path, filter_ref?)` | The components of the netlist with their nets |

The netlist of kicad-cli is the most exact source for the connections. Use it before
the schematic tools when the connection is important.

The netlist tools above read the file of `generate_netlist`: make it again after a
change of the schematic. The pin, device tree, hierarchy and search tools, and
`get_symbol_details`, make the netlist with kicad-cli at each call. Thus they always
give the current state of the schematic, and they need kicad-cli. For a large
project, each call takes some seconds.

For a sub-sheet or an other top-level sheet, these tools and the ERC tools use the
schematic of the project name, as for the BOM. A KiCad 10 project with more than one
top-level sheet needs kicad-cli of KiCad 10.0.7 or later, which loads all top-level
sheets: kicad-cli 10.0.0 to 10.0.6 loads only one, thus these tools give an error, not
a result with some sheets.

The hierarchy and search tools get the components from the netlist: each component
of each sheet path, at each level of the hierarchy, with the reference of that path
(a sheet that the hierarchy uses two times gives two references). The netlist has
no power symbols and no symbol with no library symbol.

## Boards

These tools import `pcbnew` in the server process. Without `pcbnew`, they read the
board file as text, with no design rules and no board outline, and each answer
has the warning "pcbnew not available" (see [installation.md](installation.md)).
When `pcbnew` cannot read the board (for example a board of a newer KiCad), the
tools give an error: the text reader does not know the rules of such a board. The
text reader reads only the boards of KiCad 8 to 10; an older board needs `pcbnew`, or
a save in KiCad 8 or later.

| Tool | What it does |
|---|---|
| `list_pcb_footprints(file_path, filter_layer?)` | The footprints, optionally of one layer |
| `get_pcb_statistics(file_path)` | The size of the board, the layers, the counts, the design rules |
| `find_tracks_by_net(file_path, net_name)` | The tracks and vias of one net, with their lengths and widths |
| `analyze_pcb_nets(file_path)` | The distribution of track widths and vias, the nets by length, the layers |
| `analyze_pcb_signal_integrity(file_path, net_name?)` | Differential pairs and their length difference (heuristic: the pairs come from the net names, for example `USB_P` and `USB_N`), RF tracks, the longest signal nets |
| `analyze_pcb_power_integrity(file_path)` | The copper zones, the routing of the power nets, the area of the ground zones |

## Checks

| Tool | What it does |
|---|---|
| `run_erc(schematic_path)` | ERC of kicad-cli, with the severities of the project. It adds the annotation check of the ERC dialog (no annotation, a duplicate reference, units with different values), which kicad-cli does not run |
| `get_erc_violations(schematic_path, severity?)` | The ERC violations, with a filter by severity |
| `export_erc_report(schematic_path, output_path?)` | The ERC report of kicad-cli as a JSON file |
| `run_drc(pcb_path)` | DRC of kicad-cli. When the schematic with the same name is next to the board, also the schematic parity check |
| `get_drc_violations(pcb_path, violation_type?)` | The DRC violations, with a filter by type |
| `export_drc_report(pcb_path, output_path?)` | The DRC report of kicad-cli as a JSON file |
| `check_schematic_pin_pad_mapping(schematic_path, references?)` | For each symbol: the pins with no pad, the pins with no number, the pads with no pin, and the symbols whose footprint KiCad does not find. It uses the netlist and the rules of "Update PCB from Schematic" |
| `check_pin_pad_mapping(symbol_lib_id, footprint_lib_id?, project_path?)` | The same check for one library symbol and one footprint |
| `check_library_links(project_path)` | The links of a project to its libraries: the library checks of ERC and DRC, the footprints of the schematic, and the 3D models (as the STEP export finds them) |
| `detect_pin_conflicts(schematic_path)` | Nets with more than one output or driver pin (errors), nets with one pin (warnings), and pins with no connection (information). The pin types come from the netlist |
| `validate_pin_configuration(schematic_path)` | Pin conflicts before a device tree generation (heuristic, see [firmware.md](firmware.md)) |

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

## Bill of materials

| Tool | What it does |
|---|---|
| `export_bom(schematic_path, output_path?, fields?, group_by?, exclude_dnp?, variant?, all_variants?)` | The BOM of the full project, as `kicad-cli sch export bom` makes it: the selected fields (default: all), one line for each reference or groups, the DNP filter, and the variants of KiCad 10 (one variant, or a CSV file for each variant). It gives an error when the units of one package have a different Value, MPN, Value_Confirmed or MPN_Confirmed |
| `check_symbol_fields(schematic_path)` | The check of the confirmed MPNs (Value, MPN, Value_Confirmed, MPN_Confirmed), for a user with no datasheet-lens. See [datasheet-lens.md](datasheet-lens.md) |

## Edits

The edit tools write the file in the format of the installed KiCad (after the
conversion of an older file), and refuse a file that KiCad has open. See [editing.md](editing.md).

| Tool | What it does |
|---|---|
| `create_kicad_project(project_path, project_name, title?, company?, template?, overwrite?)` | A new project as the project manager of the installed KiCad makes it: "New Project", or "New Project from Template" (see [editing.md](editing.md)) |
| `add_component_from_library(file_path, library_name, symbol_name, reference, value, footprint?, x?, y?, unit?, sheet_path?, snap_to_grid?)` | Places a symbol of a library, as the schematic editor places it |
| `add_wire(file_path, points, snap_to_grid?)` | A wire through two or more points, with the junctions and the wire changes of the schematic editor |
| `add_label(file_path, text, x, y, orientation?, snap_to_grid?)` | A local label |
| `add_global_label(file_path, reference, pin, label_name, shape?, x?, y?, snap_to_grid?)` | A global label on a pin of a component |
| `add_hierarchical_label(file_path, reference, pin, label_name?, shape?, x?, y?, snap_to_grid?)` | A hierarchical label on a pin of a component |
| `set_symbol_fields(schematic_path, changes)` | Writes fields of placed symbols, as KiCad writes them: each unit of a reference, a new field hidden, the fields of a variant of KiCad 10. It checks all changes and files first |
| `setup_pcb_layout(schematic_path, width?, height?, unit?, overwrite?)` | A new board with an outline, written by `pcbnew` of the installed KiCad. It does not replace an existing board without `overwrite=True` |

## Exports and images

| Tool | What it does |
|---|---|
| `export_schematic_svg(schematic_path, output_dir?)` | SVG files made by kicad-cli with the full hierarchy: all pages for the root schematic, the pages of its sheet paths for a sub-sheet |
| `render_pcb(pcb_path, side?, output_path?, width?, height?, quality?, rotate?)` | A 3D view of the board as PNG, made by kicad-cli |
| `export_pcb_3d(pcb_path, output_path?, board_only?)` | A STEP model of the board, made by kicad-cli |
| `export_gerber(pcb_path, output_dir?)` | Gerber and drill files, made by kicad-cli |

## Firmware

These tools are heuristic. See [firmware.md](firmware.md) for the supported
microcontrollers and the limits.

| Tool | What it does |
|---|---|
| `analyze_pin_functions(schematic_path, reference?)` | The pins of each component (or of one reference), with a function from the net name (I2C, SPI, UART, GPIO), and the family of a microcontroller |
| `extract_pinmux_config(schematic_path, component_type?)` | The peripheral of each pin of a microcontroller |
| `extract_gpio_config(schematic_path, soc_family?)` | The GPIO pins, with port and bit (STM32) |
| `extract_i2c_devices(schematic_path)` | The I2C buses and the devices on them |
| `extract_spi_devices(schematic_path)` | The SPI buses and the devices on them |
| `extract_power_domains(schematic_path)` | The power nets and the regulators |
| `generate_device_tree(schematic_path, target_soc?, output_path?)` | A device tree source (.dts) for `stm32f4`, `esp32` or `nrf52` |

## Parts registry

The registry is open and needs no login (default [PartReel](https://partreel.com);
`PARTS_REGISTRY_URL` changes it).

| Tool | What it does |
|---|---|
| `search_parts_registry(query, limit?)` | Parts whose ID, name, family, category, manufacturer or keywords match |
| `get_registry_part(part_id)` | The record of a part: the download URLs, the datasheet link, the dimensions |
| `download_registry_part(part_id, save_dir, formats?)` | Downloads the files of a part: the footprint and the symbol, or the formats that `formats` gives (also the 3D model). `save_dir` must exist, in the working folder, the home folder or a folder of `PARTS_REGISTRY_SAVE_ROOTS`. Only HTTPS from the hosts of the registry, at most 50 MB for each file, and only the file extensions of each format |
