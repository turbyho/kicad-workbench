# Changelog

Each release of KiCad Workbench, with the changes for the user.

## 0.2.0 (2026-11-20)

KiCad Workbench 0.2.0 adds 26 tools: the server now has 77 tools. With the new tools,
the agent makes and changes the project libraries, writes the attributes and the
footprints of placed symbols, and updates placed symbols from their libraries. It also
changes the board in the PCB editor of a running KiCad, through the IPC API of KiCad.
A tool call that takes longer than 20 s now continues as a background job. KiCad 10 is
now the supported version.

### Supported KiCad versions

- KiCad 10 and later is the supported version. The checks of the server compare its
  rules with KiCad 10.0.7.
- The existing tools also work with KiCad 8 and 9, as in 0.1.0. Their rules for
  KiCad 8 and 9 do not change.
- The new tools and the new argument `refill_zones` have the rules of KiCad 10.0 only.
  With KiCad 8 or 9, or with a nightly build, they give an error. `job_status` needs no
  KiCad.
- The IPC tools need a running KiCad 10.0.6. The server has IPC rules only for this
  version: a different version gives an error.
- What this means for you: install KiCad 10 to use the new functions. A project that
  you use with KiCad 8 or 9 continues to work with the existing tools.

### Install

- New optional extra `ipc` for the IPC tools: `kicad-workbench[ipc]`. It installs
  [kicad-python](https://pypi.org/project/kicad-python/) 0.8.0. The server needs this
  exact version. The extra `test` also has kicad-python 0.8.0. The other dependencies
  do not change.
- The IPC tools also need the API server of KiCad. In KiCad, select Preferences >
  Preferences > Plugins, then set "Enable KiCad API". The server connects to the
  default socket of kicad-python, or to the socket of the environment variable
  `KICAD_API_SOCKET` of KiCad when it is set.
- With KiCad 10.0.6, the IPC tools do not write while the schematic editor is open: a
  commit then stops KiCad (KiCad bug #24966, fixed in KiCad 10.0.7).

### New tools (26)

#### Project libraries (13)

These tools change the project library tables and the libraries in them, as the
editors of KiCad 10 do. The global library tables do not change. The tools need KiCad
10.0, and they refuse a project that KiCad has open. When a tool changes a library,
kicad-cli saves it, and a second save must give the same bytes.

- `create_project_library`: makes a new, empty symbol or footprint library and adds it
  to the project table, as "New Library" of the symbol editor and of the footprint
  editor.
- `add_library_to_table`: adds a KiCad library that exists to the project table, as the
  table dialog of KiCad.
- `remove_library_from_table`: removes a library from the project table. The library
  files stay.
- `set_library_table_row`: enables or disables a library of the project table, and
  shows or hides a symbol library.
- `upgrade_project_libraries`: converts the older libraries of the project tables to
  the file version of the installed KiCad, with `kicad-cli sym upgrade` and
  `kicad-cli fp upgrade`. The tool writes a library only when a second save of KiCad
  gives the same bytes and the same names. It makes no backup.
- `copy_symbol_to_library`: copies a symbol into a project library, as "Save As" of
  the symbol editor. A derived symbol goes with its parents, or as one flattened symbol
  (`flatten=True`).
- `copy_footprint_to_library`: copies a footprint into a project library, as "Save As"
  of the footprint editor.
- `set_library_symbol_properties`: changes the fields, the keywords and the footprint
  filters of a symbol of a project library, as the Symbol Properties dialog.
- `create_derived_symbol`: makes a new symbol that extends a symbol of the same project
  library, as "Derive from existing symbol".
- `rename_library_symbol`: gives a symbol of a project library a new name, as "Rename"
  of the symbol editor. The answer gives the placed symbols with the old name as a
  warning: KiCad does not change them.
- `delete_library_symbol`: deletes a symbol from a project library, as "Delete" of the
  symbol editor. With `delete_derived=True`, it also deletes the derived symbols. The
  answer gives the placed symbols that use a deleted symbol.
- `rename_library_footprint`: gives a footprint of a project library a new name, as
  "Rename" of the footprint editor. The answer gives the uses of the old name in the
  project as a warning: KiCad does not change them.
- `delete_library_footprint`: deletes a footprint from a project library, as "Delete"
  of the footprint editor. The answer gives the uses of the footprint in the project.

#### Schematic edits (3)

These tools need KiCad 10.0. They check all changes and all files first, then write all
files or no file. They refuse a file that KiCad has open.

- `set_symbol_attributes`: writes the attributes of placed symbols, as the Symbol
  Properties dialog: "Do not populate" and "Exclude from" bill of materials, board,
  position files and simulation. It also writes the attributes of a variant.
- `assign_footprint`: writes the footprint of placed symbols, as CvPcb ("Assign
  Footprints") saves it. The footprint must be in a library that KiCad loads. When the
  footprint filters or the pin count do not match, the answer has a warning.
- `update_symbols_from_library`: updates placed symbols from their library symbols, as
  "Update Symbols from Library" of the schematic editor, with the options and the
  defaults of that dialog.

#### Live board edits through the IPC API (9)

These tools operate on the board that is open in the PCB editor of a running KiCad
10.0.6. KiCad does each change, and one tool call is one undo step. The API of KiCad 10
cannot run DRC: after a change, run DRC.

- `ipc_status`: shows the state of the connection to the running KiCad, if the IPC
  tools can operate now, and the limits of the running KiCad version. Use it first.
- `ipc_get_selection`: shows the items that the user selected in the PCB editor.
- `ipc_select_items`: selects footprints by reference, and the tracks, arcs, vias and
  shapes of nets. The selection is not a change of the board.
- `ipc_place_footprints`: moves footprints to the other side of the board. With KiCad
  10.0.6, the tool can only change the side, not the position, the rotation or the
  lock.
- `ipc_add_tracks`: adds straight tracks and through vias on one net.
- `ipc_add_zone`: adds a copper zone with a solid fill on one net and one copper layer.
  The zone settings are required arguments. KiCad does not fill the new zone.
- `ipc_add_board_outline`: adds the board outline as one closed polygon on Edge.Cuts,
  to a board that has no Edge.Cuts shapes.
- `ipc_refill_zones`: fills all zones, as "Fill All Zones", and waits for the end of
  the fill.
- `ipc_save_board`: saves the board into its file, as File > Save.

#### Background jobs (1)

- `job_status`: lists the background jobs, gives the state or the result of one job,
  or cancels a job.

A tool call that takes longer than 20 s becomes a background job. The answer starts
with "⏳" and gives the job ID. The work continues, also when the MCP client stops its
wait (Claude Desktop stops after 60 s). A second call of the same tool with the same
arguments while the job runs starts no second run. The jobs are in the memory of the
server process: a restart of the server loses them. A finished job stays for 1 h.

A cancel stops the job at its next step. Work that kicad-cli, the Python of KiCad or a
file write already does can still end and change files.

### New arguments

- `refill_zones` of `run_drc`, `get_drc_violations` and `export_drc_report`: with
  `True`, kicad-cli fills all zones again before the DRC (`--refill-zones`), as the DRC
  dialog of KiCad does by default. The fill is only in memory: the board file does not
  change. The default (`False`) checks the zone fills of the file. Each answer has the
  line "Zones". With `refill_zones`, `export_drc_report` writes
  `<board>_drc_refill_report.json` by default. Needs KiCad 10.0.
- `refill_zones` of `export_gerber`: with `True`, kicad-cli fills all zones again before
  the plot (`--check-zones`), as the Plot dialog of KiCad does by default. The board
  file does not change. Needs KiCad 10.0.
- `severity` of `detect_pin_conflicts`: `"error"` or `"warning"`. Empty gives all
  violations.

### Changes

- `detect_pin_conflicts` now runs the ERC of kicad-cli and gives the items about the
  connections of the symbol pins. It uses the pin map, the severities and the
  exclusions of the project. Before, the tool used its own rules on the netlist: two
  driver pins on a net were an error, and a net with one pin was a warning. In the
  default pin map of KiCad, two open collector pins are not a conflict. With KiCad 8 or
  9, the answer has a warning.
- `export_gerber` now uses the plot settings that the board saves, as the Plot dialog
  of KiCad. The drill files use the drill origin of the board (the auxiliary origin
  when the plot settings use it). Before, the tool used the default options of
  kicad-cli and the absolute origin. The answer gives the plot settings, the zone
  fills, the drill origin and the Gerber job file.
- `get_symbol_details` now gives each field of the placed symbol as KiCad 10 loads it:
  the mandatory fields, then the user fields (for example MPN). Each text is a JSON
  string. Before, the field list had only the Reference field. With KiCad 8 or 9, the
  tool gives the five mandatory fields and a warning. A unit with different field texts
  gets a warning line.
- `get_erc_violations` and `get_drc_violations` count only the active violations. The
  table still shows an excluded violation with "(excluded)". `get_erc_violations`
  refuses an unknown severity.
- `get_drc_violations`: a type filter that matches no active violation gives "✅" only
  when the board has no other active violation. Else the answer is "⚠️" with their
  count, also for a type with a typing error.
- The board tools give the standard names of the copper layers (F.Cu, In1.Cu, B.Cu),
  also when the board has user names for them. `list_pcb_footprints` refuses a
  `filter_layer` other than F.Cu or B.Cu. Before, it gave an empty list.
- The board tools count only the copper zones, as the Zone Manager of KiCad: no rule
  area, no teardrop, and no zone that is only on non-copper layers. Each zone has all
  of its copper layers.
- `find_tracks_by_net` also gives the copper zones of the net, and finds a net that has
  only zones. The line "Track Layers" replaces "Layers Used".
- With no `pcbnew`, the board tools now give the same answers as with `pcbnew`, except
  the board size, the net count, the design rules and the fill state of the zones. The
  warning names these limits. Before, the text reader of each tool had its own
  output, with different counts and word lists, and no RF nets in
  `analyze_pcb_signal_integrity`.
- The board tools do not block the server while they read a board. A long board call
  can become a background job.
- `get_netlist_nets`, `analyze_hierarchical_nets` and `search_symbols`: a filter
  pattern that is not a regular expression gives an error, and a search that takes
  longer than 15 s gives an error. The searches do not block the server.
- `add_component_from_library` with KiCad 10.0 copies the library symbol as a save of
  kicad-cli gives it, thus with the texts as KiCad loads them. A symbol of an older
  library with "~" as an empty text or with a second body style is not refused now.
- With KiCad 10, the tools read the text of a mandatory field as KiCad 10 loads it: "~"
  of an older file is an empty text, and the white space at the two ends goes.
- The descriptions of the tools now tell how each tool operates, what it refuses, and
  the properties of its result that are correct but can look strange.

### Fixes

- The pcbnew reader gave F.Cu as the layer of each zone. Thus
  `analyze_pcb_power_integrity` gave a false warning about the GND zones on each board
  with more than 2 copper layers, and `find_tracks_by_net` gave wrong zone layers.
- `add_global_label` and `add_hierarchical_label` now take the angle of a label on a
  pin from the direction of the pin, as KiCad does. Before, a label on the long side of
  a tall symbol pointed along the body.
- `add_label` wrote "left" justification at each angle, thus a label at 180 degrees
  had its text over the wire. The label tools now write the justification and the
  integer angle as KiCad writes them. `add_label` refuses an angle that is not 0, 90,
  180 or 270.
- `add_global_label` now writes the hidden field Intersheetrefs at the point of the
  label, as KiCad makes it. Before, the field was at the point 0, 0.
- `set_symbol_fields` with KiCad 10 finds a field by the name that KiCad gives it at
  the load: a second "MPN" field is "MPN_1". Before, the tool added a new field for
  "MPN_1", and refused "MPN" when the symbol had two such fields.
- With KiCad 10, the library tools read the library tables with the rules of KiCad 10.
  Before, they showed libraries that KiCad 10 does not load.
- `get_netlist_nets` gave a traceback for a pattern that is not a regular expression.

All tools are in [docs/tools.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/tools.md).

## 0.1.0 (2026-11-08)

KiCad Workbench is an MCP server that lets an AI agent work with KiCad 8, 9 and 10
projects. The agent reads schematics, boards and libraries, runs the checks of KiCad,
makes the bill of materials (BOM), and writes changes to the schematic. This is the
first release.

KiCad Workbench started as a fork of
[Seeed-Studio/kicad-mcp-server](https://github.com/Seeed-Studio/kicad-mcp-server) (MIT).
The notes below give the changes for a user of that server.

### Install

- The package, the command and the Python module have new names:
  `kicad-workbench` (PyPI), the command `kicad-workbench`, the module
  `kicad_workbench`. The server gives the name `kicad-workbench` to the client (before:
  `kicad-mcp-server`). The documentation registers it in the client with the key
  `kicad`: the tools are then `mcp__kicad__<tool>` in Claude Code.
- Requirements: Python 3.10 or later, KiCad 8, 9 or 10 with kicad-cli. For exact board
  data, install the server into a Python that can import the module `pcbnew` of
  KiCad ([docs/installation.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/installation.md)).
- Dependencies: `kicad-skip`, `python-dotenv`, `jinja2` and the extra `cli` of `mcp`
  are not necessary now. New: `packaging` and `resvg-py`. The extras `hardware` and
  `all` are removed. The extra `test` has `hatchling` (new) and no `pytest-mock`. The
  extra `dev` is the extra `test` and ruff (one version): it has no `black` and no
  `mypy`.

### Removed

- The server does not read a file `.env` now (the template `.env.example` and the
  dependency `python-dotenv` are removed), and the resource `kicad://config` is
  removed. No tool read their values (`KICAD_PROJECT_PATHS`, `DEFAULT_SUMMARY_DETAIL_LEVEL`,
  `INCLUDE_NETS_IN_SUMMARY`, `INCLUDE_POWER_IN_SUMMARY`, `DEFAULT_TEST_FRAMEWORK`,
  `DEFAULT_TEST_TYPE`).
- The 8 firmware tools are not in this release: `analyze_pin_functions`,
  `extract_pinmux_config`, `extract_gpio_config`, `extract_i2c_devices`,
  `extract_spi_devices`, `extract_power_domains`, `validate_pin_configuration` and
  `generate_device_tree`. They guessed the data from the net names and the values,
  gave values that come from no datasheet, and `generate_device_tree` gave a device
  tree that `dtc` refuses. A new design for more than one SDK (Zephyr, STM32Cube,
  ESP-IDF, Linux and others) is planned. `detect_pin_conflicts` stays: it reads the
  pin types of the netlist of KiCad.

### New tools (12)

The server has 51 tools: 39 of the 47 tools of the fork, and 12 new tools.

- Libraries: `list_libraries`, `search_library_symbols`, `search_library_footprints`,
  `get_library_symbol`, `get_library_footprint`, `render_library_item` (PNG images of
  a symbol or a footprint at 300 DPI or more).
- Checks: `check_pin_pad_mapping` (a library symbol and a footprint),
  `check_schematic_pin_pad_mapping` (each symbol of a schematic, as "Update PCB from
  Schematic"), `check_library_links` (the links to the symbol and footprint libraries
  and the 3D models).
- BOM and fields: `export_bom` (the BOM of kicad-cli, also for the variants of KiCad
  10), `check_symbol_fields` (the check of the confirmed MPNs), `set_symbol_fields`
  (the fields of placed symbols, as KiCad writes them).

### KiCad is the specification

- The tools use KiCad where KiCad has an interface: kicad-cli for the netlist, the
  BOM, ERC, DRC and the exports, and `pcbnew` for boards and footprints. Where KiCad
  has no interface, the tools use the rules of the KiCad source for the installed
  KiCad version. We compare these rules with KiCad 8.0.9, 9.0.9 and 10.0.7.
- The tools read a file as the installed KiCad loads it. The server does not convert
  files:
  - A schematic, a board or a library of an older version than the installed KiCad
    gives a warning with the step for the user (the conversion in KiCad, or
    `kicad-cli sym upgrade` and `kicad-cli fp upgrade` for a library).
  - A tool that writes refuses an older schematic or board.
  - A file of a newer version than the installed KiCad is an error.
- With no KiCad, each tool that needs KiCad gives an error that tells the user to
  install KiCad 8 or later. The minimum is KiCad 8.
- The installed KiCad version: each tool that uses KiCad refuses a KiCad that the
  server has no rules for (for example 11.0, or a nightly build other than 10.99) and
  a release candidate. The tools of the parts registry and the tools that read a
  netlist file need no KiCad. A newer patch release (for example 10.0.8) and a
  development build of a release branch get the rules of the newest compared patch
  release, and each answer of a tool that uses this KiCad gets a warning. The server reads the full version from `kicad-cli version --format about`.
- The server finds kicad-cli in the environment variable `KICAD_CLI`, then in `PATH`,
  then in the standard install folder of KiCad (on Windows the newest version folder
  that has kicad-cli). The Python of KiCad for `pcbnew` comes from `KICAD_PYTHON`,
  else the Python of the server process when it can import `pcbnew`, else the Python
  of the installation of kicad-cli. A `KICAD_CLI` or `KICAD_PYTHON` with a path that is
  not a file is an error: the server does not use a different KiCad then.
- The text of a result is honest: an error starts with "❌", a warning with "⚠️", and
  a "✅" comes only after a full check. A tool tells when a result can be incomplete,
  for example with an annotation error of the schematic.

### Schematics and edits

- The tools read the files with an own S-expression parser (the lexer rules of
  KiCad), not with `kicad-skip`.
- A placed symbol has the reference and the unit of each sheet path, as KiCad gives
  them: a sheet that the hierarchy uses two times gives two references.
- The edits (`add_component_from_library`, `add_wire`, the label tools,
  `set_symbol_fields`) write the format of the file version. Components, wires and
  labels get the junctions and the connection grid of the schematic editor. A tool
  refuses a file that KiCad has open (the lock file of KiCad), and it saves a file as
  KiCad does: a temporary file, then a rename.
- `run_erc` and `get_erc_violations` add the annotation check of the ERC dialog,
  which kicad-cli does not run, with the severities and the exclusions of the
  project.

### Boards

- The board tools use the `pcbnew` of the installed KiCad. A `pcbnew` of a different
  KiCad version than kicad-cli is an error. With no `pcbnew`, they read the board file
  as text and give a warning with the limits of each tool.
- `list_pcb_footprints` gives the full library ID and the number of pads of each
  footprint.
- `render_pcb` needs KiCad 9 or later, and writes PNG or JPEG images.
- `export_pcb_3d` gives a warning for each 3D model that kicad-cli did not add to the
  STEP file.

### Other

- kicad-cli writes its messages and reports in English, also when the user selected
  a different language in KiCad.
- The server tells the agent about a newer release on PyPI (one check a day). A value
  other than "" and "0" in the environment variable `KICAD_WORKBENCH_NO_UPDATE_CHECK`
  stops this check.
- The descriptions of the tools are in ASD-STE100 Simplified Technical English.

All tools are in [docs/tools.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/tools.md).
