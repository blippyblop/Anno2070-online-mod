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

> **Parked by decision (2026-11-09).** The tab is a multi-player feature: its four
> buttons are the World Patron's favours granted to *other players on the map*
> (multiplayer or AI), so a local single-player world has no use for it. The opt-in
> loader patch below (`UPC_POLITICS=probe|on`, default **off**) stays in the tree as
> a curiosity; nothing else is planned here unless someone wants player buffs in
> their own game.

**Verdict (2026-11-09, re-derived from the binary after the "but the wiki says it
works in continuous games" objection): the tab is enabled by local world/slot/UI
state only - no server reply, document or session parameter reaches the
condition.** The wiki's "politics powers are only available in Continuous
gameplay" does **not** correspond to any game-mode, DLC or campaign check in this
build, which is why starting a continuous game changed nothing.

### Where the tab is enabled (this is the real gate)
`CObjectmenuArkFrame::Update` is `.pdata` range `0x3fa830-0x3fb9ac`. The politics
widget - `GetChildByHash(0x551c36f7)`, cached at `frame+0x108` by `Init`
(`0x3d65a0`) - gets its state from exactly this computation, once per update:

```
0x3fb865  rax = [rsp+0x38]                       ; player-slot object, see below
0x3fb86a  test rax,rax          ; je  -> disable
0x3fb86f  cmp byte [rax+0x1c],0 ; jne -> disable
0x3fb875  rax = [CAccess]                        ; 0x1BA1428 singleton
0x3fb87c  rcx = (int)[rax+0x180]                 ; active world slot, 0..30
0x3fb883  cmp ecx,0x1e          ; ja  -> disable
0x3fb888  rax = [rax+0x1e0]                      ; world* array
0x3fb894  cmp rdx=[rsp+0x28], [rax+rcx*8] ; jne -> disable   ; this Ark's world must be the active one
0x3fb89a  cmp byte [rbp+0x258],0 ; jne -> disable            ; UI page flag
0x3fb8a3  mov dl,1        | 0x3fb8a7  xor edx,edx            ; enabled / disabled
0x3fb8a9  rcx = [r14+0x108] ; call [vtable+0x198](state=dl, 1, 0)
```

`[rsp+0x28]` = `[CAccess+0x1e0][ [world+0x58]*8 ]` (the world registered under
this world's own id); `[rsp+0x38]` = `[world+0x20][ [[world+0x60]+0x374] ]`,
bounds-checked against `[[world+0x60]+0x360]`. The callee slot `+0x198` is a thunk
`0xc39930 -> [rax+0x178]`, body **`0xc393a0`** = the GUI state/setter
(`[this+0x3b4] = r9b`, state field `[this+0x1e0] = 0..3`). `[world+0x258]` is 0
normally and set to 1 at `0x3fae18` only while the current page id is one of
{8,18,19,21,28} (`bt 0x102c0000,ebx`).

**Every input is a local object-graph field**: the player-slot pointer, one byte
on it, `CAccess`'s active-world index/array, the world id, and a UI page flag.
No `RMC`/SDK/document reader appears anywhere in this function or in its callees.

### What the page selector does (content, not availability)
`FUN_1403d7ec0` picks *which* page the tab shows, and returns "no page" (NULL, at
`0x3d82d8`) when: no session; `[session_lookup+0x60]` null; `[[..]+0x260]` null;
`[CWorldManager+0x3f0] > 1` (more than one world registered); or
`[CAccess+0x208]->[+0x78]` vector count `> 1`. Otherwise `[[..+0x260]+0x14]`,
a 27-entry enum (3..0x1e, jump table at `0x3d82f8`/`0x3d8318`), chooses the page.
`[x+0x260]` is **not** resolved: no store with displacement `0x260` exists anywhere
in `.text` (function-bounded capstone sweep), so it is written by a bulk copy or
through a computed index; the related value read at `0x3fb442` is matched against
a 12-entry table at `0x1BA2A08+0x5df0`, i.e. it smells like a *scenario/world-type*
id rather than an online object. So the earlier "the world has no politics object"
wording was a guess; what is proven is only that the page lookup needs that field.

### Why no server can fix it
The tab's disabled tooltip is the widget's own disabled state (`+0x989680` added at
`0x3de73e`), i.e. `dl=0` from `0x3fb8a7` - not "no page". The condition that
produces `dl=0` contains no network data: serving a different `VotingInfo`, a
patron GUID, a session flag or a document cannot change it. `VotingInfo.f9`
(incumbent) only fills the *content* of the council page.

### If it is ever wanted (client patch, in the loader)
* cheapest, 2 bytes: at **`0x3fb8a7`** replace `33 d2` (`xor edx,edx`) with
  `b2 01` (`mov dl,1`) - the politics widget then always gets state 1. Side
  effects: the tab shows even when the world is not the active one; the page may
  still be NULL on a world whose `+0x260` is null (then it opens empty).
* or patch `0x3D82D8`'s call to pass `[frame+0x100]` instead of NULL.
* ship it as an opt-in `upc_r2_loader64.dll` patch (env/flag), never default:
  the tab's content needs `m_WorldPatronGUID` / `m_SenateEffectGUID`, both 0 in a
  local world, so two of its three blocks would be empty.

### Turning it on (staged, opt-in - a CLIENT PATCH)

Because no server reply can open it, the way in is a patch in **our loader DLL**,
applied at run time (the game's file is never modified on disk). It is opt-in, and
it says so in the log every launch.

1. `config.ini` → `[options]` → `politics = probe` (or `on`), or set the
   environment variable `UPC_POLITICS=probe|on` before `PLAY.bat` (the knob wins
   over the file).
2. **`probe`** changes nothing: it hooks `SetActivatedChild` (`0xc8ee60`) and the
   Ark's tab-state thunk (`0xc39930`) and logs, per Ark update,
   `tab widget <p> state=<0|1>` and `SetActivatedChild(tab, page)`. A `state=0`
   line for the politics widget means "disabled" (`<-- DISABLED`), a NULL page
   means `<-- NO PAGE`. One continuous-game session tells us which half fails.
3. **`on`** writes two things:
   * `0x3fb8a7`: `33 d2` (`xor edx,edx`) → `b2 01` (`mov dl,1`) - the tab always
     gets the enabled state;
   * the selector's five bail-outs (`0x3d7ef1`, `0x3d7efe`, `0x3d7f0e`, `0x3d7f22`,
     `0x3d7f57`) are retargeted to the last-resort page block at `0x3d7fdf`, so a
     page is always activated. `UPC_POLITICS_PAGE=<hex>` (default `0x100`) picks
     which page widget that block loads - the frame's own table (`Init`, hashes at
     `frame+0x100..0x148`) is the menu, and the *right* one for the politics panel
     is the open question the probe log answers.
   Every write verifies the bytes it expects first and logs `refused` if the build
   differs; if any write is refused nothing else is touched.

**Expect it to be largely empty:** the panel needs `m_WorldPatronGUID` and
`m_SenateEffectGUID` (both 0 in a local world), so its Patron and Senate blocks
render blank. This is "make the tab open", not "make it meaningful" - the content
needs world data that no longer exists.

#### What the four `ARK_PATRON_ACTION` buttons actually are (checked, not assumed)

They are the **World Patron's sector favours**, i.e. the political powers of the
elected President of the World Council - not plain item activations (that earlier
claim in this file was wrong). Evidence from the client's own data:

* the panel's action text is a runtime substitution, `1000800`
  "Available World Council actions: `[CURRENT_WORLDPATRON_ACTIONS]`", plus
  `1000812` "President of the World Council: `[CURRENT_WORLDPATRON_NAME]`";
* the layout has exactly four slots - `ARK_PATRON_ACTION1..4`, each with its own
  cooldown widget (`ARK_WORLDPATRON_COOLDOWN1..4`) - and the runtime keys
  `PatronActions`, `PatronSelected`, `WorldPatronActionsUsed`,
  `WORLDPATRONACTION_USED`, `m_ExecutedThirdPartyActions` say the *use* is
  recorded per world;
* the twelve actions are written as third-party (neutral faction) messages in
  `thirdpartydefault.xml`, GUIDs 5000834-5000856: market power (merchant fleets),
  industrial power, emission trading (carbon credits), fleet maneuver,
  negotiation skills (diplomacy), smart grids (energy), creative power, vigilance
  week, individual tutoring (licence sale), robotic workforce (maintenance),
  construction limits testing, research grant. Each is a **temporary buff handed to
  another player** in the sector, phrased as that NPC acting on your behalf
  ("`[THIRDPARTY_SENDER_PLAYER]` has passed a new Industrial law ...");
* the Ark item side carries the effect: item texts 60645/60646 are "Additional
  Effect for TechPatronAction03/04", and the string `WorldPatronItem` marks the
  item representation.

**So the intended loop is:** you win the World Council presidency (`f9` feeds the
panel, `WorldPatronVotes` the vote) -> the tab offers you 4 of the 12 favours, each
with a cooldown -> using one grants a named player in the same world a temporary
buff, delivered as a neutral-faction message, and the use is counted per world.
That is a multi-player, persistent-world feature: with no other players there is
nobody to favour, and there is no server service left to record the use or supply
the action list. A local imitation is possible - elect the player as patron, target
the player themselves, keep a cooldown and the mails in `state.json` - but it is a
self-granted buff (a cheat) and the effect-application path from the client's side
is still unimplemented in this project.

**Status:** the patch, the probe and a test that re-checks every RVA and expected
byte against `Anno5.exe` (`tests/test_politics_patch.py`) are in the tree; the DLL
built from it is staged as `loader/bin/upc_r2_loader64.dll.politics`. It has **not
been run in the game yet** - the test machine was offline when it was staged - so
probe first, then `on`, then read the log.

### The one experiment left (needs the game) in a continuous game, log at `0x3fb865`
the five inputs (`[rsp+0x38]`, `[+0x1c]`, `[CAccess+0x180]`, `[world+0x58]`,
`[world+0x258]`). The only one whose writer is not traced is the player-slot byte
`+0x1c`; if it turns out to be session-derived (e.g. "slot is a remote player"),
that is the single place a server could ever matter. Everything else is local.
