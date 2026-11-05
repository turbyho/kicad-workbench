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

The two servers use the same convention for the fields of a symbol:

| Field | Content |
|---|---|
| `Value` | The general value and the orderable MPNs in one text in parentheses, the fitted part first: `2N7002 (2N7002LT1G, BSS138LT1G)`. The separators are `,` and `;`. An MPN with `,`, `;` or a parenthesis is in quotes, as in CSV: `("BC847B,215", BSS138LT1G)` |
| `MPN` | The confirmed copy of the MPNs of `Value` |
| `Value_Confirmed` | A hidden copy of `Value` at the last confirmation |

A general value with no MPN (`10k`) has no selected part. `MPN` = `-` tells that the
symbol needs no part (for example a test point). The units of one package (`U1A` and
`U1B`) must have the same `Value`, `MPN` and `Value_Confirmed`: `export_bom` gives an
error when they do not.

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
4. **Confirm the MPNs.** `project_check` gives the changes of `Value` that the user
   must confirm, and the fields that complete a symbol by the convention. The agent
   asks the user. After the confirmation, `set_symbol_fields` writes `MPN`,
   `Value_Confirmed` (and `Value` to complete a symbol) into the schematic, as KiCad
   writes them. KiCad must not have the schematic open.
5. **Do the check again after each change.** After a change of the parts of the
   schematic (for example `add_component_from_library` or a new MPN), run steps 1
   and 2 again.

`project_availability` of datasheet-lens then gives the parts that cannot be ordered
or have no stock.

### Without datasheet-lens

`check_symbol_fields` of kicad-workbench does the check of step 4 with the same model
and the same messages as `project_check`. It reads the BOM of kicad-cli of the
default variant and of each variant. It does not check the datasheets.

## Checks against the datasheet

datasheet-lens gives the original pages of a datasheet as images (300 to 600 dpi),
not a text conversion. A conversion can lose signs, units, subscripts and the
structure of tables. The agent reads each value from the original page, and saves it
as a fact with its page.

| Work | kicad-workbench gives | datasheet-lens gives |
|---|---|---|
| Pins of a symbol | `get_library_symbol`, `check_pin_pad_mapping`, `analyze_pin_functions`, `trace_netlist_connection` | The pin table of the datasheet |
| A footprint | `get_library_footprint` (pads, size, courtyard), `render_library_item` (images) | The package drawing |
| The circuit | Nets, ERC, the pins and the values of the symbols | Absolute maximum ratings, logic levels, the application circuit |
| A part with no library item | `search_library_symbols`, `search_library_footprints` | Search links for symbols, footprints and 3D models |
