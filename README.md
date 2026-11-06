# Anno 2070 — Local Online

A local replacement for the **shut-down Anno 2070 online backend**. It brings back
the parts of the game that died with Ubisoft's servers:

* the **ARK item box**
* **senate elections** and votes (World Council too)
* **world events**
* your **profile**, chat, friends and mail plumbing
* the **notification** channel the game's SDK expects

It runs entirely on your own machine. No Ubisoft account, no internet, no
copy of the game's code is included or modified.

> **Status:** the online session, ARK, elections/votes, world events and profile
> are working and verified in game. The **conquest / multiplayer** layer is not
> reachable yet — see `docs/known-issues.md`. The project is a community
> interoperability effort, not affiliated with Ubisoft or Related Designs.

---

## Install

1. Install Anno 2070 (3.0.8051 / GOLDMASTER) as usual.
2. Install **Python 3.8+** (python.org, tick "Add to PATH").
3. Edit `config.ini` — at minimum, set your name:

   ```ini
   [player]
   name = Captain
   ```

   If the game is not in a usual Steam/Ubisoft folder, set `[game] path` too.
4. Run **`INSTALL.bat`** (copies the loader next to `Anno5.exe`, checks Python).
5. Run **`PLAY.bat`**. That is the whole interface: it starts the local server,
   launches the game, and retries if the client hits its known bootstrap crash.
6. **`PROGRESS.bat`** any time, for a summary of the last session.

Nothing is installed system-wide, no service is registered, and nothing runs at
startup. Delete the folder and the loader DLL to uninstall.

## What is in this repo

```
config.ini            the only file you need to edit (player name, paths, ports)
INSTALL.bat           one-time setup
PLAY.bat              start the server + the game (double-click this)
PROGRESS.bat          what the last session achieved
loader/               our own UPC replacement DLL (source + prebuilt)
server/               the backend: HTTP, websocket, RMC, PRUDP, conquest HTTP
docs/                 how it works, install details, known issues, protocol notes
tests/                offline test suites (no game needed)
```

## How it works (short version)

Anno 2070 talks to Ubisoft's `upc_r2` client DLL and to a set of Ubisoft web
services. Ubisoft shut both down. This package supplies:

* **`loader/upc_r2_loader64.dll`** — our own implementation of the `upc_r2` API
  the game imports, so the game gets the profile it expects instead of a missing
  DLL. It also applies a few small *in-memory* fixes so the game's online
  bootstrap does not deadlock.
* **`server/`** — a local copy of the web services: the Ubisoft-SDK HTTP API
  (`/v3/...`), the notification websocket, the RendezVous/RMC protocol over that
  websocket, PRUDP framing, and the conquest HTTP endpoint.

The game's own files are not modified. All of the behaviour lives in this
folder, and everything that talks to the game speaks the protocols the game
already expects — see `docs/how-it-works.md` and `server/PROTOCOL-NOTES.md`.

## Known issues

* About **1 in 3 launches**, the client crashes during its online bootstrap
  (a lifetime bug in its own websocket code). `PLAY.bat` retries automatically.
* **Conquest / multiplayer** is not reachable: the client only reads the
  conquest server address from a config document it never populates from server
  data. Documented in full in `docs/known-issues.md`.
* If the game crashes hard (Task Manager / `taskkill`), `Config\Engine.ini` can
  be truncated and the next launch fails. Delete
  `%APPDATA%\Ubisoft\ANNO 2070\Config` and let the game recreate it.

## Credits and licence

* Code in this repository: **MIT** (see `LICENSE`).
* Not affiliated with, endorsed by, or supported by Ubisoft, Related Designs or
  Blue Byte. Anno 2070 is their property; this project contains none of their
  code, data or assets.
