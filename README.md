# KiCad Workbench

A Model Context Protocol (MCP) server for KiCad 9.0+ that lets AI assistants analyze schematics, inspect PCBs, trace connections, validate designs, and generate embedded code.

## Features

- **Schematic Analysis** - Components, nets, symbols, hierarchical sheets
- **PCB Analysis** - Footprints, tracks, routing quality, signal/power integrity via pcbnew API
- **Netlist Tracing** - 100% accurate pin-level connection tracking
- **Design Validation** - ERC/DRC via kicad-cli (headless, CI friendly)
- **Pin Analysis** - Pin function detection, conflict analysis, pinmux config
- **Code Generation** - Device tree (.dts) and hardware test code generation
- **Project Management** - Create KiCad projects from templates
- **Parts Registry** - Search 21,000+ verified parts on an open, no-login registry ([PartReel](https://partreel.com) by default, `PARTS_REGISTRY_URL` to change) and download footprints/symbols/3D into the project; downloads are host-allowlisted, extension-checked and size-capped

## Requirements

- KiCad 8.0+ (9.0 or 10.0 recommended)
- `kicad-cli` in PATH (included with KiCad)

## Installation

### Recommended: Install into KiCad Python (Full PCB Analysis)

KiCad ships with its own Python that includes the `pcbnew` module. Installing into this Python gives you full PCB analysis capabilities (precise track lengths, via statistics, signal integrity, power integrity).

**Step 1**: Find your KiCad Python path:

| Platform | Path |
|----------|------|
| Windows | `C:\Program Files\KiCad\<version>\bin\python.exe` |
| macOS | `/Applications/KiCad/KiCad.app/Contents/Frameworks/python3` |
| Linux | `/usr/bin/python3` (if kicad python bindings installed) |

**Step 2**: Install the package into KiCad Python:

```bash
# Windows example (KiCad 10.0)
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip install fastmcp
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip install -e .

# macOS example
/Applications/KiCad/KiCad.app/Contents/Frameworks/python3 -m pip install fastmcp
/Applications/KiCad/KiCad.app/Contents/Frameworks/python3 -m pip install -e .
```

**Step 3**: Configure MCP to use KiCad Python (see Configuration section below).

### Fallback: Install into System Python (Limited PCB Analysis)

If you skip KiCad Python setup, the server still works but falls back to text-based PCB parsing. You get basic data (track counts, net names, widths) but lose precise lengths, design rules, and signal integrity analysis.

```bash
pip install -e .
```

## Configuration

### Claude Desktop

Edit your config file:
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

**With KiCad Python (recommended):**

```json
{
  "mcpServers": {
    "kicad": {
      "type": "stdio",
      "command": "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe",
      "args": ["-m", "kicad_workbench"],
      "cwd": "C:\\Users\\YourName\\Desktop\\kicad-workbench"
    }
  }
}
```

**With system Python (fallback):**

```json
{
  "mcpServers": {
    "kicad": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "kicad_workbench"],
      "cwd": "C:\\Users\\YourName\\Desktop\\kicad-workbench"
    }
  }
}
```

### Claude Code CLI

```bash
# With KiCad Python (recommended)
claude mcp add kicad -s user -- "C:\Program Files\KiCad\10.0\bin\python.exe" -m kicad_workbench

# With system Python (fallback)
claude mcp add kicad -s user -- python -m kicad_workbench
```

### Cursor / Windsurf / Other MCP Clients

Use the same config structure with your client's MCP settings. Point `command` to KiCad Python for full analysis, or `python` for fallback mode.

### Verify Installation

After restarting your AI client, ask it:

> "What tools are available for KiCad?"

You should see a list of KiCad MCP tools. To verify pcbnew is working:

> "Get PCB statistics for MyBoard.kicad_pcb"

If the response shows **Design Rules** section with clearance/width values, pcbnew is active. If it shows **board dimensions as approximation**, you're in text-only mode.

Common issues:
1. KiCad is installed and `kicad-cli` is accessible
2. The `cwd` path in your config points to the correct directory
3. Python can import the package (`python -c "import kicad_workbench"`)

## Usage

Once configured, you can interact with your KiCad projects through natural language in your AI assistant. Below are common workflows.

### 1. Analyze a Schematic

Point the AI to your `.kicad_sch` file and ask questions:

> "List all components in C:\Projects\MyBoard\MyBoard.kicad_sch"

> "Show me all the power nets in MyBoard.kicad_sch"

> "Find all resistors with value 10k"

The AI will call tools like `list_schematic_components`, `list_schematic_nets`, `search_symbols` behind the scenes.

### 2. Trace Connections (Netlist-based)

First export a netlist, then trace connections with pin-level accuracy:

> "Generate a netlist from MyBoard.kicad_sch"

> "Trace all connections from U1"

> "What pins are connected to the I2C_SDA net?"

This uses `generate_netlist` and `trace_netlist_connection` for 100% accurate results.

### 3. Validate Your Design

Run electrical and design rule checks:

> "Run ERC on MyBoard.kicad_sch and show me any errors"

> "Run DRC on MyBoard.kicad_pcb"

> "Are there any unconnected pins?"

Uses `run_erc`, `run_drc`, and `detect_pin_conflicts`.

### 4. Analyze PCB

> "Get statistics for MyBoard.kicad_pcb"

> "List all footprints on the top layer"

> "Find all tracks on the VDD_nRF net"

> "Analyze PCB routing quality"

> "Check signal integrity - are USB differential pairs length-matched?"

> "Analyze power integrity - how's the GND coverage?"

### 5. Create a New Project

> "Create a new KiCad project called LED_Blinker in C:\Projects"

> "Add a 1k resistor called R1 to the schematic"

> "Add an LED called D1"

> "Add a wire from R1 pin 2 to D1 pin 1"

### 6. Pin Analysis and Code Generation

> "Analyze pin functions for U1 in MyBoard.kicad_sch"

> "Detect any pin conflicts in the design"

> "Generate a device tree file for the STM32 on this board"

> "Generate pytest hardware tests for all I2C devices"

## Available Tools Reference

### Schematic Analysis

| Tool | Description |
|------|-------------|
| `list_schematic_components` | List components with filtering by type, value, or DNP status |
| `list_schematic_nets` | List all nets, optionally filter power nets |
| `get_schematic_info` | Project metadata and statistics |
| `search_symbols` | Search components by pattern (regex) |
| `get_symbol_details` | Detailed info for a specific component |

### PCB Analysis

| Tool | Description |
|------|-------------|
| `list_pcb_footprints` | List footprints with optional layer filter |
| `get_pcb_statistics` | Board dimensions, layer count, design rules |
| `find_tracks_by_net` | Track segments, lengths, widths, vias for a specific net |
| `analyze_pcb_nets` | Routing analysis: width/via distribution, net length ranking |
| `analyze_pcb_signal_integrity` | Diff pair matching, RF traces, longest signal nets |
| `analyze_pcb_power_integrity` | Power zones, GND coverage, power routing analysis |

### Netlist Analysis

| Tool | Description |
|------|-------------|
| `generate_netlist` | Export netlist from schematic via kicad-cli |
| `trace_netlist_connection` | Trace all connections from a component/pin |
| `get_netlist_nets` | List all nets with pin connections |
| `get_netlist_components` | List components with their net connections |

### Bill of Materials

| Tool | Description |
|------|-------------|
| `export_bom` | The BOM of the full project, as `kicad-cli sch export bom` of the installed KiCad makes it: the fields that the caller selects (default: all fields), one line for each reference or groups, the DNP filter, and the variants of KiCad 10 (one variant, or all variants with one CSV file for each). It gives an error of the design when the units of one package have a different Value, MPN or Value_Confirmed. It can keep the CSV file of kicad-cli, for example for `project_check` of [datasheet-lens](https://github.com/turbyho/datasheet-lens) |
| `check_symbol_fields` | The check of the MPNs of the symbols when datasheet-lens is not available, with the same messages as its `project_check`: Value holds the general value and the MPNs in one text in parentheses (`2N7002 (2N7002LT1G, BSS138LT1G)`), MPN is the confirmed copy of these MPNs, and the hidden field Value_Confirmed is the Value of the last confirmation. It reads the BOM of kicad-cli of the default variant and of each variant of KiCad 10, and gives the conflicts, the changes, the symbols that are not confirmed, the fields that complete a symbol, and the symbols with no part, each with the change for `set_symbol_fields` |

### Libraries

The library tools read the library tables of the installed KiCad and of the project.

| Tool | Description |
|------|-------------|
| `list_libraries` | Symbol or footprint libraries, with the table, the state and the path |
| `search_library_symbols` | Symbols that match a name, a description or keywords |
| `search_library_footprints` | Footprints that match a name, a description or tags |
| `get_library_symbol` | Fields, units, body styles, footprint filters and pins (with alternate functions) of a library symbol, as KiCad places it |
| `get_library_footprint` | Type, attributes, size, courtyard, fields, pads (with padstacks) and 3D models (with the file that KiCad uses) of a library footprint, read by pcbnew of the installed KiCad |
| `check_pin_pad_mapping` | Pin numbers of a library symbol that have no pad in a footprint, pins with no number, and pads with no pin, with the rules of "Update PCB from Schematic" of the installed KiCad (KiCad 10: stacked pin numbers such as `[1-4]`) |
| `check_schematic_pin_pad_mapping` | The same check for each symbol of a schematic, with the netlist that "Update PCB from Schematic" gives pcbnew (the pins of the placed units, no symbol that is excluded from the board), and the symbols whose footprint KiCad does not find |
| `check_library_links` | The links of a project to its libraries: the library checks of ERC (symbols) and DRC (board footprints) of KiCad, the footprints of the schematic as "Update PCB from Schematic" finds them, and the 3D models of the schematic and board footprints as the STEP export finds them |
| `render_library_item` | PNG images of a library symbol (each unit and body style) or footprint (all layers), drawn by kicad-cli with the theme "KiCad Default". The caller gives the largest image that its vision input reads; tiles keep the smallest text legible (at least 15 px, at least 300 DPI). Also an overview, a region and a selection of pages |

### Validation

| Tool | Description |
|------|-------------|
| `run_erc` | Electrical Rules Check on schematic |
| `run_drc` | Design Rules Check on PCB |
| `detect_pin_conflicts` | Find conflicting pin connections |

### Editing

| Tool | Description |
|------|-------------|
| `create_kicad_project` | Create an empty project (default) or one from a KiCad template (`template="Arduino_Uno"`) |
| `add_component_from_library` | Add a symbol of a library of the library tables (also a derived symbol) |
| `set_symbol_fields` | Write the fields of placed symbols (for example MPN and Value_Confirmed of `project_check` of datasheet-lens): each unit of a reference, a new field hidden, the fields of a variant of KiCad 10, in the format of the file version. It checks all changes and all files first, and refuses a symbol of a shared sheet when the changes do not give each of its references the same fields |
| `add_wire` | Add wire connection |
| `add_label` | Add local label |
| `setup_pcb_layout` | Initialize PCB with dimensions (does not replace an existing board without `overwrite=True`) |
| `export_gerber` | Export Gerber files |

## Editing Limitations

Schematic editing is **experimental**. KiCad has no Python API for schematic editing, so tools use manual S-expression manipulation. `add_component_from_library` finds the library in the library tables of the installed KiCad and of the project, as the symbol chooser of KiCad does: a library of the project table replaces a global library with the same name. The tool gets the KiCad version from `kicad-cli`, to find the global library table. The tool copies the symbol into the schematic. A derived symbol gets the units, the pins and the drawings of its parents, as in KiCad (`LIB_SYMBOL::Flatten`). The placed symbol gets the fields of the library symbol at their library positions, the flags `in_bom`, `on_board`, `exclude_from_sim` and `in_pos_files` of the library symbol, and the reference of its sheet path (`(instances)`), as KiCad places a symbol. The tool does not move the fields (KiCad: autoplace). A sheet file that the hierarchy uses more than one time needs `sheet_path`: the sheet that gets the reference.

The connection grid is `schematic.connection_grid_size` of the project file (default 50 mils, 1.27 mm). ERC finds a pin or a wire end off this grid (`endpoint_off_grid`). `add_component_from_library`, `add_wire`, `add_label`, `add_global_label` and `add_hierarchical_label` have the parameter `snap_to_grid` (default `True`):
- `True`: the tool moves a symbol to the nearest position that puts its pins on the grid. It moves each wire point and each label to the nearest grid point. A point on a connection point of the sheet does not move, also when it is off the grid: a pin, a wire end, a bus entry end, a junction, a no-connect flag, a label or a sheet pin. For a label, also a point on a wire or a bus. (A pin or a wire end does not connect in the middle of a wire: KiCad needs a junction there.) Thus a new wire or label connects to an item off the grid. A horizontal or vertical wire segment stays horizontal or vertical.
- `False`: the tool uses the positions as they are.

`add_wire` and `add_component_from_library` add junctions as the schematic editor of KiCad does, with the rules of the file version (KiCad 8, 9 or 10): a junction where three or more wire directions meet, for example a wire end or a pin in the middle of a wire. The wire there splits into two wires. Before that, the tool merges the segments of a new wire that are on one straight line, merges a new wire with a wire on the same straight line that it touches or overlaps, and removes the part of a wire between two pins of a symbol (KiCad: `TrimWire`). When the rule of KiCad uses data that the file does not give (the box of a symbol with a hidden pin, the pen width of a bus entry from its net class) and the result depends on it, the tool refuses the change. The tool does these steps of `CleanUp` and `TrimWire` only for the new items. When old wires of the sheet that the schematic editor would change first (two wires on one straight line that `CleanUp` merges, a wire through two pins that `TrimWire` cuts) are at a point of the decision, the tool refuses the change.

The output gives the position that the tool writes, and each pin or wire end off the grid. It does not tell about these points when the project ignores the ERC check. `add_wire` adds one wire for each segment, because a KiCad wire has two points.

Known limitations:
- Wire connections may not form perfect electrical connections
- Visual alignment is basic
- The editing tools write the file directly. KiCad does not read a change to a file that it has open, and the next save in KiCad removes the change. Thus each editing tool refuses the write when KiCad has the file open (KiCad writes the lock file `~<file name>.lck` next to it). Close the schematic editor or the PCB editor before the edit, and open the file again after the edit.
- After a crash of KiCad, the lock file stays and the tools refuse the write. Open the file in KiCad and close it again, or delete the lock file.
- The schematic editor locks only the root schematic. To find the root schematic of a sub-sheet, the tools read the sheet hierarchy of the projects in the directory of the sub-sheet and in its parent directories. They cannot find the root schematic of a sub-sheet outside the directory tree of its project (for example `../common/power.kicad_sch`). Thus the tools do not see when KiCad has such a sub-sheet open.

**Recommendation**: Use KiCad GUI for design work. Use this MCP server for analysis, validation, and code generation.

## Troubleshooting

### "KiCad template not found" / "KiCad's Python (pcbnew) not found"

Make sure KiCad is installed at the standard path:
- **Windows**: `C:\Program Files\KiCad\<version>\`
- **macOS**: `/Applications/KiCad/`
- **Linux**: `/usr/share/kicad/`

`create_kicad_project` and `setup_pcb_layout` write boards with KiCad's own `pcbnew` module, using KiCad's bundled Python when the server runs under another interpreter. For a non-standard install, set `KICAD_PYTHON` to KiCad's python executable.

### "kicad-cli not found"

Add KiCad's bin directory to your PATH:
```bash
# Windows example
set PATH=%PATH%;C:\Program Files\KiCad\10.0\bin

# Linux/macOS example
export PATH="/usr/bin:$PATH"
```

### "pcbnew module not found" / PCB analysis is limited

pcbnew is only available in KiCad's bundled Python. Two options:

**Option A (recommended)**: Configure MCP to use KiCad Python — see [Installation](#recommended-install-into-kicad-python-full-pcb-analysis).

**Option B**: Accept text-only mode. You'll still get basic PCB data (footprints, track counts, net names) but without precise lengths or design rules.

### Python 3.14 install fails

Make sure you have `pip >= 26.0`:
```bash
pip install --upgrade pip
pip install -e .
```

## Resources

- [KiCad Documentation](https://docs.kicad.org/)
- [KiCad File Format Spec](https://dev-docs.kicad.org/en/file-formats/)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [kicad-cli Documentation](https://docs.kicad.org/en/cli/)

## Acknowledgments

Thanks to all contributors and community feedback:
- [@raffaeler](https://github.com/raffaeler) for KiCad 10 compatibility testing and feedback (#9)
- [@shivam5594](https://github.com/shivam5594) for Python 3.14 install issue report (#11)
- [@derekc00](https://github.com/derekc00) for lint fixes (#10)
- [@befedo](https://github.com/befedo) for SchematicComponent dataclass access bug report and patch (#13)

## License

MIT, see [LICENSE](LICENSE). KiCad Workbench started as a fork of
[Seeed-Studio/kicad-mcp-server](https://github.com/Seeed-Studio/kicad-mcp-server)
(MIT, copyright Seeed Studio). The history of this repository starts on 2026-11-04
with the state of that fork.
