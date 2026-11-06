# Career progress: what feeds it, what we serve, what to watch

_Status 2026-11-06. This page exists because the career UI is the one part of
the online surface we cannot fully prove offline._

## The two documents

The client's profile has a **Reputation** page (`MainMenuPlayerProfile`, tab
`ReputationBtn`) and a **Statistics** page. They are fed at login by
`PlayerProfileServiceProtocol` reads:

| method | reply shape | what we serve now |
|---|---|---|
| `GetReputationPoints_V1` | `map<u32,u32>` (key -> points) | the player's real reputation map: +50 per senate/World Council vote, +50/+150/+300 per solved world-event mission (add-only) |
| `AreReputationPointsMissing_V1` | one bool, request carries the key | `0` (present) once the key has points, else `1` |
| `GetStats_V1` | `u32 count` + N x `StatsEntry` (6 x u32, meanings unproven) | empty list - deliberately, see below |
| `GetWorldStats_V1` | `u32 version` + list of `StatsEntry` | empty |

`Vote_V1` itself is a **void** reply: nothing about the "+50" can travel with
it. The points must therefore come from the profile, which is why they are
stored in `players.<pid>.reputation` and served by `GetReputationPoints_V1` -
and why the career page only shows them **after a restart** (or a profile
reload): the client reads the profile once per login.

## Why `GetStats_V1` stays empty

`StatsEntry` is 36 bytes of six u32s whose *meaning is not decoded* (only the
widths and order are proven, `decomp/batch/step4_readers/PROFILE-UNKNOWN.md`).
Inventing values there risks a Statistics page full of nonsense, which looks
more broken than an empty page. The honest order of work is:

1. have the player open the Reputation page once while the server log is being
   captured (`PROGRESS.bat` keeps `logs/server.log`),
2. see whether `GetReputationPoints_V1` is called (it should be) and what
   `body=` size we answered,
3. if the page is still empty while the map is non-empty, the page is reading
   `GetStats_V1`/`GetWorldStats_V1` and the row semantics must be decoded from
   the client's own writer before we fill it.

## What is guaranteed today

* a vote and a mission both credit reputation, add-only, and the total is in
  `server/state.json` under `players.<pid>.reputation` (key = faction id);
* `PROGRESS.bat` prints that total straight from the file after a session;
* nothing can lose it: atomic save + `state.json.bak`, and a legacy
  `data/mimic_state.json` is adopted on the first run of this build.

## One-minute check for a player

```
PLAY.bat  ->  vote in the senate  ->  quit  ->  PLAY.bat  ->  open the profile
PROGRESS.bat  shows "career points : 50"
```
If `PROGRESS.bat` says 50 and the profile says 0, the server is right and the
client is reading a different document - that is exactly the case
`GetStats_V1` above describes.
