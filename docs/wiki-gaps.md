# Wiki vs. implementation: online functionality

_Status 2026-11-09. The whole Anno 2070 wiki was downloaded (489 pages,
`/workspace/wiki/pages.json`) and compared against this server. The wiki is the
description of how the retail online service was *supposed* to behave; the code
is the truth about what a player gets here._

Verdicts: **impl** = implemented · **partial** = works but with less than the wiki
describes · **not** = blocked/undecodable/out of scope · **differs** = we
deliberately do something else.

| Feature | Wiki says (page) | We do | Verdict |
|---|---|---|---|
| Account / login / Central hub | one Ubisoft account + serial per PC; the Central menu holds the online entries (Multiplayer, Interface) | loader fabricates the UPC identity, SDK login on 8443, `h_activate_unique_key`, `h_get_account_info` | impl |
| Player profile | portrait + name at first launch; the profile sets how you appear (Multiplayer#Player Profile) | five u32 = portrait/icon/colour/title/ark, served from the store, zero-wipe guard, survives restarts (`_profile_document`, `_set_profile_value`) | impl |
| Contact list / friends / chat | search a name, add anyone but yourself; Friend Finder filters; channels + DMs (Multiplayer) | friends/chat methods answer and store; `FindPlayers` returns an empty list (nobody else exists); messages are recorded, not delivered | partial |
| Mail | system mail about promotions, career advancement and Senate votes (Interface) | full mailbox; only world-event mails are ever sent | partial |
| Senate vote | vote Thorne/Rodriguez/Devi; each round a problem with one solution per candidate; when it concludes the new rules apply and *all* players get a small bonus and a small loss (Voting#Senate) | 3-day cycles, drawn electorate revealed over time, own vote persisted, participation pays 50 to **every** faction once per election; no "conclusion" state, no law effect | differs |
| World Council vote | elect Banes/Green/F.A.T.H.E.R. as president (Voting#World Council) | slot 1, incumbent faction served in `f9`, same simulated electorate | impl (ballot) |
| Council actions / **licenses** | four actions per president, bought with licenses, activated in the Ark's **Politics** tab, only in continuous gameplay | nothing (no license state anywhere); the tab is client-gated, see `known-issues.md` #10 | not |
| Career points: earning (Career Points) | daily quest 50, senate participation 50, world participation 50, election gift 25 (**elected faction**), global-event missions 50/150/300, crisis thresholds 50/100/150/200. The **"All logos"** icon means **the player chooses the faction**, not "all three" (confirmed by the project owner); only campaign/single missions are fixed to a faction | the chosen faction gets the points: a vote credits the faction voted for (a change of vote moves them), a mission/tier/quest credits the player's aligned faction (their last vote, Eco if none), the election gift goes to the elected faction. Served via `GetReputationPoints` with a version pair so the client refetches | impl |
| Career ladder & rewards | 13 levels, 12,650 points per faction: colour at 2, votes ×10 at 3, an ark socket at 4, portraits at 6/9, ark cargo at 7, ark model at 10, 20 % discount at 11, express delivery at 12, protection formulas at 13 (Career Points#Rewards) | the reputation map is stored and served; the ladder itself is client data (this build shows 10 levels, cap 6,950 in the base game) and nothing is gated server-side | differs |
| Vote weight | "your votes gain more power if you earn enough career points"; level 3 = votes for that faction worth **ten times** more | one ballot counts one | not yet (cheap: weight the own ballot by the faction's stored total) |
| Achievements | 223; no gameplay benefit; unlock titles/colours; "Vote in the Senate 1/25/50"; "Mad Scientist" for filling every ark socket (Achievements) | client-pushed ids are stored (dedup by id); world-event missions grant theirs | partial |
| Bonus content / unlocks | portraits at career 6/9, ark models at 10, formulas at 1/5/8/13, UPlay-point items (Bonus Content) | nothing unlocks server-side; `ListShopItems` returns an empty shop | not (client-owned) |
| Ark | goods carry across games and into co-op; capacity grows at career 4/7; rarities 3/2/1 sockets; upgrades only saved out of game (Ark, Continuous Game#Ark Storage) | box persists, quality-group slot rules, mission rewards, bodiless `SetArkItems` is a no-op | partial (persists regardless of mode/settings) |
| World events | 5 chapters × 3 missions, 30 days, ends 20:00 EST; milestones 25k/75k/150k/300k; career only on the first completion and again when it returns (~5 months) (Global Events) | 30-day campaign, 5-chapter rotation (so ~5-month recurrence), drawn target 25k–125k filled day by day, mails, achievements, ark rewards | partial (target range is the owner's requirement; two of four DLC tiers are unreachable inside it) |
| Daily quests | Current Events screen, 50 points per task, 350/week (Career Points) | `SolveDailyQuest_V1` stored and paid 50 to every faction, once per quest id | impl |
| Multiplayer | up to 4 players, vs/co-op, invites, "Allow Open Access", AI takeover, "Start as Multiplayer" (Multiplayer) | `MatchMakingServiceProtocol` shapes exist but have no handlers - the stateless default answers `GetSessions` with one fake session; the 24 `ConquestServer/*.php` endpoints exist but the client never receives that address | not |
| Uplay / reward points | UPlay points buy portraits and items (Bonus Content) | the loader stubs the UPC API; no Uplay service | not (out of scope) |

## Cheap wins that were taken in 0.1.6

1. **Single-faction credit is now exact**: the faction the player *chooses* gets
   the points, and it is written to the save file (verified by re-opening the
   file in the tests). A changed vote moves its 50 to the new faction; a
   mission/tier/quest credits the aligned faction.
2. **Election gift**: 25 points to the elected faction when an election
   concludes (`_pay_election_gifts`, idempotent per slot+election).
3. **Daily quest 50** (was 100): the wiki's own arithmetic says 350/week = 7×50.
4. **The client is told when the map changed** (`reputationVersion` /
   `reputationSeen`), so the career page refetches instead of keeping its own
   zeros - the other half of "the points were not saved".

## Still worth doing, in payoff order

1. **Vote weight ×10** above 300 reputation with the voted faction (the wiki's
   level-3 "Special Status") - one branch in `apply_vote` plus a `weight` field
   in the vote record.
2. **Achievements from real actions**: nth senate vote (1/25/50) and "Mad
   Scientist" when every socket is filled - `_grant_achievement` already exists.
3. **Vote/career mail**: `deliver_mail` is idempotent; only loca ids are missing.
4. **`VotingInfo.f10`**: fill it with a real senate-effect id so the senate page's
   "active effects" block is not blank.
5. **Matchmaking**: wire the 13 body-carrying methods onto the session store the
   conquest layer already has - the only plausible route to a lobby.

## Where our own docs were wrong (corrected here)

* `career.md` said "there is no bonus content gated on reputation" - the wiki's
  Career Points#Rewards lists thirteen levels of unlocks. They are gated
  *client-side*, not absent.
* `career.md` said "10 levels, 6,950" - that is the base game; Deep Ocean adds
  three ranks and the wiki's 13 thresholds sum to 12,650 per faction.
* `voting.md` said "nothing opens or closes a vote by clock" - the wiki describes
  rounds that *conclude* and apply the new rules; we never conclude, which is a
  design gap, not a client limitation.
* `known-issues.md` #2 said everything behind the conquest address is
  implemented "including session lists and matchmaking reply shapes" - only the
  PHP half is; the A5 lobby (`MatchMakingServiceProtocol`) has no handlers and is
  answered by the stateless defaults.
