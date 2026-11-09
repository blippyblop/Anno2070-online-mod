# Known issues and limits

Honest list, newest first. Symptoms are described the way you will see them, not
the way the code sees them.

## 1. About 6 launches in 10 do not come up online (they no longer crash)

**Symptom:** the game opens, and about 36 s later it settles at the menu in
offline mode. Earlier builds actually *crashed* here; since 0.1.1 the client
stays alive and simply abandons its own login.

**Cause:** a race inside the game's own network client. At a fixed ~36 s after
it starts, it tears down and rebuilds its connection state; if that lands while
its login exchange is running, the login is cancelled. The server's replies are
byte-identical between a launch that works and one that does not, and the reply
arrives *after* the client has already given up, so this cannot be fixed from
the server side.

**What 0.1.1 changed:** the crash that used to accompany this is gone. The
client's queue push into a destroyed `CBackEndServiceImpl` pool is now skipped
(that was the `0xc0000005` write to `+8`), and a loader hook that was storing a
garbage vtable pointer on every boot has been disabled. Failed launches now log

```
mgwaq: pool destroyed (CS=NULL) - skipped the queue push that would have crashed
```

and produce no exception at all.

**What to do:** just start the game again. Measured today: 7 of 18 launches
reached a full session, so one or two restarts is the normal cost. PLAY.bat
starts the game exactly once and says so if it dropped out - it does not retry,
and it never force-closes the game (a hard kill can truncate
`Config\Engine.ini`, which then breaks every later launch).

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

## 8. It must be started from your own desktop session

`PLAY.bat` has to run in the same interactive Windows session you play in —
double-clicking it is exactly right. Starting it from a **service**, a **remote
shell** (SSH/WinRM) or a **scheduled task set to "run whether user is logged on or
not"** puts the game in session 0, where it has no desktop: the process starts but
its online bootstrap never finishes, and `PROGRESS.bat` reports
`RV calls answered : 0`.

## 9. Antivirus / SmartScreen

The package contains a small DLL that is copied next to a game executable, which
is the same shape as a loader an attacker would use, and it is unsigned. Windows
may warn or quarantine it. Build the loader yourself from
`loader/src/upc_stub.c` (`loader/build.ps1`) if you prefer a binary you made.

## 10. "Connection to the World Council or Senate not possible at this time"

**Symptom:** the Ark's **Politics** tab cannot be opened; hovering shows that.

**Verdict (2026-11-09, proven from the binary): the gate is entirely client-side
and reads nothing that comes from a server reply.** The Ark window is
`CObjectmenuArkFrame` (RTTI `.?AVCObjectmenuArkFrame@@`, vtable `0x167c670`, its
`Init` virtual at **`0x3d65a0`**). `Init` caches the three tab widgets; the
politics tab is `GetChildByHash(0x551c36f7)` stored at `frame+0x108`. The page is
assigned by `FUN_1403d7ec0`:

```
0x3d7ef1  je 0x3d82e6   ; no game session            -> no page
0x3d7efe  je 0x3d82e6   ; no world ([session+0x60])   -> no page
0x3d7f0e  je 0x3d82e6   ; no politics object          -> no page
                        ;   ([world+0x260] == NULL)
0x3d7f1b  cmp [CWorldManager+0x3f0], 1 ; jg -> no page
0x3d7f57  cmp (vector count), 1        ; jg -> no page
          ; ...then a 27-entry jump table picks the page widget
0x3d80b6  SetActivatedChild(tab, page)
0x3d82d8  SetActivatedChild(tab, NULL)     <- what a local world hits
```

`SetActivatedChild` is `0x140c8ee60`; "no page" is what renders as disabled, and
the engine derives the disabled tooltip by adding `0x989680` (10,000,000) to the
normal one (proven at `0x3de73e`), which is why the hover text is loca
**21412007** - the disabled variant of 11412007 "Politics - Left click to view
the current World Patron and voting results of the Senate".

**So:** no RMC reply, no `VotingInfo` field and no profile/document value can
open it (the callee list of `FUN_1403d7ec0` contains no service or document
reader at all). The two "serve a vote result / serve the patron GUID"
experiments previously listed here are **dead ends** - `VotingInfo.f9` only
feeds the *content* of the council page, never this tab's availability.

**What the tab would show if forced open:** the `ARK_POLITICS_PANEL` page with a
World Patron block (needs `m_WorldPatronGUID`, texts 1000798/1000799), a Senate
block (needs `m_SenateEffectGUID`, texts 1000761/62/65) and four
`ARK_PATRON_ACTION1..4` buttons that are *item activations* from the Ark's own
box (so those would work). In a local single-player world both GUIDs are 0, so
the two text blocks stay empty.

**The only route, if it is ever wanted** (client patch - our loader already
patches client defects): first hook `0x1403d7ec0` and log
`[CWorldManager+0x3f0]`, `[CGameEngine+0x208]`, `[session+0x60]`,
`[[session+0x60]+0x260]`, `[frame+0x108]`, `[[frame+0x108]+0x27c]` and
`[[frame+0x108]+0x380]` - one session tells us which of the five conditions
actually trips. Then either trampoline `0x3D82D8` to call
`SetActivatedChild(tab, [frame+0x100])` instead of `NULL`, or - if it is the
widget's own disabled bit - clear bit 28 of `tab+0x27c` and set the tooltip back
to `0xAE2227` (11412007). Both are 6-14 byte patches inside the Ark window's own
update, trivially reversible.
