# Known issues and limits

Honest list, newest first. Symptoms are described the way you will see them, not
the way the code sees them.

## 1. The game sometimes crashes during startup (about 1 launch in 3)

**Symptom:** the game window appears and disappears during the first minute, or
it shows "the connection to the server has been lost" and exits.

**Cause:** a lifetime bug in the game's own websocket client. When a websocket
event arrives before its endpoint finished attaching, it jumps through a null
pointer (`Anno5.exe+0x1362214`). The server cannot prevent this; it is a race
inside the client.

**What this package does:** `PLAY.bat` waits 100 s and relaunches the game up to
three times. In practice a session starts within one or two tries.

## 2. Conquest and multiplayer are not reachable yet

**Symptom:** the multiplayer/lobby paths in the game either do nothing or fail.

**Cause:** Anno reads its conquest server address from one specific document
inside the SDK config store — and that document is only ever filled by the
client's own notification handler, which requires section values the client's
parser cannot produce (`AnnoURLs` is dropped, and the value type it wants does
not match what the notification path can store). This was proven exhaustively:

* the document is written **only** by the notification path (`FUN_140f4d940`);
  `/parameters` responses and the `OnlineConfiguration` entity never touch it;
* with both staging gates force-patched, the filler runs and still stores
  nothing usable;
* therefore no server payload can make the conquest address non-empty.

Everything behind that address **is** implemented and tested offline: the 24
`ConquestServer/*.php` endpoints, save upload/download, session lists, and the
matchmaking reply shapes. If the address ever becomes reachable (a client patch,
a future build), the server side is already there.

## 3. Never hard-kill the game

`taskkill /f /im Anno5.exe` (or Task Manager → End task) can truncate
`%APPDATA%\Ubisoft\ANNO 2070\Config\Engine.ini`, after which every launch fails.
Recovery: delete that `Config` folder and let the game recreate it. Quit from the
game's own menu instead.

## 4. "Offline mode" appears and nothing else happens

The session did not complete. Run `PROGRESS.bat`:

* `RV calls answered : 0-5` → the client died early: run `PLAY.bat` again.
* `RV calls answered : 25+` but the game says offline → look at
  `logs\server.log` around the login, and please open an issue with that file
  (`logs\server.log`, it contains no personal data beyond the player name).

## 5. The name in game does not change

`config.ini` sets the name the **server** reports. An Anno profile that already
exists keeps the name the game itself stored for it (the game writes its own
profile files under `Documents\ANNO 2070`). Changing `[player] name` affects new
profiles, chat, mail and the online display name; to rename an existing local
profile, rename it in the game.

## 6. Ports are fixed

The loader points the game at `127.0.0.1` on ports 8443 (HTTPS/WS), 3091
(PRUDP) and 3080 (conquest). `config.ini` can move the server, but the loader
must be rebuilt to match. 8443/3080/3091 must be free when you start.

## 7. Not redistributed on purpose

Some things this project used during development are deliberately **not** in
this repository, and the code that needs them degrades gracefully:

| not shipped | why | effect |
|---|---|---|
| the 42 MB real 2015 server log | Ubisoft's data | `tests/replay_conquest.py` prints SKIP |
| Ghidra decompilation dumps | derivative of the game binary | none at runtime; the findings live in `docs/protocol-notes.md` |
| the full ARK item table (821 ids + names) | the names are the game's text | only the six starter ids ship (ids are facts, names neutralised) |

Write your own extractor against your own install if you want the full table.

## 8. Antivirus / SmartScreen

The package contains a small DLL that is copied next to a game executable, which
is the same shape as a loader an attacker would use, and it is unsigned. Windows
may warn or quarantine it. Build the loader yourself from
`loader/src/upc_stub.c` (`loader/build.ps1`) if you prefer a binary you made.
