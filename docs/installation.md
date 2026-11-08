# Installation

## Requirements

- KiCad 8, 9 or 10, with kicad-cli. The server finds kicad-cli in this order: the
  path in the environment variable `KICAD_CLI`, then `PATH`, then the standard install
  folder of KiCad (see "kicad-cli not found"). A `KICAD_CLI` with a path that is not a
  file is an error: the server does not use a different kicad-cli then. With no
  kicad-cli, each tool that works with KiCad files gives an error that tells the user
  to install KiCad 8 or later, also when the server process can import `pcbnew`. When
  kicad-cli is there but does not give its version, the error gives the reason. A
  KiCad before KiCad 8, and a KiCad version that the server has no rules for (for
  example a later major version, or a nightly build other than 10.99), give an error.
  Only the tools of the parts registry and the tools that read a netlist file work
  with no KiCad.
- Python 3.10 or later.
- A Python that can import the module `pcbnew` of KiCad, for the board tools
  (`list_pcb_footprints`, `get_pcb_statistics`, `find_tracks_by_net`,
  `analyze_pcb_nets`, `analyze_pcb_signal_integrity`, `analyze_pcb_power_integrity`).
  These tools import `pcbnew` in the server process. Without it (but with KiCad
  installed), they read the board file as text, and their answer has a warning with
  the limits: `get_pcb_statistics` gives no board outline and no design rules,
  `analyze_pcb_nets` and `analyze_pcb_signal_integrity` give no design rules, and
  `analyze_pcb_power_integrity` gives no fill state of the zones. When the server
  process finds `pcbnew` but cannot import it (for example a build for an other
  Python), the board tools give an error, not a text result.

Other tools also use `pcbnew`: `get_library_footprint`, `check_pin_pad_mapping`,
`check_schematic_pin_pad_mapping`, `check_library_links` and
`setup_pcb_layout`. They run it in a different process, with the Python of KiCad that
the server finds: the Python of the installation of kicad-cli (Windows: `python.exe`
next to kicad-cli; macOS: the Python of the application bundle; Linux: `python3` next
to a `<prefix>/bin/kicad-cli`, for example `/usr/bin/python3`). The environment
variable `KICAD_PYTHON` gives a different Python, and a path that is not a file is an
error. Each `pcbnew` must be of the KiCad version (major and minor) of kicad-cli: else
the tool gives an error that names the two versions.

Thus the best place for the server is a Python that can import `pcbnew`.

## Linux

The KiCad package of a Linux distribution installs `pcbnew` for the system Python.
Do a check:

```sh
/usr/bin/python3 -c "import pcbnew; print(pcbnew.Version())"
```

Install the server with [pipx](https://pipx.pypa.io/) into a virtual environment of
the system Python. The option `--system-site-packages` lets the environment import
`pcbnew`:

```sh
pipx install --python /usr/bin/python3 --system-site-packages kicad-workbench
```

pipx puts the command `kicad-workbench` into `~/.local/bin`. We tested this procedure
with a wheel of version 0.1.0, KiCad 10.0.6 and Python 3.14 (Manjaro).

Without pipx, make the virtual environment yourself:

```sh
/usr/bin/python3 -m venv --system-site-packages ~/.kicad-workbench/.venv
~/.kicad-workbench/.venv/bin/pip install kicad-workbench
```

The command is then `~/.kicad-workbench/.venv/bin/kicad-workbench`.

## Windows

We did not test this procedure on Windows yet.

KiCad has its own Python with `pcbnew`. It must be Python 3.10 or later:

```bat
"C:\Program Files\KiCad\10.0\bin\python.exe" --version
```

Install the server into that Python, for the user only:

```bat
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip install --user kicad-workbench
```

Change `10.0` to the version of your KiCad. The MCP client starts the server with
this Python and the arguments `-m kicad_workbench`.

## macOS

We did not test this procedure on macOS yet.

KiCad has its own Python with `pcbnew`. It must be Python 3.10 or later:

```sh
KIPYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
"$KIPYTHON" --version
"$KIPYTHON" -m pip install --user kicad-workbench
```

Use `--user`. Without it, pip writes into the KiCad application, and macOS can then
refuse the signature of the application. The MCP client starts the server with this
Python and the arguments `-m kicad_workbench`.

## A Python with no pcbnew

All tools work, except that the board tools read the board file as text (see
"Requirements"):

```sh
pipx install kicad-workbench
```

## MCP clients

The server is an MCP server on stdio. The examples use the name `kicad`: the tools
are then `mcp__kicad__<tool>` in Claude Code.

### Claude Code

```sh
# Linux (pipx)
claude mcp add --scope user kicad -- kicad-workbench

# Windows (the Python of KiCad)
claude mcp add --scope user kicad -- "C:\Program Files\KiCad\10.0\bin\python.exe" -m kicad_workbench

# macOS (the Python of KiCad)
claude mcp add --scope user kicad -- /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 -m kicad_workbench
```

### Claude Desktop

The configuration file:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Claude Desktop does not always have the `PATH` of your shell. Give the full path of
the command:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/home/USER/.local/bin/kicad-workbench",
      "args": []
    }
  }
}
```

On Windows and macOS, the command is the Python of KiCad, and `args` is
`["-m", "kicad_workbench"]`.

### Other MCP clients

Set the command `kicad-workbench` with no arguments, or the Python of KiCad with the
arguments `-m kicad_workbench`. Restart the client after a change of its
configuration.

## Environment variables

Set them in the MCP configuration of the server (for example `env` in the JSON file
of Claude Desktop).

| Variable | Effect |
|---|---|
| `KICAD_CLI` | The path of kicad-cli, when the server does not find it, or to select one of more KiCad installations (see "More than one KiCad") |
| `KICAD_STOCK_DATA_HOME` | Windows: the folder of the stock libraries, as for KiCad |
| `KICAD_DOCUMENTS_HOME` | The base of the documents folder of KiCad, as for KiCad: the server adds `kicad/<major>.<minor>` (`KiCad/<major>.<minor>` on Windows and macOS). The documents folder gives the default of `KICAD_USER_TEMPLATE_DIR` and `KICAD<major>_3RD_PARTY` |
| `KICAD_PYTHON` | The Python with `pcbnew` for the tools that run it in a different process |
| `KICAD_WORKBENCH_NO_UPDATE_CHECK` | A value other than empty and `0` stops the request to PyPI (see "Release notices") |
| `PARTS_REGISTRY_URL` | The API of the parts registry (default `https://partreel.com/api/v1`) |
| `PARTS_REGISTRY_ASSET_HOSTS` | More hosts (comma list) from which `download_registry_part` can download files |
| `PARTS_REGISTRY_SAVE_ROOTS` | More folders (comma list) into which `download_registry_part` can save files (default: the working folder and the home folder) |

The server reads the KiCad settings (library tables, path variables) from the
settings folder of the installed KiCad. `KICAD_CONFIG_HOME` changes that folder, as
for KiCad.

## More than one KiCad

The server uses one KiCad installation: the kicad-cli of `KICAD_CLI`, else the
kicad-cli of `PATH`, else the kicad-cli of the standard install folder (on Windows the
newest version folder that has kicad-cli). The version of this kicad-cli gives the
settings folder (for
example `~/.config/kicad/10.0`) and the format of the files that the server writes.
Its installation gives the stock libraries, that is the default value of
`KICAD<major>_SYMBOL_DIR`, `KICAD<major>_FOOTPRINT_DIR`, `KICAD<major>_3DMODEL_DIR`,
`KICAD<major>_TEMPLATE_DIR` and `KICAD<major>_DESIGN_BLOCK_DIR`. The server finds
the folder of the stock libraries as KiCad finds it:

| System | Folder of the stock libraries |
|---|---|
| Windows | `KICAD_STOCK_DATA_HOME`, else `share\kicad` in the folder above the folder of kicad-cli (`C:\Program Files\KiCad\<version>\share\kicad`) |
| macOS | `Contents/SharedSupport` of the application bundle of kicad-cli |
| Linux | The build of KiCad sets this folder, and kicad-cli does not give it. For a kicad-cli with the name `kicad-cli` in a folder `bin`, the server uses the folder `share/kicad` next to `bin` (the default of the build) when it has the folder `template`. For an other kicad-cli the folder is not known |

Thus on Windows and macOS, `KICAD_CLI` alone selects the installation. On Linux, a
second installation (for example the package `kicad-nightly`, with the start script
`/usr/bin/kicad-cli-nightly` and the data in `/usr/share/kicad-nightly`) also needs
the path variables of its stock libraries. The server takes the value of a path
variable as KiCad does: from the environment of the server, else from
`environment.vars` of `kicad_common.json` in the settings folder (the path variables
that the user set in KiCad, Preferences > Configure Paths), else from the default of
the installation. In the environment:

```json
"env": {
  "KICAD_CLI": "/usr/bin/kicad-cli-nightly",
  "KICAD10_SYMBOL_DIR": "/usr/share/kicad-nightly/symbols",
  "KICAD10_FOOTPRINT_DIR": "/usr/share/kicad-nightly/footprints",
  "KICAD10_3DMODEL_DIR": "/usr/share/kicad-nightly/3dmodels",
  "KICAD10_TEMPLATE_DIR": "/usr/share/kicad-nightly/template",
  "KICAD10_DESIGN_BLOCK_DIR": "/usr/share/kicad-nightly/blocks"
}
```

When the folder is not known and a variable has no value, the library tools tell
it. Use the KiCad installation that you open the files with: a newer KiCad converts
the files of an older version when it saves them, and an older KiCad does not open
the files of a newer version.

`KICAD_PYTHON` and the Python of the server select the `pcbnew` of the board tools
(see "pcbnew not found"). Set them to the same installation as kicad-cli: a `pcbnew` of
an other KiCad version gives an error.

## Release notices

The server tells the agent about a newer release on PyPI, and about an upgrade while
the server runs. The agent gives each notice to you one time in a session:

- **Newer release.** When the server starts, a background thread gets the newest
  version from PyPI. When the last request that succeeded is younger than 24 hours,
  the server sends no request. The tools read only the stored result, thus no tool
  waits for the network.
- **Restart.** The installed package has a different version than the running
  server. The MCP client starts the server, thus a restart of the client starts the
  new code.

To stop the request to PyPI, set `KICAD_WORKBENCH_NO_UPDATE_CHECK=1`. An editable
install (`pip install -e`) and a source tree in `PYTHONPATH` get no notices.

A defect of the check does not stop a tool or the server. The agent gets the line
"The version check of kicad-workbench failed (...)" one time for each different
defect, and the log gets the details. A cache folder that the server cannot write is
such a defect: the server then sends a request at each start.

## Check the installation

Restart the MCP client, and ask the agent:

> Which KiCad tools do you have?

Then a check of `pcbnew`:

> Analyze the nets of `MyBoard.kicad_pcb`.

When the answer has the warning "pcbnew not available", the server reads the board as
text. Install the server into a Python with `pcbnew`.

## Troubleshooting

### kicad-cli not found

The tools give the error "KiCad is not installed". Install KiCad in its standard
folder, add the folder of kicad-cli to `PATH`, or set `KICAD_CLI`. The standard
folders that the server looks in after `PATH`:

- Windows: `%ProgramFiles%\KiCad\<version>\bin\kicad-cli.exe` (the newest version
  folder that has kicad-cli; `%ProgramFiles%` is normally `C:\Program Files`)
- macOS: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`
- Linux: no standard folder. The package of the distribution puts kicad-cli into
  `PATH` (for example `/usr/bin/kicad-cli`); for an other place, set `KICAD_CLI`.

### pcbnew not found

The board tools import `pcbnew` in the server process: install the server into a
Python with `pcbnew` (see above). For the tools that run `pcbnew` in a different
process (the library footprints, the pin and pad checks, the library links, new
boards), set `KICAD_PYTHON` to the Python of KiCad.

### An edit tool refuses the write

KiCad has the file open. KiCad does not read a change to a file that it has open, and
its next save removes the change. Close the schematic editor or the PCB editor, and
open the file again after the edit. After a crash of KiCad, the lock file
`~<file name>.lck` stays next to the file: open the file in KiCad and close it again,
or delete the lock file. See [editing.md](editing.md).

## Upgrade

```sh
pipx upgrade kicad-workbench
```

With the Python of KiCad, use `-m pip install --user --upgrade kicad-workbench`.
Then restart the MCP client: the client starts the server, and the running server
keeps the old code.

## Uninstall

1. Remove the MCP server from the client. For Claude Code:

   ```sh
   claude mcp remove --scope user kicad
   ```

2. Remove the package:

   ```sh
   pipx uninstall kicad-workbench
   ```

3. Remove the cache folder of the server (the index of the libraries and the state
   of the release check): `~/.cache/kicad-workbench` on Linux,
   `~/Library/Caches/kicad-workbench` on macOS,
   `%LOCALAPPDATA%\kicad-workbench` on Windows.
