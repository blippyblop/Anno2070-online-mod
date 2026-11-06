# Install, in detail

## The easy way (recommended)

Run **`Anno2070Online-Setup.exe`**. It asks for your player name (and for the
game folder only if it cannot find it), then installs everything into
`%LOCALAPPDATA%\Anno2070Online`, copies the loader next to `Anno5.exe`, and adds
a Desktop + Start Menu shortcut. Start the game from that shortcut from then on.
No Python is required.

## The manual way (developers / troubleshooting)

## Requirements

| | |
|---|---|
| Anno 2070 | build 3.0.8051 (GOLDMASTER). Steam, Ubisoft or retail install — all use the same `Anno5.exe`. |
| Windows | 10 or 11, 64-bit (the game is 64-bit). |
| Python | 3.8 or newer, 64-bit, "Add to PATH" ticked. The server is plain Python, no packages to `pip install`. |
| Disk | ~10 MB for this folder. |

## Steps

1. **Edit `config.ini`.** The only mandatory line is your name:

   ```ini
   [player]
   name = Captain
   ```

   If Anno 2070 lives somewhere unusual, add its folder:

   ```ini
   [game]
   path = D:\Games\Anno 2070
   ```

2. **Run `INSTALL.bat`.** It finds the game, prints the SHA-256 of `Anno5.exe`
   (so you can tell which build you are on), copies
   `loader\bin\upc_r2_loader64.dll` next to `Anno5.exe`, and checks Python.
   If the game folder is protected, run it as administrator.

3. **Run `PLAY.bat`.** It starts the server in a minimised console window,
   waits for the port, starts the game once, and tells you if the client
   dropped its online login during bootstrap (run it again - it normally comes
   up on the first or second try).

4. Play. When you finish, quit from the game's own menu, then close the
   "Anno 2070 Local Server" window.

## What got written to disk

| path | what |
|---|---|
| `<game>\upc_r2_loader64.dll` | our loader — the only file placed in the game folder |
| `<game>\upc_r2_loader64.dll.original-backup` | Ubisoft's original loader, kept so UNINSTALL can put it back |
| `logs\server.log` | protocol log of the last session |
| `logs\server.prev.log` | the session before that |
| `server\state.json` | your saved online state: ARK items, elections, world events |

Nothing else. No registry keys, no services, no scheduled tasks, no firewall
rules.

## Uninstall

Run **`UNINSTALL.bat`** (PLAY.bat build) or **`Uninstall-Anno2070Online.exe`**
(.exe build, also in the Start Menu), then confirm. It undoes only what this
project did:

1. stops a launcher or a local server that is still running (Anno 2070 itself is
   never killed - a forced kill can truncate `Config\Engine.ini`)
2. puts Ubisoft's original `upc_r2_loader64.dll` back from
   `upc_r2_loader64.dll.original-backup`, which INSTALL.bat keeps before it
   copies ours. If that backup does not exist (an install from before 0.1.2, or
   a manual copy), our loader is deleted and you are told how to restore the
   original: Steam > right click Anno 2070 > Properties > Installed files >
   Verify integrity of game files. A DLL that is not ours is never touched.
3. deletes `upc_stub.log` from the game folder
4. keeps your online state in `%LOCALAPPDATA%\Anno2070Online-backup`, then
   deletes the runtime folder and the shortcuts

Your savegames are never touched - they are the game's own files.

| uninstall everything, state included | `UNINSTALL.bat /purge` or `Uninstall-Anno2070Online.exe --purge` |
| see what it would do, change nothing | `Uninstall-Anno2070Online.exe --dry-run` |
| no questions (scripted) | `UNINSTALL.bat /yes` or `Uninstall-Anno2070Online.exe --yes` |

Troubleshooting: it refuses to run while `Anno5.exe` is up (quit from the game's
menu first). A file still locked afterwards means a virus scanner or the game is
holding it - reboot and delete the folder by hand.

## Changing ports or address

`config.ini` can change the ports, but the game is told the address by the
loader, which currently assumes `127.0.0.1:8443` (Ubisoft SDK), `127.0.0.1:3091`
(PRUDP) and `127.0.0.1:3080` (conquest). Changing them means editing
`loader/src/upc_stub.c` (`urlmap_inject`) and rebuilding — see
`loader/README.md`. Running the server on another machine on your LAN is
possible in principle but needs the same edit plus firewall work; it is not
supported yet.

## Verifying it is working

Run `PROGRESS.bat` right after a session. A healthy session shows
`RV calls answered : 25+` and `failed replies : 0`, `ARK item box : YES`,
`senate elections : YES`.

Inside the game: the top-left corner should say the online state instead of
"Offline mode", the ARK item box should contain six items, and the senate /
World Council panels should each list an election you can vote in.
