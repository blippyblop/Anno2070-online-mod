# What works in game, and what to expect

_Last verified offline 2026-11-08 (SELFTEST + all suites green); the 2026-11-07
live session still stands as the last in-game run (45 RV calls, 0 failed replies,
0 save failures). Every id used below is one the game itself ships - no invented
content._

## Start here

```
SELFTEST.bat     checks career, ARK, events, mail and the save - no game needed
PLAY.bat         starts the local server and the game (once; no retry loop)
PROGRESS.bat     after a session: what the game asked for, and what got saved
UNINSTALL.bat    removes the mod again (keeps your state in %LOCALAPPDATA%)
```

## Online features and where they show up

| feature | what you should see | timing |
|---|---|---|
| online session | the game leaves "Offline mode"; chat/mail/friends/event icons are live | at login |
| **career / reputation** | Profile → Reputation: three panels (Eco/Tycoon/Tech). The server stores the points and counts them, but this build of the game never asks the server for them - see the note below | stored at once, shown when the client asks |
| senate + World Council votes | both panels accept a vote and keep it; a vote is worth 50 career points | immediate, saved at once |
| ARK item box | six sockets, real items; the game can move items in and out | at login, saved per change |
| world events | one of the five real chapters per **month**, its three missions, real reward/payout, and a global progress bar that fills up every day | new chapter each campaign (30 days) |
| event mail | the chapter's own intro mail, a progress mail per solved mission, a conclusion mail at the end | when the event rolls / you finish a mission |
| achievements | the world-event achievements unlock as you solve their missions | immediately |
| mailbox badge | counts your unread mail | at login |
| chat / friends | channels and friend list answer; messages and friend adds are recorded | on use |

## Career points: what the server does and what the game does

`Vote_V1` and `CompleteMission_V1` return **no data** - the client announces
"+50" itself. The server credits every vote, every solved mission and every
crossed campaign tier into `server\state.json` (add-only, one payment per
election, `PROGRESS.bat` prints the total). The in-game career page, however, is
drawn from the client's **own** map: in 243 captured logins it probes
`AreReputationPointsMissing_V1` and never once calls `GetReputationPoints_V1`.
So the number is reliably *saved*, and whether it is *shown* depends on the
client asking - see `docs/career.md` for the proof and for the one-session
probe (`RV_REPUTATION_PROBE`) that settles it.

What you can check any time, without the game:

```
PROGRESS.bat                  career total, votes, ARK box, campaign progress,
                              and a LIVE check of the server that is running
PROGRESS.bat selftest         a full fake session in a temp file
server\probe_rmc.py 8443      the same live check on its own (server must be up)
```

The live check asks the running server the questions the game asks - it votes
once in the World Council, then reads the council's bars, the career map and the
event - so "did the fix work" is answerable without starting Anno. It prints
`ALL LIVE PROBES PASSED` or the exact line that failed.

## The world event is a month, and the world is simulated

A campaign lasts 30 UTC days and hosts one of the five real chapters. Over those
days the "worldside progress" bar fills up: every campaign draws a global target
between **25,000 and 125,000 points** and the number climbs a little each day
(the pace is uneven on purpose, and the last day lands exactly on the target).
When the world crosses one of the chapter's four tier thresholds, **every player
gets that tier's reputation** - once, ever, per campaign. Nothing to do but
play; `PROGRESS.bat` shows `worldside progress: … of … points (day n of 30)`.

## ARK rewards and sockets

An ARK item must sit in the socket group of its own quality (A: 1 socket,
B: 2, C: 3) - the game refuses a mismatched slot. Missions whose shipped reward
is an item therefore fill the first free slot **of that group**; if the group is
full, one of our six starter items (or an earlier mission reward) makes way, and
an item you placed yourself is never touched. `RV_ARK_REWARDS=0` in the
environment turns mission item grants off.

Eight of the fifteen world-event reward GUIDs are **not** items at all - they are
profile titles, a portrait and a decoration building. Those missions pay
reputation only, which is what the game itself does with them.

## When a launch ends in offline mode

Roughly two launches in five, the client cancels its own login during bootstrap
(its own timers, not the server's answers - measured). No crash, no data loss:
quit and start again. Nothing was lost - the save is written after every change.

## Never end the game with Task Manager

A forced close can truncate `Config\Engine.ini`, after which every later launch
fails. Quit from the game's own menu. If it ever happens, delete
`%APPDATA%\Ubisoft\ANNO 2070\Config` and start again.
