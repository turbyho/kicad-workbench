# Datasheets of a project with datasheet-lens

[datasheet-lens](https://github.com/turbyho/datasheet-lens) is an MCP server for the
datasheets of electronic components. It keeps each datasheet in a central store, and
the project file `datasheet-lens.toml` in the repository of a design holds the parts
of the design and their datasheets. It does not read schematics: the parts come from
the CAD tool. kicad-workbench gives these parts, and it does not read datasheets.
With the two servers, the agent can:

- Keep the datasheets of a design equal to its BOM, in the repository, the same for
  each person of the team.
- Keep the MPN fields of the schematic equal to the parts that the user confirmed.
- Check the design against the original datasheets.

## The convention of the MPN fields

The two servers use the same convention for the fields of a symbol. Each field has
one function:

| Field | Content |
|---|---|
| `Value` | The general value: `2N7002`, `100nF 10V`. The check reads no MPN from it |
| `MPN` | The orderable MPNs of the designer, the fitted part first: `2N7002LT1G, BSS138LT1G`. The separators are `,` and `;`. An MPN with `,` or `;` is in quotes, as in CSV: `"BC847B,215", BSS138LT1G` |
| `Value_Confirmed` | A hidden copy of `Value` at the last confirmation |
| `MPN_Confirmed` | A hidden copy of `MPN` at the last confirmation |

A difference between a field and its confirmed copy is a change that the user must
confirm. Until the user confirms it, the confirmed MPNs are the parts of the design.
A new general value with the confirmed MPN (`100nF` to `220nF`) can need a new part:
the user selects a new part, or confirms that the part stays. A new general value
with an MPN that the user never confirmed is a change of its own.

A general value with no MPN (`10k`) has no selected part. `MPN` = `-` tells that the
symbol needs no part (for example a test point). The units of one package (`U1A` and
`U1B`) must have the same `Value`, `MPN`, `Value_Confirmed` and `MPN_Confirmed`.
When they do not, the answer of `export_bom` starts with an error of the design. The
BOM and the CSV file are still written. Their line of the package has the values of
the units joined with a comma (KiCad 9 and 10: the joined MPNs look like an MPN and
its alternative), or `-- mixed values --` (KiCad 8).

The old convention put the MPNs in the parentheses of `Value`
(`2N7002 (2N7002LT1G)`). For a symbol of the old convention (no `MPN_Confirmed`),
the check proposes the move into the four fields. The move keeps the state of the
confirmation: a confirmed symbol stays confirmed. When only `Value_Confirmed` has the
parentheses (the MPNs were removed from `Value` in KiCad), the move writes only the
confirmed copies. The move writes a new field `MPN` as KiCad adds a new field:
hidden in a file of KiCad 9 and 10 (show it in KiCad, for example in the Symbol Fields
Table), visible in a file of KiCad 8.

## The procedure

1. **Make the BOM.** `export_bom` with `output_path` and with no `group_by` gives one
   line for each symbol, as a CSV file of kicad-cli. For a project with variants
   (KiCad 10), also give `all_variants`: the tool makes one CSV file for each variant
   and names the files.
2. **Compare the BOM with the project file.** `project_check` of datasheet-lens with
   the argument `bom` (the CSV file, or the list of files) gives the parts that are
   missing, extra or different, and the next step for each part.
3. **Add the datasheets.** For each missing part, datasheet-lens finds the datasheet
   in its store, or adds it from the URL of the `Datasheet` field, a distributor or a
   local PDF. `project_sync` copies the documents into the project folder. Then
   `project_check` must give no missing, extra or different part.
4. **Confirm the MPNs.** `project_check` gives the changes of `Value` and `MPN` that
   the user must confirm, and the moves of the old convention. For a new MPN, the
   agent adds its datasheet first (this changes no project), then asks the user.
   After the confirmation, `set_symbol_fields` writes `MPN_Confirmed` and
   `Value_Confirmed` (for a move also `Value` and `MPN`) into the schematic, as KiCad
   writes them, and datasheet-lens updates the project file. KiCad must not have the
   schematic open.
5. **Do the check again after each change.** After a change of the parts of the
   schematic (for example `add_component_from_library` or a new MPN), run steps 1
   and 2 again.

`project_availability` of datasheet-lens then gives the parts that cannot be ordered
or have no stock.

### Without datasheet-lens

`check_symbol_fields` of kicad-workbench does the check of step 4 with the same model
and the same messages as `project_check` of a datasheet-lens with the model of the four
fields (2026-11-07 or later). `check_symbol_fields` reads the BOM of kicad-cli of the
default variant and of each variant. It does not check the datasheets. An older
datasheet-lens reads the MPNs from the parentheses of `Value`.

## Checks against the datasheet

datasheet-lens gives the original pages of a datasheet as images (300 to 600 dpi),
not a text conversion. A conversion can lose signs, units, subscripts and the
structure of tables. The agent reads each value from the original page, and saves it
as a fact with its page.

| Work | kicad-workbench gives | datasheet-lens gives |
|---|---|---|
| Pins of a symbol | `get_library_symbol`, `check_pin_pad_mapping`, `get_symbol_details`, `trace_netlist_connection` | The pin table of the datasheet |
| A footprint | `get_library_footprint` (pads, size, courtyard), `render_library_item` (images) | The package drawing |
| The circuit | Nets, ERC, the pins and the values of the symbols | Absolute maximum ratings, logic levels, the application circuit |
| A part with no library item | `search_library_symbols`, `search_library_footprints` | Search links for symbols, footprints and 3D models |
