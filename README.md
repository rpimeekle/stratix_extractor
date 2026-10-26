# Stratix Extractor

A Windows desktop tool that connects to Rockwell Automation **Stratix 5200, 5700 and 8000**
managed switches over SSH, one after another, and pulls switch details and full port status
into a single Excel workbook.

Port descriptions are taken from `show interfaces`, so they are the **complete** configured
descriptions, not the 18-character versions that `show interfaces status` truncates to.

## Download

Get `StratixExtractor.exe` from the [Releases](https://github.com/rpimeekle/stratix_extractor/releases)
page (or from the artifacts of the latest **Test and build** run under Actions). It is a single
file with no install and no Python needed.

## Using it

1. **Switch list** - click *Browse…* and pick a CSV or Excel file listing the switches
   (*New template…* writes one to fill in). The switches appear in the list, all ticked.
2. **Default credentials** - if every switch uses the same login, leave the username and
   password blank in the file and enter them once here. Tick *Use these for every switch* to
   ignore any credentials in the file. An enable secret is only needed if the login lands at
   the `>` prompt.
3. **Choose switches** - click a row to tick or untick it. *Select all* ticks everything;
   *Reset* clears the whole selection (and the previous results).
4. **Output spreadsheet** - choose where the `.xlsx` goes. The default name is
   `stratix_extract_YYYYMMDD_HHMM.xlsx`.
5. **Go** - each ticked switch is connected to, read and disconnected in turn, with a
   timestamped log of every step. *Stop* finishes the current switch and then writes what has
   been collected so far.

When the run finishes the workbook is re-opened and checked, and a dialog confirms how many
switches succeeded, where the file was saved and how many rows each sheet holds, with an
option to open it.

A switch that fails (wrong password, unreachable, SSH disabled) is recorded with the reason and
the run carries on with the next one.

### Switch list format

CSV or `.xlsx` (first sheet). Header names are case-insensitive; only `ip` is required.

| Column | Notes |
|---|---|
| `hostname` | Name to show in the list and report (defaults to the IP) |
| `ip` | IP address or DNS name |
| `username` | Blank = use the default credentials |
| `password` | Blank = use the default credentials |
| `enable_secret` | Optional |
| `port` | SSH port, default 22 |

Rows starting with `#` are skipped. Common alternative headers such as `IP Address`,
`Host Name` and `User` are recognised. See [`examples/switches_example.csv`](examples/switches_example.csv).

> Passwords in the switch list are plain text. Keep the file somewhere private, or leave the
> password columns blank and type the credentials into the program. The program never saves
> passwords; it only remembers file locations, the username and options.

### What the workbook contains

| Sheet | One row per | Columns include |
|---|---|---|
| Summary | switch attempted | Success/failed, reason, warnings, port count, time taken; run start/finish |
| Switches | switch | Family (5200/5700/8000), model, serial, firmware, image, uptime, last reload, base MAC, management IPs, port counts by state |
| Ports | port | **Full description**, status (connected/notconnect/disabled/err-disabled), link/protocol, mode, VLAN, access/native/allowed/voice VLANs, duplex, speed, media, MAC, MTU, last input/output, packet and error counters (input, CRC, output, collisions, resets), CDP/LLDP neighbour on that port |
| Inventory | inventory item | Chassis, modules, SFPs: name, description, PID, VID, serial |
| VLANs | VLAN | ID, name, status, member ports |
| Neighbours | CDP/LLDP neighbour | Local port, neighbour name, port, IP, platform, chassis ID, description |

Every sheet has filters and a frozen header row; port status cells are coloured.

### Commands run on each switch

`show version`, `show inventory`, `show interfaces`, `show interfaces status`,
`show interfaces switchport`, `show ip interface brief`, `show vlan brief`,
`show cdp neighbors detail`, `show lldp neighbors detail` — all read-only. Nothing is
changed on the switch. Tick *Also save raw command output* to keep the text of each command
in a `_raw` folder next to the spreadsheet, which is useful for checking anything that looks
wrong.

## Troubleshooting

- **Authentication failed** - check the username/password, and whether the switch uses local
  accounts or RADIUS/TACACS.
- **No SSH response** - check the IP, that SSH is enabled (`ip ssh version 2`, `transport input ssh`)
  and that nothing blocks TCP 22. Raise *SSH timeout* for slow links.
- **Logged in at user EXEC level** - add an enable secret, or give the account privilege 15.
- **Spreadsheet locked** - if the output file is open in Excel when the run finishes, the
  workbook is saved with a time suffix instead and the log says where.

## Building from source

```
python -m pip install -r requirements-dev.txt
python -m pytest
python run_stratix_extractor.py              # run the GUI from source
pyinstaller --noconfirm StratixExtractor.spec # builds dist\StratixExtractor.exe
```

GitHub Actions runs the tests (including a GUI test driving the real window against recorded
Stratix output) and builds the `.exe` on Windows for every push. Pushing a tag such as
`v1.0.1` also publishes a release with the `.exe` and a zip attached. Update `__version__` in
`stratix_extractor/__init__.py` first.

## Project layout

```
stratix_extractor/
  gui.py           Tkinter window
  runner.py        runs the selected switches in turn, then writes and checks the workbook
  collector.py     SSH session per switch (Netmiko) and assembly of the rows
  parsers.py       parsers for each show command (IOS and IOS-XE)
  excel_writer.py  workbook layout and verification
  switchlist.py    CSV/XLSX switch list loading and credential handling
tests/             recorded Stratix 5700 / 5200 output and tests
```
