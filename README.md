# Warning

Experimental, use at own risk, not affiliated with Ubisoft, EA, Blue Byte, Digicircus, Or any game publisher etc etc

# About

Implements online features to a local server process. Patches your Anno 2070 exe to point to a localhost server, which then exposes online functionality to you.

This is not fully tested so if you find features that don't work that should; or crashes, find out how to replicate the crash then provide me with what I need to debug it (copy of the save file, instructions on where the crash/feature is missing, and profile save if relevant)

Currently working (I think):

* Elections
* Ark storage (items and stuff)
* Profile and Career progression
* Event Quests

Not working:

* Multiplayer


> Status detail: the online session, ARK, elections/votes, world events and the
> profile are verified in game (a full session answers 25-75 service calls with
> zero failed replies). Multiplayer stays out of reach for the reason explained
> in `docs/known-issues.md` - it needs a client-side change, not a server one.

# Install

## The PLAY.bat build (recommended right now)

Download **`Anno2070Online-playbat-v0.1.0.zip`** from the release, extract it so
the folder sits inside your Anno 2070 folder, install Python 3.8+ once, then
double-click `Anno2070Online\PLAY.bat`. This is the build that is verified
working (3 consecutive clean launches). The .exe installer below is newer but its
launcher does not yet behave like PLAY.bat - use this zip until that is fixed.

```
...\Anno 2070\Anno5.exe
...\Anno 2070\Anno2070Online\PLAY.bat      <- double-click this
```

## The .exe build (experimental)

1. Install Anno 2070 (3.0.8051 / GOLDMASTER) as usual. (This is the latest steam release)
2. Download `Anno2070Online-Setup.exe` from the [latest release](../../releases/latest)
   and run it. It asks for your player name (and for the game folder only if it
   cannot find it).
3. Start the game from the new **"Anno 2070 - Local Online"** shortcut.

The installer puts a small loader DLL next to `Anno5.exe`, unpacks the runtime to
`%LOCALAPPDATA%\Anno2070Online`, and writes a `config.ini` you can edit (player
name, ports, options). The launcher starts the local server, launches the game,
retries the known 1-in-3 bootstrap crash, and prints a summary when you quit
(`Anno2070Online.exe --status` re-prints it).

The game's own files are **not modified**: the loader DLL applies everything at
run time and verifies the bytes it expects before touching anything.

# Uninstall

Same button for both builds - **`UNINSTALL.bat`** in the package folder, or
**`Uninstall-Anno2070Online.exe`** (also in the Start Menu) for the .exe build.
It undoes only what this project did:

* stops a launcher/local server that is still running (your game is never killed
  by force, and never your savegames)
* puts Ubisoft's own `upc_r2_loader64.dll` back from the
  `upc_r2_loader64.dll.original-backup` that INSTALL.bat keeps. If that backup is
  missing (an install from before 0.1.2, or a hand-copied DLL) it deletes our
  loader and tells you to use Steam's *Verify integrity of game files*. A DLL
  that is not ours is never touched.
* deletes our `upc_stub.log` from the game folder
* keeps your online state (`state.json`, `config.ini`) in
  `%LOCALAPPDATA%\Anno2070Online-backup`, then removes the folder and shortcuts

```
UNINSTALL.bat              ask, then do it (keeps your online state)
UNINSTALL.bat /purge       ... and throw the state away too
UNINSTALL.bat /yes         no questions
Uninstall-Anno2070Online.exe --dry-run    show what it would delete, change nothing
```

The game is back to its normal, offline behaviour afterwards. If a file is still
locked at the end, a virus scanner or the game is holding it: reboot and delete
the folder by hand.

# What is in this repository

```
PLAY.bat              start the server and the game (the working way in)
INSTALL.bat           one-time: put the loader next to Anno5.exe
UNINSTALL.bat         undo all of it again (keeps your online state)
config.ini            player name, game path, ports, options
packaging/            launcher_app.py, setup_app.py, uninstall_app.py, build-exes.ps1
                      (the exes are built from these)
server/               the backend: SDK HTTP, notification websocket, RV/RMC (83 methods),
                      PRUDP framing, conquest HTTP
loader/               our own upc_r2 client DLL (C source + prebuilt) - replaces the
                      Ubisoft DLL the game imports
docs/                 install, how it works, known issues, full protocol notes
tests/                offline suites, no game needed (tests/run_all.sh)
.github/workflows/    CI: build + attach the exe set to a release on every v* tag
```

# Build from source

```
powershell -ExecutionPolicy Bypass -File packaging\build-exes.ps1
```

Needs Python 3.8+ with pip; PyInstaller is installed by the script. The output is
`dist\Anno2070Online-Setup.exe` (installer) and `dist\Anno2070Online\` (portable).
The loader DLL can be rebuilt with `loader\build.ps1` (mingw-w64) or used prebuilt.

# How it works (short version)

The game imports `upc_r2_loader64.dll` (Ubisoft's retired client API) and talks to
Ubisoft web services. This project supplies both locally: our own implementation
of that API, and a Python server that speaks the exact protocols the game already
expects (Ubisoft SDK HTTP, the notification websocket, RendezVous/RMC, PRUDP and
Anno's own conquest PHP endpoints). Nothing in the game is patched on disk.

Details: `docs/how-it-works.md`. Protocol archaeology: `docs/protocol-notes.md`.

# Known issues

* About **1 launch in 3** hits a crash inside the game's own websocket code; the
  launcher retries automatically.
* **Multiplayer/conquest** cannot be reached yet - the client only reads its
  conquest address from a document it never populates from server data.
* Never end the game with Task Manager: it can truncate `Config\Engine.ini`.

Full list: `docs/known-issues.md`.

# Credits and licence

Code: MIT (see `LICENSE`). Not affiliated with, endorsed by or supported by
Ubisoft, Related Designs or Blue Byte. This repository contains none of their
code, data or assets.
