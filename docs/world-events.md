# World events: what works, what rotates, what to build next

_Status 2026-11-06. Research notes are condensed here; the full protocol table
is in `docs/protocol-notes.md`._

## What the client actually needs from us

The event's **content is local**. The client ships everything the panel renders:

| data | where it lives | served by us? |
|---|---|---|
| 5 chapters ("Atomic Terror", "The Eden Project", "Global Distrust", "The Nordamark Border Conflict", "The Secret of the Ebashi Trench") | `data/config/game/assets.xml` in `Data1.rda`/`patch8.rda` | chapter GUID chosen per day, below |
| 3 missions per chapter (GUID `…014/015/016` etc.), points 1/5/10, reputation 50/150/300, reward item GUIDs | same file | echoed back when you complete one |
| titles, briefings, difficulty text | `data/loca/eng/txt/chapters.txt`, `data/config/texteditor/world_event_0N.xml` | we never send text - there is no text field on the wire |
| the playable levels (`.www`) | `Data3.rda`, `data/levels/world_events/…` | launched by the client when you press play |

The wire record is only
`u32 instance, u32 chapter GUID, String name(unused), u64 packed date, bool
running(inverted), u32 progress, u32 progress max, bool visited`.

So "believable world events" is not authoring - it is **picking a real chapter,
putting a real date on it, and echoing the mission GUIDs the player solved**.

## What we do now (2026-11-06)

* `content_events.event_for_day()` rotates the chapter:
  `chapters[(utc_day // period_days) % 5]`, one event per UTC day, persisted as
  a day key in `worldEvent.day` so a restart mid-day keeps today's event and no
  RNG is involved. `period_days = 7` gives weekly chapters (a 3-mission story
  chain with its own mails reads better as a weekly arc - flip it with
  `event_for_day(state, now, period_days=7)`).
* `GetLatestWorldEvent_V1` inherits the rotation; the instance id follows the
  chapter so a client that caches by id sees a new event.
* Completing a mission is recorded per player and echoed by
  `GetOwnCompletedMissions_V1`, which is what turns a mission box "Cleared".

## Known limits (proven, do not fight them)

1. **No server-chosen title or description.** The client parses the `String`
   field and drops it; the title comes from the chapter asset.
2. **No icon/picture choice** - same reason.
3. **One event at a time**, and only chapters the client ships.
4. **No push when an event starts.** The client asks once per login, so a new
   event lands on the next launch, not mid-session.
5. **Per-mission progress bars with text are not available** - one bar per
   event (`progress`/`progress max`), plus a 4-state mission box.
6. **Mission order is client-owned**: mission 2 needs mission 1
   (`MissionsRequired` in the assets). Reporting a later mission as solved does
   not unlock it in the UI.

## What a mission completion now does (2026-11-06, later the same day)

* `CompleteMission_V1` looks the mission GUID up in the shipped reward table
  (1/5/10 points, 50/150/300 reputation), stamps those numbers on the record,
  credits `players.<pid>.reputation` (add-only, the same map the senate vote
  fills, so the career page shows one growing number) and adds the points to
  `worldEvent.reputation`.
* The event record's progress bar is derived: solved missions of the current
  chapter out of the number it ships (e.g. 2/3), never a random number.
* A solved mission also unlocks its **achievement** (the id from
  `achievements.xml`, e.g. `1116925`); the client owns the name, description and
  icon, so the Achievements tab shows the real entry.
* A solved mission also sends that chapter's **ProgressMail** (and the
  **ConclusionMail** once all three of its missions are done) - again real loca
  ids, so the mailbox reads like the official server's.
* When a new chapter rolls, the player receives that chapter's **own intro
  mail** (`InitialMail` from its `WorldEvent` block - `1005860` for Atomic
  Terror, `7311006` for the Ebashi Trench, …), so the mail panel carries the
  event's real text. Asking for the event again in the same day never
  duplicates it.
* `GetStats_V1` is built from those records: per-player points and mission
  counts in list A, per-player reputation in list B, best first, capped at the
  client's four rows, with the three counters set to their totals.

## Next steps, in payoff order

1. **Rewards that land in the ARK - done for the seven that are real items.**
   The mission -> reward link is not in the mission asset at all: it is the
   achievement table (`patch8.rda:data/config/questconfig/achievements.xml`,
   `CompleteWorldEventNNMissionMM` -> `<Rewards><Item><Reward>GUID`). Of the 15
   reward GUIDs, 8 are profile cosmetics (`ProfileTitle`, `ProfilePortrait`, a
   decoration building) and can never be an ArkItem; the other 7 are real
   `ConstructionPlanItem` / `BuildingUpgradeGlobal` assets (`7001000-7001004`,
   `7111101-7111103`) and are now granted:
   * into the first free slot **of the item's own quality group** (A/B/C are
     separate - the client refuses a mismatched slot);
   * never over an item the player placed; when the group is full only one of
     our own seed items or an earlier mission reward may be replaced;
   * `RV_ARK_REWARDS=0` disables the grants.
2. **`UploadWorldEventTemplate_V1`**: store the raw bytes instead of the size.
   If a client path ever uploads its template table, that hands us ground truth
   for the twelve undecoded fields.
3. **Per-mission progress text** is not available: the panel has one bar and a
   four-state mission box (`Enabled/Disabled/Cleared`), so anything finer has to
   be client-side.

## Experiments that settle the open questions

| # | do | learn |
|---|---|---|
| E1 | the rotation now sends a real chapter GUID (was `0`) | whether `0` was ever valid - watch the mission list and the log line "No mission GUID found for the given mission index." |
| E2 | send the window end vs the window start in the date field | which one the panel labels (the packed date is a bit-packed Anno date, not epoch seconds) |
| E3 | set progress 40/100, then max 0 | whether the bar reads those fields at all, and that `0` breaks it |
| E5 | answer `CompleteMission` with 0 vs the progress max | confirms the reply overwrites the same slot the progress max sits in - keep 0 |
