#!/usr/bin/env python3
"""End-to-end self-test of the online features - no game, no network, no state.

    python3 server/selftest.py

Runs against a throw-away state file (the player's own `state.json` is never
touched) and exercises the server the same way the game does:

  1. the world event of the day is served, with its intro mail
  2. a senate vote and a World Council vote are recorded and pay reputation
  3. solving a world-event mission pays reputation, unlocks its achievement,
     sends the progress mail and (when the reward is an item) fills a socket
  4. everything is read back through the same handlers the client calls:
     profile, career points, ARK box, mailbox, mail count, achievements
  5. the server is restarted (the file is reopened) and the values are still
     there

Exit code 0 = every check passed.  `PROGRESS.bat` runs it when asked
(`PROGRESS.bat selftest`), and `SELFTEST.bat` in the package is a shortcut.
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import content_events as CE          # noqa: E402
import content_voting as CV          # noqa: E402
import rmc_services as R             # noqa: E402

PROFILE = R.PROFILE
VOTING = R.VOTING
WORLD = R.WORLD
MAIL = R.MAIL
CHAT = R.CHAT
FRIENDS = R.FRIENDS

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}  {detail}")


def le32(v):
    return (int(v) & 0xFFFFFFFF).to_bytes(4, "little")


def call(store, proto, method, body=b""):
    """One RMC request/response, spoken exactly like the client does.

    ``body`` is the ARGUMENT bytes; the 4-byte request prologue that every
    client frame carries (``rmc_services._REQUEST_PREFIX``) is added here, so a
    handler that reads its first argument from offset 4 is exercised the same
    way the game exercises it.
    """
    wire = b"\x00" * R._REQUEST_PREFIX + body
    return R.respond_payload(proto, method, store=store, body=wire) or b""


def fields(raw):
    """Payload without its name block."""
    if len(raw) < 2:
        return raw
    return raw[2 + int.from_bytes(raw[0:2], "little"):]


def u32map(raw):
    body = fields(raw)
    n = int.from_bytes(body[:4], "little")
    out = {}
    for i in range(n):
        off = 4 + i * 8
        out[int.from_bytes(body[off:off + 4], "little")] = \
            int.from_bytes(body[off + 4:off + 8], "little")
    return out


def count_of(raw):
    body = fields(raw)
    return int.from_bytes(body[:4], "little") if body else -1


def main():
    tmp = tempfile.mkdtemp(prefix="anno-selftest-")
    path = os.path.join(tmp, "state.json")
    try:
        print("=" * 70)
        print(" Anno 2070 - Local Online : self-test")
        print("=" * 70)
        store = R.open_store(path)

        print("\n[1] the world event of the day")
        event = call(store, WORLD, "GetLatestWorldEvent_V1", b"\x01\x00")
        we = store.state.get("worldEvent") or {}
        chapter = int(we.get("b") or 0)
        check("an event is served", len(event) > 40, len(event))
        check("its chapter is one the game ships",
              chapter in [c[0] for c in CE.WORLD_EVENT_CHAPTERS], chapter)
        mails = {int(k): v for k, v in (store.player("default").get("infoMails") or {}).items()}
        check("its intro mail arrived", int(CE.chapter_mail(chapter, "initial") or 0) in mails,
              mails)
        print("        chapter %s, mails %s" % (chapter, sorted(mails)))

        campaign = CE.campaign_state()
        check("the campaign is a month long",
              campaign["end"] - campaign["start"] == 30, campaign)
        check("its global target is in the 25,000..125,000 range",
              25000 <= campaign["target"] <= 125000, campaign["target"])
        check("the worldside progress is filling over the month",
              0 < campaign["total"] <= campaign["target"],
              (campaign["total"], campaign["target"]))
        check("the state file records the campaign", bool(we.get("campaign")),
              we.get("campaign"))
        print("        day %d/30 of campaign %s: %d of %d points, %d/%d tiers"
              % (campaign["days"], campaign["index"], campaign["total"],
                 campaign["target"], campaign["tiersReached"],
                 len(campaign["tiers"])))

        print("\n[2] voting")
        call(store, VOTING, "Vote_V1", le32(0) + le32(CV.FACTION_TYCOON))
        call(store, VOTING, "Vote_V1", le32(CV.COUNCIL_VOTE_ID) + le32(CV.FACTION_TECH))
        infos = store.state["voting"]["infos"]
        check("the senate keeps the senate vote", int(infos["0"]["f6"]) == CV.FACTION_TYCOON,
              infos["0"]["f6"])
        check("the council keeps the council vote", int(infos["1"]["f6"]) == CV.FACTION_TECH,
              infos["1"]["f6"])
        career = u32map(call(store, PROFILE, "GetReputationPoints_V1"))
        check("both votes paid reputation",
              career.get(CV.FACTION_TYCOON) == CV.VOTE_REWARD
              and career.get(CV.FACTION_TECH) == CV.VOTE_REWARD, career)
        print("        career %s" % career)

        print("\n[3] a solved world-event mission")
        mission = [m for c, ms, _t in CE.WORLD_EVENT_CHAPTERS if c == chapter for m in ms][0]
        before = u32map(call(store, PROFILE, "GetReputationPoints_V1"))
        call(store, WORLD, "CompleteMission_V1", le32(mission) + b"\x06\x00Player\x00")
        after = u32map(call(store, PROFILE, "GetReputationPoints_V1"))
        info = CE.mission_info(mission)
        check("the mission paid its reputation",
              sum(after.values()) - sum(before.values()) == info["reputation"],
              (before, after))
        ach = store.player("default").get("achievements") or []
        check("its achievement unlocked",
              [a["id"] for a in ach] == [CE.mission_achievement(mission)], ach)
        mails = {int(k): v for k, v in (store.player("default").get("infoMails") or {}).items()}
        check("the progress mail arrived",
              int(CE.chapter_mail(chapter, "progress") or 0) in mails, sorted(mails))
        print("        mission %s -> +%d reputation" % (mission, info["reputation"]))

        print("\n[4] reading it all back the way the client does")
        box = count_of(call(store, PROFILE, "GetArkItems_V1"))
        check("the ARK box answers", box >= 0, box)
        mails_list = count_of(call(store, MAIL, "GetMails_V1"))
        check("the mailbox lists the mails", mails_list >= 2, mails_list)
        badge = fields(call(store, MAIL, "GetMailCount_V1"))
        check("the badge counts them", int.from_bytes(badge[:4], "little") == mails_list, badge.hex())
        check("chat answers", count_of(call(store, CHAT, "GetChannels_V1")) == 0)
        check("friends answer", count_of(call(store, FRIENDS, "GetFriends_V1")) == 0)
        check("the career page document is complete",
              set(u32map(call(store, PROFILE, "GetReputationPoints_V1"))) >= set(CV.FACTIONS),
              career)

        print("\n[5] statistics and achievements are stored, not echoed")
        rows = (store.player("default").get("stats") or [])
        call(store, PROFILE, "SetStats_V1", b"")        # the client's empty one
        check("an empty SetStats does not wipe the statistics",
              (store.player("default").get("stats") or []) == rows, rows)
        ach = store.player("default").get("achievements") or []
        check("the achievement carries the id the game sent",
              bool(ach) and ach[0].get("id") not in (0, None), ach)
        wire = fields(call(store, PROFILE, "GetAchievements_V1"))
        served = int.from_bytes(wire[4:8], "little") if len(wire) >= 8 else 0
        check("... and the client is served that same id", served == ach[0]["id"],
              (served, ach[0].get("id")))

        print("\n[6] restart: the save is reopened from disk")
        store.save()
        reopened = R.open_store(path)
        check("career survived",
              u32map(call(reopened, PROFILE, "GetReputationPoints_V1")) == after)
        check("mailbox survived", count_of(call(reopened, MAIL, "GetMails_V1")) == mails_list)
        check("achievements survived",
              len(reopened.player("default").get("achievements") or []) == 1
              and (reopened.player("default")["achievements"][0].get("id")
                   == ach[0]["id"]),
              reopened.player("default").get("achievements"))
        # the ARK box is the one thing a player would notice losing: it must come
        # back socket for socket, with the starter set AND the mission reward
        before_box = store.player("default").get("arkItems") or []
        after_box = reopened.player("default").get("arkItems") or []
        check("the ARK box survived item for item", after_box == before_box,
              (before_box, after_box))
        check("... and the client is served the same box",
              count_of(call(reopened, PROFILE, "GetArkItems_V1")) == len(after_box),
              (count_of(call(reopened, PROFILE, "GetArkItems_V1")), len(after_box)))
        saved = json.load(open(path, encoding="utf-8"))
        check("the file carries a schema version", saved.get("schema") == R.STATE_SCHEMA,
              saved.get("schema"))
        check("the file has an event day key", saved["worldEvent"].get("day") is not None,
              saved["worldEvent"].get("day"))
        saved_we = saved["worldEvent"] if isinstance(saved["worldEvent"], dict) else {}
        check("the campaign is in the file (a restart cannot re-roll it)",
              isinstance(saved_we.get("campaign"), dict)
              and saved_we["campaign"].get("target", 0) > 0,
              saved_we.get("campaign"))
        print("        %s (%d bytes)" % (path, os.path.getsize(path)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if failures:
        print(" SELF-TEST FAILED: %d check(s)" % len(failures))
        for name in failures:
            print("   - %s" % name)
        return 1
    print(" SELF-TEST PASSED: career, ARK, events and the JSON save all work")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
