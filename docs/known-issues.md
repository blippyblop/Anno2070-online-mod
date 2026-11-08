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

**Symptom:** the Ark's **Politics** tab cannot be opened; hovering it shows that
sentence.

### What the tab is

The Ark has exactly three tabs, all visible in `guidata64/Game.cdf`:
`ARK_STORAGE_TAB`, `ARK_UPGRADE_TAB` and `ARK_POLITICS_TAB` (the game spells it
`POLTITCS_TAB`). The politics tab is the local view of the **world's** political
state. Its own tooltip, and the strings the panel fills, come from the shipped
localisation (`eng1.rda`):

| loca id | file | text |
|---|---|---|
| 11412007 | `tt_normal.txt` | **Politics** - "Left click to view the current World Patron and voting results of the Senate." |
| 21412007 | `tt_disabled.txt` | **Politics** - "Connection to the World Council or Senate not possible at this time." |
| 11002778 | `tt_normal.txt` | Senate - "Laws are passed in the Senate that may be both an advantage and a disadvantage to all players. Frequent votes take place." |
| 1000798 | `interface.txt` | "World Patron until [WORLDPATRON_END_DATE]: [CURRENT_WORLDPATRON_NAME]" |
| 1000799 | `interface.txt` | "The next World Patron vote will begin on: [TIME_WHEN_WORLDPATRON_VOTE_STARTS]" |
| 1000765 | `interface.txt` | "Winner of the last Senate vote: [SENATE_VOTE_WINNER] ..." |
| 1000761/62 | `interface.txt` | "The following item on the agenda is up for vote in the Senate: ..." / "The next Senate vote will begin on: ..." |

So 21412007 is the **disabled-state variant of the tab's own tooltip**, and the
two ids differ by exactly **+10,000,000** - the engine's tooltip convention
(98 such normal/disabled pairs exist in the two loca files, e.g. 11000064 ->
21000064 "Not available offline"). The tab is not broken; it is *greyed out*,
and the game prints its "disabled" text.

### Why it is greyed out

The tab's content is not voting-protocol data. The Ark's main-menu content binds
six **game-state** properties by name (`FUN_14074a240`, verified by resolving
every RIP-relative property binding in that function):

```
m_WorldPatronGUID        who is World Patron right now
m_SenateEffectGUID       which senate law is currently in effect
m_BlackSmokerStartTime   world event timing
m_EnableTownAcquisition  world rule
m_PlayerArkFeedback      the Ark's own feedback state
m_DynamicAmbientList     world ambience
```

A **World Patron is an elected player of a live world**, and the Senate effect is
a law the whole world shares. Neither can exist on a single-player local server:
there is no world session, no other players, and therefore no patron GUID - so
the client disables the tab before it ever talks to us. It is also not
deliverable over RMC: none of the **153** client methods across the ten
protocols carries a patron, an effect GUID or a senate agenda (checked against
`server/client_methods.json`, which is extracted from the client itself).

That is the honest bottom line: **no server payload can open this tab.** What it
would show (patron name + expiry, senate agenda, last winner, next vote times)
are world-wide facts a local server does not have.

### What we do serve, and what to try next

Both elections are live: `GetVotingInfos_V1` returns two `VotingInfo` documents
(senate key 0 / vote id 0, World Council key 1 / vote id 1001), with a running
flag and three packed dates; `GetVoteCount_V1`/`GetLastVoters_V1` answer per
election id; `Vote_V1` records the vote and pays 50 career reputation once per
election. The main-menu **Senate vote** and **World Council vote** entries are
the working face of the same feature.

Ordered experiments for the next live session (each one is a single knob):

1. **Serve a vote *result*.** Our `VotingInfo` sends `f9`/`f10` =
   `0xFFFFFFFF` ("no result"). If those last two words are the winning
   faction/effect reference, filling them with a real faction could flip the tab.
   Try `RV_VOTE_RESULT=0|1|2` and watch the tab.
2. **Check where `m_WorldPatronGUID` comes from.** If it is a session parameter
   (`SetSessionParameters`/`GetGameSettings` in `MatchMakingServiceProtocol`),
   it is blocked together with conquest/matchmaking (issue 2 above) - the client
   never receives its matchmaking address.
3. **Log the gate.** The tab's handle is cached at `this+0x108` right after the
   name hash `0x551c36f7` is resolved (`Anno5.exe+0x3d66c7`, second site
   `+0x3d763c`); a loader detour there can print the boolean the frame tests. If
   it turns out to be a plain flag, forcing it would open the tab - but with no
   patron data to show, so it would be a hollow win and is labelled a client
   patch, not an online feature.
