# Career progress: what feeds it, what we serve, what to watch

_Status 2026-11-08. Rewritten: this page used to claim the client reads
`GetReputationPoints_V1` at login. **It does not.** That claim came from reading
the decompiled *service wrapper* instead of the captures; the census below
settles it._

## What the client actually asks for

**Live correction (2026-11-08, first session on the fixed build):** the client
**does** call `GetReputationPoints_V1` - twice within one session, right after a
World Council vote, and it was answered `{0: 50, 1: 0, 2: 50}` (see
`logs/server.log`). It is not a *login* call (a plain login never issues it), so
the census below is still right about the login chain - it was wrong to conclude
the document is never read at all. Treat the map as read-on-demand: the vote
refreshes the career page.

Census over 243 captured logins (`runs/*/server.log`):

```
267  PlayerProfileServiceProtocol::GetStats_V1
267  PlayerProfileServiceProtocol::SetStats_V1
243  PlayerProfileServiceProtocol::AreReputationPointsMissing_V1
243  WorldEventServiceProtocol::CompleteMission_V1
  0  PlayerProfileServiceProtocol::GetReputationPoints_V1
  0  PlayerProfileServiceProtocol::SetReputationPoints_V1
  0  UploadStatsReputation_V1 / UploadFactionRewards_V1 / SolveDailyQuest_V1
```

So the reputation map travels **one way only** today: the server writes it, the
client never reads it and never pushes its own. `CreatePlayerProfile_V1` fires on
*every* login (the profile is re-created each session) and still no reputation
fetch follows.

Where the career page really gets its numbers (decompiled
`CMainMenuPlayerProfileFrame`, `FUN_140386d00` -> `FUN_140381d00`):

* the page reads a `std::map<u32,u32>` at `profile + 0x2fd0`,
* key = `window.Type` = `FACTION_ECO 0` / `FACTION_TYCOON 1` / `FACTION_TECH 2`
  (the same key space our vote uses),
* value = points, and the **level ladder is client-side data** (10 levels, total
  `0x1b26 = 6950`; levels 11-13 exist in the addon art as "impossible" levels),
* a level is *unlocked* when `points >= start` and *done* when
  `points >= start + span`. Nothing asks the server whether a level is unlocked,
  and there is no `BYREPUTATION` condition anywhere in this build, so there is no
  bonus content gated on reputation to hand out.

**Consequence:** server-side reputation is a *backup channel*, and a dead one in
both directions right now. No number we serve can make the career page move by
itself. What we can do - and now do - is keep that channel complete and
trustworthy, so that (a) the day the client pushes or reads, the truth is there,
and (b) a client patch (see "Plan B") has something to paint.

## The two documents we serve

| method | reply shape | what we serve now |
|---|---|---|
| `GetReputationPoints_V1` | `map<u32,u32>` (faction -> points) | **always** the three factions: +50 per senate/World Council vote, +50/+100/+150/+200 per crossed campaign tier, +50/+150/+300 per solved world-event mission (add-only) |
| `AreReputationPointsMissing_V1` | one bool; request carries one u32 | `0` (present) once the profile has any reputation, else `1` |
| `SetReputationPoints_V1` | void | **merge-max**: a client echo may raise a total, never lower it |
| `GetStats_V1` | `u32 count` + N x `StatsEntry` (`u32,u32,u32,u64,u64,u64`) | the rows the client last uploaded (empty if it never did) |
| `SetStats_V1` | void | an **empty** request is "no rows", never "delete my statistics" |
| `SolveDailyQuest_V1` | void | captured, **and** pays `DailyQuestReputation` (100, `properties.xml:21267`) once per quest id - a real in-game action, so it earns like one |
| `Upload*` (`StatsReputation`, `FactionRewards`, `ReputationMails`, `WorldStatsStencils`) | void | decoded rows + a bounded hex tail, kept in `players.<pid>.*` |

`Vote_V1` itself is a **void** reply: nothing about the "+50" can travel with it.

## The four bugs that were on this path (all fixed 2026-11-08)

1. **The 4-byte request prologue.** Every client RMC request body starts with a
   `u32` class word (0 in every capture) *before* the arguments; we read from
   offset 0, so `Vote_V1`'s `(id, faction)` arrived as `(0, id)`. A senate vote
   (id 0) was therefore recorded as *faction 0* - it "looked saved" - while a
   World Council vote (id 1001) arrived as *faction 1001*, failed the enum check
   and was dropped with no reputation. Same shift made `AddAchievement` store id
   0 and could clear the ARK box. Fixed in `rmc_services.request_reader()`.
2. **`AreReputationPointsMissing_V1` was answered per faction.** With one Tycoon
   vote, the client's Eco probe got "missing" and it kept its local zero.
   Presence is a property of the *profile*.
3. **`SetReputationPoints_V1` overwrote.** A client echo of its own local zero
   would have deleted what we paid out. It is merge-max now.
4. **Nothing else was persisted**: `SetStats_V1` wiped the rows on every login
   (the client sends it empty), and every `Upload*` went to a blind no-op that
   did not even read the body.

## Why `GetStats_V1` is not the career store

`StatsEntry` is 36 bytes: three u32 **key words** plus three u64 **counters**
(proven from the client's own reader/writer - it is *not* six u32). The key
space is the profile/asset counter datasets (`ProfileCounter`, `AssetCounter`);
**no counter in either dataset is reputation**, and the page reads the map, not
this list. So filling it in would invent rows for a page we cannot see.

The one interesting lead is `UploadStatsReputation_V1`: its
`StatsReputation{u32,u32,u32}` is exactly the *key* of a `StatsEntry`, i.e. on
the real backend "which stat rows are reputation" was declared separately from
the numbers. That is why every upload is now captured instead of dropped - one
live session with a payload answers the layout question for good.

## Settling the polarity in one session (no rebuild)

`AreReputationPointsMissing_V1` may mean "do you have points for me?" (answer 0
when we do) or "is my copy stale?" (answer 1 so the client asks). We have never
seen a follow-up under either answer, so it is unproven. Try the other answers
live:

```
set RV_REPUTATION_PROBE=alternate     &  PLAY.bat   (one login, then quit)
set RV_REPUTATION_PROBE=missing       &  PLAY.bat
```

`normal` (default) answers from the profile, `present` always 0, `missing`
always 1, `alternate` the opposite of the truth ONCE per process. Every answer
is logged as `reputation: AreMissing asked=.. have=.. -> ..`. If a
`GetReputationPoints_V1` or `SetReputationPoints_V1` ever appears right after a
probe line, the probe is the trigger and the answer becomes "missing=1 whenever
our map is non-empty and the client has not asked yet".

## Plan B: if the client is local-first (the evidence says it is)

Then no server payload can make the ladder move, and the fix is a loader detour
- this project already patches client defects that way (`loader/src/upc_stub.c`,
runtime-only, env-gated):

* hook `FUN_140381d00` (RVA `0x381d00`), the per-faction level fill;
* resolve `profile = *(void**)(*(void**)(module + 0x1ba1510) + 0x4e0)`, then
  `map = profile + 0x2fd0`, and insert `map[faction] = total` from a file the
  server writes (engine `std::map` node: key `+0x1c`, value `+0x20`);
* the tab recomputes on the next refresh (`frame+0x649`), no GUI knowledge
  needed.

~40 lines. It cannot be called "online", so it would be labelled a client patch.

## What is guaranteed today

* a vote, a world-event mission, a crossed campaign tier and a solved daily
  quest all credit reputation, add-only, in `server/state.json` under
  `players.<pid>.reputation` (key = faction id);
* a vote pays **once per election per player** - clicking the bar again cannot
  farm points or inflate the bars;
* an empty `SetStats_V1` cannot wipe statistics; every upload is stored, not
  echoed;
* `PROGRESS.bat` prints the total straight from the file, and
  `server/state_report.py` prints career + campaign + ARK + votes;
* nothing can lose it: atomic save + `state.json.bak`.

## One-minute check for a player

```
PLAY.bat  ->  vote in the senate  ->  quit
PROGRESS.bat  shows "career points : 50"
```

If `PROGRESS.bat` says 50 and the in-game career page says 0, the server is
right and the client is reading its own map - that is exactly the case the
"what the client actually asks for" section describes. Report it with
`logs\server.log`; the `reputation:` lines in it say what we served.
