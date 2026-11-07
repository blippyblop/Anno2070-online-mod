# What works in game, and what to expect

_Last verified 2026-11-07 with a live session (45 RV calls, 0 failed replies,
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
| **career / reputation** | Profile → Reputation: one panel per faction, with the points you earned | **after the next login** - the profile is read once per session |
| senate + World Council votes | both panels accept a vote and keep it; a vote is worth 50 career points | immediate, saved at once |
| ARK item box | six sockets, real items; the game can move items in and out | at login, saved per change |
| world events | one of the five real chapters per day, its three missions, real reward/payout | new event each UTC day |
| event mail | the chapter's own intro mail, a progress mail per solved mission, a conclusion mail at the end | when the event rolls / you finish a mission |
| achievements | the world-event achievements unlock as you solve their missions | immediately |
| mailbox badge | counts your unread mail | at login |
| chat / friends | channels and friend list answer; messages and friend adds are recorded | on use |

## Why career points appear only after a restart

`Vote_V1` and `CompleteMission_V1` return **no data** - the client announces
"+50" itself. The only document that carries a career total is
`GetReputationPoints_V1`, and the profile reads it once per login. So: vote or
solve a mission, quit, start again, then open Profile → Reputation. `PROGRESS.bat`
between those steps is the receipt: it prints the career total straight from
`server\state.json`.

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
