# Changelog

## 0.1.3 - 2026-11-09

Added
- **Simulated elections.** The vote bars were a frozen filler count; they are now
  a drawn contest that fills up over the election's lifetime. Elections are
  cycles anchored to a fixed date (Senate every 3 days, World Council every 7),
  each cycle draws its own outcome (three seeded weights, normalised, 4 % floor -
  so one vote is a two-horse race and the next a landslide), draws an electorate
  of 800..4200, and reveals it along a seeded, strictly increasing curve with a
  per-faction pace, so early counts can have a different leader than the result
  while the last counted ballot lands exactly on the drawn shares. Stats: it is a
  pure function of (slot, election index, time) - restart-stable, nothing to
  persist. Your own ballot is added on top for the running election only, the
  voter list is filled with stable generated handles, and `RV_VOTE_SIM=0` restores
  the old static behaviour. `docs/voting.md` has the details.
- The election's own vote and its 50-reputation reward are now **per election
  instance**: the vote log records which cycle a ballot belongs to, so a new
  election lets you vote (and earn) again while a change of mind inside one
  election still pays nothing. `state_report.py` prints the bars, the election
  index and how far through the cycle the count is.
- `GetLastVoters_V1` sends your ballot under your **display name** (the panel
  bolds "your" row by comparing it with the profile name; the internal player id
  never matched).

## 0.1.2 - 2026-11-08

Fixed (reported after the first live session: "I vote in the World Council,
restart, and the vote can be done again")
- **The World Council's own vote was never persisted.** `content_voting.apply_vote`
  wrote the own-vote field only into a document that already existed, and nothing
  created one: every save has `voting.infos["0"]` (the senate) but never
  `infos["1"]` (the council), so a council vote landed in the counts, the vote
  log and the reward book and the *own vote* was dropped. `GetVotingInfos_V1`
  then rebuilt the council document with `f5 = 0xFFFFFFFF` ("not voted"), which
  is the only thing the client reads to decide whether the vote UI is locked -
  so the vote could be cast again (the reputation was still protected by the
  reward ticket, so no double pay). `apply_vote` now creates the missing
  document (with the correct Vote id, 1001), `elections_from_state(voter=pid)`
  recovers the own vote from the durable vote log when a stored document lacks
  it, and `heal_infos()` repairs an existing save on load - including the user's
  live save (`infos` gains `"1"` with `f6 = 2`) and writes it back once.
- New `docs/voting.md`: the two documents, the client's `f3` gate, why `f5` is
  the only "you voted" signal (with the function addresses), and the wire dump
  knob (`RV_DUMP_REPLY=1`) that settles any future "did the server say it?"
  question by hex-dumping reply bodies.
- New keys in the voting state now copy the file's existing key style (`"0"` vs
  `0`); a mixed-key dict made `json.dump(sort_keys=True)` raise and silently fall
  back to an unsorted write.
- `server/probe_rmc.py` now also proves the council's own vote comes back
  (`GetVotingInfos` → key 1 / vote id 1001 / own vote = the faction just cast).

Deployed
- Installed to `C:\projects\Anno2070` with `packaging/update-install.ps1`: the
  running server is stopped first, everything it touches is backed up to
  `_backup-before-update-<stamp>.zip`, the save is parked and restored, and the
  update verifies itself (version, single-launch, name terminator, 119 methods).
  Verified on the host: `SELFTEST PASSED`, `ALL LIVE PROBES PASSED` on port 8443.
- New `server/repair_state.py` (+ tests): repairs a save the pre-0.1.2 build
  damaged - the request-prologue bug stored an impossible ARK record (socket
  quality 1.9 billion) and achievements with id 0. The tool drops only provably
  invalid data, refills the documented starter box, keeps a
  `state.json.before-repair-<stamp>` copy and is idempotent. The player's live
  save on the host was repaired with it (6 starter items restored, career 50 and
  the senate vote untouched).

Verified live (first session on the new build, `logs/server.log`)
- `Vote_V1` with the real 12-byte council body
  (`00 00 00 00 | e9 03 00 00 | 02 00 00 00` = id 1001, faction Tech) is counted
  on the **council** slot (`counts["1"] = {2: 1}`), credited (`reputation`
  `{0: 50, 2: 50}`) and pays exactly once (`rewarded {"1:default": 2}`).
- `GetReputationPoints_V1` is **called by the client** after a vote and answered
  `{0: 50, 1: 0, 2: 50}` - the career map is read on demand, not at login.
- an achievement is stored with its **real id** (`1116866`), not `0`.
- both `WARNING request prologue kind=1/2` lines in that session are what led to
  the declared-type block above.

Fixed
- **The request prologue also declares types, and that is now skipped.**
  Caught live on the first session of the fixed build: the leading u32 is a
  *count of declared type names* that follow it, each a String + u16 flag, and
  the arguments come after them (`GetArkItems_V1` declares `ArkItem`,
  `GetLastVoters_V1` declares `VoteEntry` and THEN sends `(id, count)`, World
  Event `GetStats_V1` declares two nested types). Skipping only 4 bytes made
  `GetLastVoters_V1` read the length of the string "VoteEntry" as its election
  id, so the voter list came from the wrong election. `request_reader()` now
  skips the block (bounded, with a safe fallback) and logs the declared names
  once - they confirm each reply's shape for free.
- **The 4-byte request prologue (the root cause of most of the 2026-11-08
  reports).** Every client RMC request body begins with a `u32` class word (0 in
  every capture) before its arguments; the handlers read from offset 0, so every
  argument was shifted one word. `Vote_V1`'s `(id, faction)` arrived as
  `(0, id)`: a senate vote became "faction 0" (so it looked saved), a World
  Council vote became "faction 1001", failed the enum check and was dropped with
  no reputation; `GetVoteCount_V1` always read id 0 (both polls shared one set
  of bars); `AddAchievement_V1` stored id 0; `SetArkItems_V1` read the class
  word as its list count and cleared the box. `rmc_services.request_reader()`
  now skips the prologue for both the PRUDP responder and the websocket seam, an
  unproven non-zero kind is logged once, and every test body speaks the real
  wire (that mirror is why the suites were green while the bug shipped).
- **Career points are credited at all now.** With the arguments aligned, a vote
  pays its faction, a solved mission pays its 50/150/300, a crossed campaign tier
  pays every player, and a solved daily quest pays `DailyQuestReputation` (100,
  `properties.xml:21267`) once per quest id. `GetReputationPoints_V1` always serves the three
  factions (never an empty map), `SetReputationPoints_V1` is **merge-max** (a
  client echo can raise a total but never lower it), and a vote pays **once per
  election per player** - clicking the bar again cannot farm points.
- **The world event is a month-long campaign with a simulated world.** A
  campaign is 30 UTC days anchored to a fixed date; each campaign draws a global
  target between **25,000 and 125,000** points from a seeded splitmix64 draw and
  fills it day by day (per-day seeded weights, monotone, exact on the last day,
  pure function of the date so a restart cannot change it). The wire carries
  f6 = collected, f7 = target, f4 = the campaign's last day;
  `worldEvent.campaign` records it in the save. Crossing one of the chapter's
  four shipped tier thresholds pays that tier's reputation to every player, once
  per `(campaign, tier)` (`worldEvent.campaignPaid`). `CompleteMission_V1` now
  replies with the campaign target because the client stores that u32 into the
  progress-**max** slot (`ddl_region.c:8694`).
- **Achievements were stored but unrenderable.** `rv_shapes._pair()` only knew
  the keys `key`/`k`, so a stored `{"id": …}` was served with id 0 - every
  achievement collapsed into one row. `AddAchievement_V1` is de-duplicated by
  id and keeps the first stamp (the client re-sends its achievements on every
  login).
- **Statistics survived nothing.** The client sends `SetStats_V1` **empty** on
  every login and we treated that as "delete my statistics"; empty now means "no
  rows". `UploadStatsReputation_V1`, `UploadFactionRewards_V1`,
  `UploadReputationMails_V1`, `SolveDailyQuest_V1` and
  `UploadWorldStatsStencils_V1` used to be blind no-ops that did not even read
  the body - they now store the decoded rows plus a bounded hex tail, so nothing
  the client pushes is ever lost.
- **`AreReputationPointsMissing_V1` polarity is now testable live** with
  `RV_REPUTATION_PROBE=normal|present|missing|alternate` (logged per call):
  the one career question that cannot be settled from the captures.
- **A second server can no longer overwrite the save.** The HTTP port is bound
  before anything else starts (a taken port is now a refusal to boot, not a
  server that looks alive while the game talks to another process), and the save
  file is protected by an exclusive lock (`state.json.lock`). Both refuse loudly
  and touch nothing.
- **The save heals itself.** `state.json.bak` now rotates on **every** save (so
  it is always "the last known good file", not "the file from the first run"),
  and an unreadable `state.json` is recovered from the backup instead of being
  reset to the defaults - the old behaviour overwrote the player's progress with
  an empty document on the next save. `Store.save()` also returns whether it
  worked.
- **`server/probe_rmc.py`**: a LIVE self-check over the websocket the game
  actually uses - it votes once in the World Council, then reads the council's
  bars, the career map and the event, and prints `ALL LIVE PROBES PASSED`.
  `PROGRESS.bat` runs it against the running server.
- Duplicate (byte-identical) `set_visited`/`_split_instance`/
  `complete_mission`/`completed_missions` definitions removed from
  `content_events.py` - the later copy silently won.

Docs
- `docs/career.md` rewritten: the client never calls
  `GetReputationPoints_V1`, the ladder is client-side (10 levels, cap 6950), and
  a one-session probe plus a loader detour are the only routes to a moving
  career page.
- `docs/world-events.md`: the campaign machinery, the tier payouts, the
  `CompleteMission` reply slot.
- `docs/known-issues.md` #10: the Politics tab is disabled **by the client**
  (loca 21412007, `POLTITCS_TAB`, hash 1427912439) - no server reply can open it,
  with the exact probe plan to find the flag.
- `docs/PLAYING.md`: the campaign, and an honest career section.

Tests
- New regression blocks: the real captured `Vote_V1`/`GetVoteCount_V1` bytes
  (senate 0 and council 1001), the achievement id round-trip, the empty
  `SetStats_V1`, the upload capture, merge-max reputation, the campaign month
  (30 days, target in range, monotone fill, exact landing, tier ladder) and
  one-payment-per-tier. `SELFTEST.bat` now also proves the campaign and the
  achievement id.

Fixed
- **Player name showed as `Player` + boxes.** The Quazal `String` writer omitted
  its NUL terminator and counted the length without it, while the game's own
  writer (`FUN_141370f90`) writes `u16 (strlen + 1)` and then the bytes + NUL
  and its reader allocates `len + 1` and copies `len` bytes. The client's last
  byte was therefore uninitialised heap memory; it widened into replacement
  glyphs. `rv_shapes.qstr` and the two hand-rolled copies on the login/register
  path now emit the terminator. Every test oracle that mirrored the old
  convention was corrected (that mirror is why 4881 checks passed while the
  bug shipped).
- **Votes were forgotten after a restart.** A World Council vote sends Vote id
  `1001`, but the handler coerced every id onto the senate slot (`0`) and
  `elections_from_state` only restored the own-vote field for slot 0 - so the
  council came back "not voted" while the senate claimed the vote. Ids are now
  mapped to their slot, `GetVoteCount_V1` honours the id the client polls, and
  both slots are restored.
- **Career points went nowhere.** `Vote_V1` credits `VOTE_REWARD` (50) to
  `players.<pid>.reputation` and mirrors it into `worldEvent.reputation`, so
  `GetReputationPoints_V1` returns it. (Correction: the client does **not** call
  `GetReputationPoints_V1` - 0 times in 243 captured logins; it only probes
  `AreReputationPointsMissing_V1`. Server-side reputation is a backup channel
  until the client asks, see `docs/career.md`.)
  `AreReputationPointsMissing_V1` now answers once for the profile instead of
  per key, so a faction the player has not earned yet no longer reports
  "the server has nothing".
- **`SetReputationPoints_V1` silently dropped writes** on a state file that
  stored `reputation` as a list (`IndexError`, swallowed by the reply path, so
  the client believed it saved). Migration now coerces it to a map, and the
  handler cannot raise.
- **The world event never changed.** It was one constant (chapter id `0`, no day
  boundary - and `0` is not even a real chapter). It now rotates over the five
  chapter GUIDs the client actually ships (Atomic Terror, The Eden Project,
  Global Distrust, The Nordamark Border Conflict, The Secret of the Ebashi
  Trench), one per UTC day, persisted as a day key so a restart mid-day keeps
  today's event. `RV_WORLD_EVENT_PERIOD_DAYS`-style pinning is
  `period_days=0` for A/B runs.

Fixed
- **Career page: the presence probe.** `AreReputationPointsMissing_V1` answered
  per faction, so a player whose points were in Tycoon got "missing" for Eco,
  and the client kept its local zero. Presence is now answered for the profile,
  and `GetReputationPoints_V1` serves all three factions (earned and 0) so the
  panels render.
- **`StatsEntry` is `u32,u32,u32,u64,u64,u64` (36 B), not six u32** - corrected
  in the docs; the key space is still undecoded, so `GetStats_V1` stays empty.

Fixed
- **An install update no longer wipes the save.** `server/state.json` lives
  inside the folder an update replaces; `packaging/update-install.ps1` now parks
  `state.json` + `.bak` first and puts them back, and the procedure backs up
  everything it touches.

Added
- **Mail badge with a real count.** `MailServiceProtocol::GetMailCount_V1`
  (polled once per login) reported an empty document; it now returns the real
  mailbox - total and unread, from the same map `SetInfoMailRead_V1` flips - so
  the event's intro mail shows as unread. `UploadMail_V1` stores what the client
  reports instead of discarding it.
- **Events mail you as you play**: the chapter's `ProgressMail` on a solved
  mission and its `ConclusionMail` when the chain is finished, beside the
  `InitialMail` a new chapter sends.
- **Events send mail and unlock achievements.** A new event puts that chapter's
  own `InitialMail` id in the mailbox (real client text: `1005860` Atomic
  Terror, `1005939` Eden, `7111160` Global Distrust, `7211011` Nordamark,
  `7311006` Ebashi Trench), and a solved mission unlocks its real achievement id
  from `achievements.xml` - the client renders the name, description and icon
  itself.
- **World-event missions grant their real ARK items** where the shipped reward
  is an item at all: the mission->reward link is the achievement table, 7 of the
  15 rewards are DLC `ConstructionPlanItem`/`BuildingUpgradeGlobal` assets, and
  they are allocated into a matching-quality slot (never over the player's own
  items, only over our seed items or an earlier mission reward).
- **`state_report.py` + a "saved progress" section in PROGRESS.bat.** After a
  session the player sees the career points, the ARK box socket by socket, both
  vote panels (own vote + counted votes), the event of the day and the missions
  solved - read straight out of `server/state.json`, so "did it save?" is one
  double-click away.
- **A second server can no longer hijack the port.** `http.server`'s
  `allow_reuse_address` let a second instance bind an already-used port (on
  Windows), so two servers answered the same client and overwrote each other's
  saved state - the classic "my vote disappeared". Binding now fails loudly with
  a message naming the cause, and `SO_EXCLUSIVEADDRUSE` is set on Windows.
- **A save can no longer fail on key types.** Adding an int mail id to a
  mailbox whose keys came off disk as strings made `json.dump(sort_keys=True)`
  raise `TypeError`, and the whole save was lost (seen live: three
  "save failed" lines and a lost event roll). Integer-keyed maps are normalised
  on load and `Store.save` falls back to an unsorted dump rather than dropping
  the document.
- **The event rotation no longer clears a fresh state.** Rolling from "no day
  key yet" to today's chapter wiped the mission records of a session that had
  already completed missions; the clear now only happens when a *previous*
  chapter is really being replaced.
- **The JSON save is hardened.** Every save now records `"schema": 2`, is
  written to `.tmp`, flushed/fsynced, and the previous good file is kept once
  as `state.json.bak` before the new one replaces it.
- **World events pay out.** A solved mission credits reputation from the shipped
  reward table (1/5/10 points, 50/150/300 reputation) into
  `players.<pid>.reputation` and the world-event totals, the event's progress
  bar counts solved missions out of the chapter's three, and `GetStats_V1`
  returns a populated (four-row, best-first) community panel built from the real
  records instead of empty lists.

Changed
- **PLAY.bat launches the game once.** The 3-attempt retry loop is gone: a
  launch that exits is reported (with PROGRESS) instead of being retried, and a
  dead launch is detected in seconds instead of after a fixed 100 s wait.
- **PLAY.bat no longer force-closes the game** (that can truncate
  `Config\Engine.ini`): if Anno 2070 is running it asks you to quit from the
  game's menu. It also no longer kills every `python.exe` on the machine - only
  the process holding the server port.
- **PLAY.bat names the state file explicitly** (`--state-file`) and prints it,
  so a launch can never use a different file. `mimic_server` accepts
  `--state-file` as an alias of `--data-file`, and adopts an older
  `data/mimic_state.json` instead of starting empty.
- PLAY.bat / PROGRESS.bat: the summary is no longer skipped (`goto :end`), so the
  window stays open and the report is readable.

## 0.1.1 (uninstaller, same release)

Added
- **Uninstaller.** `UNINSTALL.bat` for the PLAY.bat package and
  `Uninstall-Anno2070Online.exe` for the .exe build (also a Start Menu entry).
  It stops a running launcher/server, restores Ubisoft's original
  `upc_r2_loader64.dll` from the backup INSTALL.bat now keeps, deletes our
  `upc_stub.log`, keeps `state.json` + `config.ini` in
  `%LOCALAPPDATA%\Anno2070Online-backup`, then removes the runtime folder and
  the shortcuts. `--dry-run` shows the plan without touching anything,
  `--purge` / `/purge` drops the saved state as well. The game's own files and
  savegames are never touched; a loader DLL that is not ours is left alone.
- INSTALL.bat and Setup.exe keep the original loader as
  `upc_r2_loader64.dll.original-backup` before copying ours, so uninstalling is
  exact.
- Test: `tests/test_uninstall.py` builds a fake game + runtime in a temp folder
  and checks restore/delete/keep/purge/dry-run/foreign-DLL behaviour (runs in CI).
- CI: the frozen uninstaller is smoke-tested with `--dry-run`, and
  `Uninstall-Anno2070Online.exe` is attached to every release.

Fixed
- The uninstaller refuses to delete a folder that is not ours (`--dir` misuse),
  and refuses to run while Anno 2070 is up instead of force-killing it - a hard
  kill can truncate `Config\Engine.ini`.

## 0.1.1 - 2026-11-06

**The bootstrap crash is fixed.** Failed launches no longer die with an access
violation; they end up at the menu in offline mode instead.

Fixed
- Loader: the queue push into a destroyed `CBackEndServiceImpl` pool is now
  skipped (guard on the pool's critical-section pointer, which is NULL only
  after teardown). This was the `0xc0000005` write to `+8` that killed roughly
  one launch in three.
- Loader: the `rdv::ConnectionManager` constructor probe is disabled. Its
  displaced bytes contained a RIP-relative `lea`, so the trampoline computed a
  garbage vtable pointer and stored it on the object **every boot**.
- Loader: the probe that could execute garbage on a PRUDP datagram was reviewed
  and is correct as written (its displaced prologue ends exactly on an
  instruction boundary) - no change needed, documented so it is not "fixed"
  later by mistake.

Changed
- Server: RMC replies are no longer delayed by 200 ms by default
  (`RV_DELAY_MS` stays as a knob). The delay guaranteed a call was in flight
  when the client's own teardown fired, which is what turned the race into a
  crash.
- Server: `us-sdkClientHttpConfig` timing values can be overridden per run
  (`RV_HTTPCFG_TIMEOUT_MS`, `RV_HTTPCFG_POPEVENTS_MS`, `RV_HTTPCFG_REST_MS`,
  `RV_HTTPCFG_CREATESESSION_REST_MS`) for measurement.
- Launcher: a launch that has not reached the login burst by 60 s is treated as
  failed, closed (forcefully if needed - the game ignores a close request) and
  retried, up to 3 attempts.

Known
- About 6 launches in 10 still fail to come up online. The remaining failure is
  a race inside the game's own client: it cancels its login at a fixed ~36 s and
  the server's replies arrive too late to matter. Restarting the game is the
  workaround; 1-3 attempts normally succeed.
- Conquest/multiplayer remains unreachable (see `docs/known-issues.md`).

## 0.1.0 - 2026-11-06

First packaged release: installer + portable build, online session, ARK item
box, senate and World Council elections and votes, world events, profile,
chat/friends/mail.
