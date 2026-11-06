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

## Next steps, in payoff order

1. **Credit the reward and reputation on completion** (half a day). We already
   know the mission → points/reputation mapping (1/5/10 and 50/150/300). On
   `CompleteMission_V1`, add the points to `players.<pid>.reputation` (the same
   key the vote reward uses - add only, never overwrite) and put the mission's
   `Reward` item GUID into the ARK. That makes completing a mission *matter*
   instead of only flipping a tick.
2. **Community stats that look alive** (half a day). `GetStats_V1` already
   returns two 4-row lists plus three counters built from per-player data; the
   three counters' meaning is unproven, so fill the lists with real per-player
   totals and leave the counters self-consistent.
3. **Progress counters from real actions** (one day). `progress`/`progress max`
   could advance with solved missions of the current chapter; keep it honest -
   never move on a timer alone.
4. **`UploadWorldEventTemplate_V1`**: store the raw bytes instead of the size.
   If a client path ever uploads its template table, that hands us ground truth
   for the 12 fields nobody has decoded.

## Experiments that settle the open questions

| # | do | learn |
|---|---|---|
| E1 | the rotation now sends a real chapter GUID (was `0`) | whether `0` was ever valid - watch the mission list and the log line "No mission GUID found for the given mission index." |
| E2 | send the window end vs the window start in the date field | which one the panel labels (the packed date is a bit-packed Anno date, not epoch seconds) |
| E3 | set progress 40/100, then max 0 | whether the bar reads those fields at all, and that `0` breaks it |
| E5 | answer `CompleteMission` with 0 vs the progress max | confirms the reply overwrites the same slot the progress max sits in - keep 0 |
