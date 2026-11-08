# Senate and World Council votes: how they work, and why one of them forgot

_Status 2026-11-08, after the "I vote in the World Council, restart, and my vote
is gone" report. Everything below is either proven from the client binary
(addresses given), from the live session logs in `evidence_20261108/`, or from
the server code._

## The two documents

`GetVotingInfos_V1` returns a **list of `VotingInfo` documents**, 49 bytes each:

| wire | type | meaning |
|---|---|---|
| key | u32 | **map key**; the panels look documents up by this |
| f2 | u32 | the **Vote() id** the client sends back in `Vote_V1` |
| f3 | bool | **vote phase**: 1 = the vote UI is live, 0 = the "results" branch |
| f4 | u64 | packed Anno date (the council shows it as the patron's end date) |
| **f5** | u32 | **own vote faction**, `0xFFFFFFFF` = not voted |
| f6, f7 | u64 | two more packed dates (display only in this build) |
| f8 | u32 | senate effect object id |
| f9 | u32 | world-patron match key - **never send a large value** (the client
  indexes an array with it unchecked) |

We serve two documents: **key 0**, Vote id 0 (the Senate) and **key 1**, Vote id
1001 (the World Council / World Patron). That is required, not cosmetic:

* the senate frame calls `GetVotingInfo(out, 0)` (`FUN_14031d1a0`),
* the council frame calls `GetVotingInfo(out, 1)` (`FUN_140391347`, `mov r8d,1`),
* `Vote_V1` sends the *document's f2*, not the key - which is why our logs show
  `Vote_V1(1001, faction)` for the council and `Vote_V1(0, faction)` for the
  senate,
* `GetVoteCount_V1` / `GetLastVoters_V1` are then polled with that same f2
  (the live session alternates `0` and `1001` every ~10 s).

## The one thing that means "you already voted"

**f5 only.** Both frames convert the document into a game struct
(`FUN_140db98e0`), keep the own vote at struct `+0x18`, and read it through
`GetVotingSystem()->GetOwnVote()` - a *local map lookup*, not an RPC:

* senate: `FUN_14031e670` compares it with `-1`; if it is a faction,
  `FUN_14031e2a0` shows that faction's **VOTED** badge and the Lua
  `MainMenuSenate.UpdateVotePanels` (`data/scripts/fastgui.lua`) disables all
  three vote buttons;
* council: `FUN_140392cc0` sets `Activated`/`Toggled` on the faction buttons,
  and `FUN_140391200` re-enables them when it is `-1`.

`GetLastVoters_V1` name matching only bolds the player's row
(`<span class="ownVote">`); `GetVoteCount_V1` and `GetVotingStats_V1` have
nothing to do with the voted state.

And `f3` gates all of it: with `f3 == 0` both frames take the "results" branch,
the vote widgets are not even consulted. So a vote phase that is open (`f3 = 1`)
is a precondition for the panel to say "voted" at all.

## Why the World Council forgot (the bug)

The client caches the two documents at login and only ever updates the own vote
from the `Vote_V1` **reply callback** (`FUN_140db8540`: it finds the cached node
whose Vote id is the id it sent and writes the faction into +0x18). It never
re-requests `GetVotingInfos_V1` after a vote - the live capture shows only
`GetVoteCount` polls. So after a restart the *only* source of "voted" is our
document.

Our server had two defects there:

1. **`apply_vote` wrote the own vote only into a document that already existed**
   (`if isinstance(record, dict): record["f6"] = faction`) - nothing created one.
   Every save has an `infos["0"]` record, so the senate worked; the World
   Council slot had none, so its vote went into the counts/votes/reward books
   and the own vote was simply dropped. After a restart the council document was
   rebuilt with `f5 = 0xFFFFFFFF` → "not voted" → vote again (only the reward
   ticket stopped a second +50).
2. **A save was never healed.** `_migrate_state` only built `infos` when the
   whole map was missing, so a partially populated map stayed partial for ever.

Both are fixed, and the fix is deliberately **self-healing** rather than a
one-shot migration:

* `apply_vote` calls `_ensure_info()`, which creates the missing document with
  the right Vote id (1001 for slot 1), so any future slot works too;
* `elections_from_state(..., voter=pid)` recovers the own vote from the durable
  **vote log** (`voting.votes`, newest record wins) whenever the stored document
  does not have a usable one - so an old save answers correctly even before it
  is written back;
* on load, `heal_infos()` creates the missing documents and fills the own vote
  from the log (only when the election has a single voter, which is the normal
  single-player case - a shared election is answered per session instead of
  guessing), and `Store.load()` writes the repaired file back once.

New keys also copy the key style already in the file (`"0"` vs `0`), because a
mixed-key dict makes `json.dump(sort_keys=True)` raise and silently fall back to
an unsorted write.

## What proves it

* `tests/test_state_fixes.py :: test_council_vote_survives_a_restart` seeds the
  user's **verbatim** save (council vote present, `infos` holding only slot 0)
  and asserts: the load heals it, the served document for key 1 has
  `f2 = 1001`, `f3 = 1` and `f5 = 2`, a fresh vote on a partial save creates the
  document, a repeated vote pays nothing, and the state still serialises with
  sorted keys.
* `server/probe_rmc.py` (run by `PROGRESS.bat` against a live server) now votes
  in the council and then asserts `GetVotingInfos` reports that vote - the
  end-to-end version of the same check, through the real websocket.
* Verified by hand against a real server process: vote in process A, restart,
  read in process B → `key=1 vote_id=1001 own_vote=2`.

## If it ever happens again

Set `RV_DUMP_REPLY=1` before `PLAY.bat`: every reply body is then hex-dumped to
`logs\server.log`. The voting document is 49 bytes per record, so the own vote
is the u32 at `record + 21` - one vote plus one restart and the wire says
whether the server or the client is wrong.

## Limits worth knowing

* A vote cannot be *blocked* once cast: the client hides the buttons, but the
  server accepts a second `Vote_V1` for the same election (it just must not pay
  twice - it does not, and it also keeps the newest choice as the own vote).
* `f9` must stay `-1` (or 0..2): the client uses it as an array index with no
  bounds check.
* The election window dates (f4/f6/f7) are display-only in this build; nothing
  opens or closes a vote by clock. `f3` is the switch.
* There is no "vote closed → show the result" logic in either frame that we can
  drive: closing a vote is `f3 = 0`, which swaps in the results widgets whose
  text comes from the world-patron data we do not have (see
  `known-issues.md` #10).
